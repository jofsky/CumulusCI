import github3.exceptions

from cumulusci.core.utils import import_global
from cumulusci.core.github import (
    is_label_on_pull_request,
    get_pull_requests_by_head,
    get_pull_requests_with_base_branch,
)
from cumulusci.tasks.release_notes.exceptions import CumulusCIException
from cumulusci.tasks.release_notes.parser import ChangeNotesLinesParser
from cumulusci.tasks.release_notes.parser import GithubLinesParser
from cumulusci.tasks.release_notes.parser import IssuesParser
from cumulusci.tasks.release_notes.provider import StaticChangeNotesProvider
from cumulusci.tasks.release_notes.provider import DirectoryChangeNotesProvider
from cumulusci.tasks.release_notes.provider import GithubChangeNotesProvider


class BaseReleaseNotesGenerator(object):
    def __init__(self):
        self.change_notes = []
        self.empty_change_notes = []
        self.init_parsers()
        self.init_change_notes()

    def __call__(self):
        self._parse_change_notes()
        return self.render()

    def init_change_notes(self):
        self.change_notes = self._init_change_notes()

    def _init_change_notes(self):
        """ Subclasses should override this method to return an initialized
        subclass of BaseChangeNotesProvider """
        return []

    def init_parsers(self):
        """ Initializes the parser instances as the list self.parsers """
        self.parsers = []
        self._init_parsers()

    def _init_parsers(self):
        """ Subclasses should override this method to initialize their
        parsers """
        pass

    def _parse_change_notes(self):
        """ Parses all change_notes in self.change_notes() through all parsers
        in self.parsers """
        for change_note in self.change_notes():
            self._parse_change_note(change_note)

    def _parse_change_note(self, change_note):
        """ Parses an individual change note through all parsers in
        self.parsers. If no lines were added then appends the change
        note to the list of empty PRs"""
        line_added_by_parsers = False
        for parser in self.parsers:
            line_added = parser.parse(change_note)
            if not line_added_by_parsers:
                line_added_by_parsers = line_added

        if not line_added_by_parsers:
            self.empty_change_notes.append(change_note)

    def render(self):
        """ Returns the rendered release notes from all parsers as a string """
        release_notes = []
        for parser in self.parsers:
            parser_content = parser.render()
            if parser_content is not None:
                release_notes.append(parser_content)
        return u"\r\n\r\n".join(release_notes)


class StaticReleaseNotesGenerator(BaseReleaseNotesGenerator):
    def __init__(self, change_notes):
        self._change_notes = change_notes
        super(StaticReleaseNotesGenerator, self).__init__()

    def _init_parsers(self):
        self.parsers.append(ChangeNotesLinesParser(self, "Critical Changes"))
        self.parsers.append(ChangeNotesLinesParser(self, "Changes"))
        self.parsers.append(IssuesParser(self, "Issues Closed"))

    def _init_change_notes(self):
        return StaticChangeNotesProvider(self, self._change_notes)


class DirectoryReleaseNotesGenerator(BaseReleaseNotesGenerator):
    def __init__(self, directory):
        self.directory = directory
        super(DirectoryReleaseNotesGenerator, self).__init__()

    def _init_parsers(self):
        self.parsers.append(ChangeNotesLinesParser(self, "Critical Changes"))
        self.parsers.append(ChangeNotesLinesParser(self, "Changes"))
        self.parsers.append(IssuesParser(self, "Issues Closed"))

    def _init_change_notes(self):
        return DirectoryChangeNotesProvider(self, self.directory)


class ParentPullRequestNotesGenerator(BaseReleaseNotesGenerator):
    """Aggregates notes from child pull requests in to a parent pull request"""

    # Header where unaggregated child pull requests are linked to
    UNAGGREGATED_SECTION_HEADER = "\r\n\r\n# Unaggregated Pull Requests"

    def __init__(self, github, repo, project_config):

        self.repo = repo
        self.github = github
        self.link_pr = True  # create links to pr on parsed change notes
        self.has_issues = True  # need for parsers
        self.do_publish = True  # need for parsers
        self.parser_config = (
            project_config.project__git__release_notes__parsers.values()
        )
        super(ParentPullRequestNotesGenerator, self).__init__()

    def _init_parsers(self):
        """Invoked from Super Class"""
        for cfg in self.parser_config:
            parser_class = import_global(cfg["class_path"])
            self.parsers.append(parser_class(self, cfg["title"]))

        # Additional parser to collect developer notes above tracked headers
        self.parsers.append(GithubLinesParser(self, title=None))
        self.parsers[-1]._in_section = True

    def aggregate_child_change_notes(self, pull_request):
        """Given a pull request, aggregate all change notes from child pull requests.
        Child pull requests are pull requests that have a base branch
        equal to the the given pull request's head."""
        self.change_notes = get_pull_requests_with_base_branch(
            self.repo, pull_request.head.ref
        )
        self.change_notes = list(filter(is_merged, self.change_notes))
        if len(self.change_notes) == 0:
            return

        for change_note in self.change_notes:
            self._parse_change_note(change_note)

        body = []
        for parser in self.parsers:
            if parser.title == None:
                parser.title = "Notes From Child PRs"
            parser_content = parser.render()
            if parser_content:
                body.append(parser_content)

        if self.empty_change_notes:
            body.extend(render_empty_pr_section(self.empty_change_notes))
        new_body = "\r\n".join(body)

        if not pull_request.update(body=new_body):
            raise CumulusCIException(
                "Update of pull request #{} failed.".format(pull_request.number)
            )

    def update_unaggregated_pr_header(self, pull_request_to_update, branch_name_to_add):
        """Updates the 'Unaggregated Pull Requests' section header with a link
        to the new child branch pull request"""
        body = pull_request_to_update.body
        if self.UNAGGREGATED_SECTION_HEADER not in body:
            body += self.UNAGGREGATED_SECTION_HEADER

        pull_requests = get_pull_requests_with_base_branch(
            self.repo, branch_name_to_add.split("__")[0], branch_name_to_add
        )

        if len(pull_requests) == 0:
            raise CumulusCIException(
                "No pull request for branch {} found.".format(branch_name_to_add)
            )
        elif len(pull_requests) > 1:
            raise CumulusCIException(
                "Expected one pull request, found {} for branch {}".format(
                    len(pull_requests), branch_name_to_add
                )
            )

        pull_request_link = markdown_link_to_pr(pull_requests[0])
        if pull_request_link not in body:
            body += "\r\n* " + pull_request_link
            pull_request_to_update.update(body=body)
            return


def is_merged(pull_request):
    return pull_request.merged_at is not None


class GithubReleaseNotesGenerator(BaseReleaseNotesGenerator):
    def __init__(
        self,
        github,
        github_info,
        parser_config,
        current_tag,
        last_tag=None,
        link_pr=False,
        publish=False,
        has_issues=True,
        include_empty=False,
    ):
        self.github = github
        self.github_info = github_info
        self.parser_config = parser_config
        self.current_tag = current_tag
        self.last_tag = last_tag
        self.link_pr = link_pr
        self.do_publish = publish
        self.has_issues = has_issues
        self.include_empty_pull_requests = include_empty
        self.lines_parser_class = None
        self.issues_parser_class = None
        super(GithubReleaseNotesGenerator, self).__init__()

    def __call__(self):
        release = self._get_release()
        content = super(GithubReleaseNotesGenerator, self).__call__()
        content = self._update_release_content(release, content)
        if self.do_publish:
            release.edit(body=content)
        return content

    def _init_parsers(self):
        for cfg in self.parser_config:
            parser_class = import_global(cfg["class_path"])
            self.parsers.append(parser_class(self, cfg["title"]))

    def _init_change_notes(self):
        return GithubChangeNotesProvider(self, self.current_tag, self.last_tag)

    def _get_release(self):
        repo = self.get_repo()
        try:
            return repo.release_from_tag(self.current_tag)
        except github3.exceptions.NotFoundError:
            raise CumulusCIException(
                "Release not found for tag: {}".format(self.current_tag)
            )

    def _update_release_content(self, release, content):
        """Merge existing and new release content."""
        new_body = []
        if release.body:
            current_parser = None
            is_start_line = False
            for parser in self.parsers:
                parser.replaced = False

            # update existing sections
            for line in release.body.splitlines():

                if current_parser:
                    if current_parser._is_end_line(current_parser._process_line(line)):
                        parser_content = current_parser.render()
                        if parser_content:
                            # replace existing section with new content
                            new_body.append(parser_content + "\r\n")
                        current_parser = None

                for parser in self.parsers:
                    if (
                        parser._render_header().strip()
                        == parser._process_line(line).strip()
                    ):
                        parser.replaced = True
                        current_parser = parser
                        is_start_line = True
                        break
                    else:
                        is_start_line = False

                if is_start_line:
                    continue
                if current_parser:
                    continue
                else:
                    # preserve existing sections
                    new_body.append(line.strip())

            # catch section without end line
            if current_parser:
                new_body.append(current_parser.render())

            # add new sections at bottom
            for parser in self.parsers:
                parser_content = parser.render()
                if parser_content and not parser.replaced:
                    new_body.append(parser_content + "\r\n")

        else:  # no release.body
            new_body.append(content)

        # add empty PR section
        if self.include_empty_pull_requests:
            new_body.extend(render_empty_pr_section(self.empty_change_notes))
        content = u"\r\n".join(new_body)
        return content

    def get_repo(self):
        return self.github.repository(
            self.github_info["github_owner"], self.github_info["github_repo"]
        )


def render_empty_pr_section(empty_change_notes):
    section_lines = []
    if empty_change_notes:
        section_lines.append("\n# Pull requests with no release notes")
        for change_note in empty_change_notes:
            section_lines.append("\n* {}".format(markdown_link_to_pr(change_note)))
    return section_lines


def markdown_link_to_pr(change_note):
    return "{} [[PR{}]({})]".format(
        change_note.title, change_note.number, change_note.html_url
    )
