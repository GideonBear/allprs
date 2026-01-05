from __future__ import annotations

import argparse
import asyncio
import contextlib
import re
import subprocess
import sys
import webbrowser
from asyncio import Event, Future, Task
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from githubkit import GitHub
from githubkit.exception import RequestFailed
from prompt_toolkit.input import create_input

from allprs import config
from allprs.config import pr_queries
from allprs.utils import (
    areadchar,
    clear,
    group_by,
    print_line,
)


if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from githubkit.versions.latest.models import (
        CheckRun,
        Commit,
        IssueSearchResultItem,
        PullRequest,
    )


class Args(argparse.Namespace):
    urls_or_titles: list[str]


def parse_args() -> Args:
    parser = argparse.ArgumentParser("allprs")

    group = parser.add_mutually_exclusive_group()

    group.add_argument(
        "urls_or_titles",
        nargs="*",
        type=str,
        help="When specified, ignore `pr_queries` and merge only these PRs. "
        "Specify URLs or a title substring.",
    )

    return parser.parse_args(namespace=Args())


def main() -> None:
    args = parse_args()
    runner = Runner(args)
    asyncio.run(runner.run())
    sys.exit(runner.exit_code)


class DoneType:
    pass


DONE = DoneType()


@dataclass
class FullPr:
    pr: PullRequest
    diff: str
    status: tuple[str, str | None]


class Runner:
    def __init__(self, args: Args) -> None:
        self.urls = None
        self.title = None
        if args.urls_or_titles:
            if all(s.startswith("https://github.com/") for s in args.urls_or_titles):
                self.urls = args.urls_or_titles
            else:
                if len(args.urls_or_titles) > 1:
                    # TODO(GideonBear): arbitrary restriction  # noqa: FIX002, TD003
                    print("Error: expected only one title to be specified")
                    sys.exit(2)
                # Must be one since it's not empty
                self.title = args.urls_or_titles[0]

        token = (
            subprocess
            .run(["gh", "auth", "token"], check=True, stdout=subprocess.PIPE)  # noqa: S607
            .stdout.decode()
            .strip()
        )
        self.gh = GitHub(token)
        self.queue: asyncio.Queue[
            tuple[
                str,  # title
                str,  # diff
                Sequence[FullPr],
            ]
            | DoneType
        ] = asyncio.Queue()
        self.follow_tasks: asyncio.TaskGroup
        self.input = create_input()
        self.quit = Event()
        self.login = self.gh.rest.users.get_authenticated().parsed_data.login
        self.warnings: list[str] = []
        self.exit_code = 0

    async def run(self) -> None:
        async with asyncio.TaskGroup() as self.follow_tasks:
            ui_task = asyncio.create_task(self.ui())

            # We care about this task being lost if we quit,
            # so we need to cancel it (if we quit)
            queue_fill_task: Future[list[None]] | Task[object]
            if self.urls:
                queue_fill_task = asyncio.create_task(self.do_pr_urls(self.urls))
            else:
                queue_fill_task = asyncio.gather(*[
                    self.do_pr_query(pr_query_data) for pr_query_data in pr_queries
                ])
            # We don't care about this task being lost if we quit
            quit_task = asyncio.create_task(self.quit.wait())
            # If the queue is filled or the user wants to quit...
            await asyncio.wait(
                [queue_fill_task, quit_task], return_when=asyncio.FIRST_COMPLETED
            )
            if self.quit.is_set():
                queue_fill_task.cancel()
                # suppress traceback
                with contextlib.suppress(asyncio.CancelledError):
                    await queue_fill_task

            # We don't care about this task being lost if we quit
            queue_empty_task = asyncio.create_task(self.queue.join())
            # If the queue is empty or the user wants to quit...
            await asyncio.wait(
                [queue_empty_task, quit_task], return_when=asyncio.FIRST_COMPLETED
            )
            # Let the ui task know we're done (if the queue was empty)
            self.queue.put_nowait(DONE)
            await ui_task

            print("Waiting for last follow-up tasks to complete...")

        for warning in self.warnings:
            print(f"WARNING: {warning}")

    async def do_pr_query(self, pr_query_data: dict[str, str]) -> None:
        pr_query = pr_query_data["query"]

        all_prs: Iterable[PullRequest] = await asyncio.gather(*[
            self.get_pr(pr)
            async for pr in self.gh.rest.paginate(
                self.gh.rest.search.async_issues_and_pull_requests,
                q=f"is:pr state:open {config.repo_query} {pr_query}",
                map_func=lambda r: r.parsed_data.items,
            )
        ])

        if "head_branch_regex" in pr_query_data:
            all_prs = (
                pr
                for pr in all_prs
                if re.match(pr_query_data["head_branch_regex"], pr.head.ref)
            )

        await self.do_pr_set(all_prs)

    async def do_pr_urls(self, urls: list[str]) -> None:
        all_prs: Iterable[PullRequest] = await asyncio.gather(*[
            self.get_pr_from_url(url) for url in urls
        ])

        await self.do_pr_set(all_prs)

    async def get_pr_from_url(self, url: str) -> PullRequest:
        url = url.removeprefix("https://github.com/")
        owner, repo, _pull, number, *_rest = url.split("/")
        return (
            await self.gh.rest.pulls.async_get(owner, repo, int(number))
        ).parsed_data

    async def do_pr_set(self, all_prs: Iterable[PullRequest]) -> None:
        title_groups = group_by(lambda x: x.title, all_prs)

        for title, title_prs in title_groups.items():
            # If we specified a title and it's not in here:
            if self.title and self.title not in title:
                continue  # Skip

            await self.do_title_group(title, title_prs)

    async def do_title_group(
        self, title: str, title_prs: Sequence[PullRequest]
    ) -> None:
        # Query the diff only after status checks are done, because new commits can get
        #  pushed by pre-commit.ci and similar
        statuses = await asyncio.gather(*[self.wait_for_status(pr) for pr in title_prs])

        diffs = await asyncio.gather(*[self.get_diff(pr) for pr in title_prs])

        title_prs_full = map(FullPr, title_prs, diffs, statuses, strict=True)
        diff_groups: dict[str, list[FullPr]] = group_by(
            lambda x: x.diff, title_prs_full
        )

        # Make sure to put an entire title group into the queue at once,
        # without any awaits in between
        for diff, diff_prs in diff_groups.items():
            self.queue.put_nowait((title, diff, diff_prs))

    async def wait_for_status(self, pr: PullRequest) -> tuple[str, str | None]:
        commit = await self.get_last_commit(pr)
        while True:
            state, fail_example = await self.get_status(pr, commit)
            if state == "pending":
                await asyncio.sleep(5)
            else:
                # Because new commits can get pushed by pre-commit.ci and similar:
                # If it failed...
                if state == "failure":
                    # ...and there's a new commit made since the failure...
                    new_commit = await self.get_last_commit(pr)
                    if commit.sha != new_commit.sha:
                        # ...continue with the new commit
                        commit = new_commit
                        # Wait until new checks are started to avoid
                        #  seeing "success" before any checks were added
                        await asyncio.sleep(5)
                        continue
                # We assume any other states will never result in a new commit
                return state, fail_example

    async def get_last_commit(self, pr: PullRequest) -> Commit:
        assert pr.base.repo.owner is not None  # noqa: S101
        return [  # type: ignore[var-annotated, no-any-return]
            x
            async for x in self.gh.rest.paginate(
                self.gh.rest.pulls.async_list_commits,
                owner=pr.base.repo.owner.login,
                repo=pr.base.repo.name,
                pull_number=pr.number,
            )
        ][-1]

    async def get_status(
        self, pr: PullRequest, commit: Commit
    ) -> tuple[str, str | None]:
        # TODO(GideonBear): Refactor and split up this function  # noqa: FIX002, TD003
        assert pr.base.repo.owner is not None  # noqa: S101

        status = await self.gh.rest.repos.async_get_combined_status_for_ref(
            owner=pr.base.repo.owner.login,
            repo=pr.base.repo.name,
            ref=commit.sha,
        )
        status_state = status.parsed_data.state
        if status_state == "pending" and status.parsed_data.total_count == 0:
            status_state = "success"

        check_run_state = "success"
        fail_example: str | None = None
        check_run: CheckRun
        async for check_run in self.gh.rest.paginate(
            self.gh.rest.checks.async_list_for_ref,
            owner=pr.base.repo.owner.login,
            repo=pr.base.repo.name,
            ref=commit.sha,
            map_func=lambda x: x.parsed_data.check_runs,
        ):
            conclusion = check_run.conclusion
            if conclusion in {"success", "neutral", "skipped"}:
                pass
            elif conclusion is None and check_run_state in {"success", "pending"}:
                check_run_state = "pending"
            elif conclusion is None and check_run_state == "failure":
                pass
            elif conclusion in {"failure", "action_required", "cancelled", "timed_out"}:
                fail_example = check_run.html_url
                check_run_state = "failure"
            else:
                raise AssertionError(conclusion, check_run_state)

        if status_state == "failure" or check_run_state == "failure":
            state = "failure"
        elif status_state == "pending" or check_run_state == "pending":
            state = "pending"
        elif status_state == "success" and check_run_state == "success":
            state = "success"
        else:
            raise AssertionError(status_state, check_run_state)

        return state, fail_example

    async def get_pr(self, pr_issue: IssueSearchResultItem) -> PullRequest:
        repository = await self.gh.arequest("GET", pr_issue.repository_url)
        return (
            await self.gh.rest.pulls.async_get(
                owner=repository.parsed_data["owner"]["login"],
                repo=repository.parsed_data["name"],
                pull_number=pr_issue.number,
            )
        ).parsed_data

    async def get_diff(self, pr: PullRequest) -> str:
        resp = await self.gh.arequest(
            "GET",
            pr.url,
            headers={
                "Accept": "application/vnd.github.diff",
            },
        )
        diff = resp.text
        return "\n".join(
            line for line in diff.split("\n") if not line.startswith("index")
        )

    async def ui(self) -> None:
        while True:
            clear()
            print("Waiting for diffgroup...")
            x = await self.queue.get()
            if isinstance(x, DoneType):
                return
            title, diff, diff_prs = x
            result = await self.ui_diff_group(title, diff, diff_prs)
            self.queue.task_done()
            if result == "quit":
                self.quit.set()
                self.exit_code |= 1
                return

    async def ui_diff_group(  # noqa: C901
        self,
        title: str,
        diff: str,
        diff_prs: Sequence[FullPr],
    ) -> Literal["quit"] | None:
        def print_header() -> None:
            clear()
            print(title)
            print(" ".join(pr.pr.base.repo.full_name for pr in diff_prs))
            print_line()

        print_header()

        for pr in diff_prs:
            status, fail_example = pr.status
            if status != "success":
                print(f"Status check: {status}! Opening and skipping...")
                webbrowser.open(pr.pr.html_url)
                if fail_example is not None:
                    webbrowser.open(fail_example)
                self.exit_code |= 1
                return None

        print_diff(diff)
        print()

        while True:
            answer = await areadchar("(a)ccept/(o)pen/(s)kip/(q)uit ")
            print()
            if answer == "a":
                for pr in diff_prs:
                    # Already added to a task group, so no need to keep a reference here
                    _ = self.follow_tasks.create_task(self.merge(pr.pr))
                break
            if answer == "o":
                print("Opening random PR from diff group...")
                webbrowser.open(diff_prs[0].pr.html_url)
            elif answer == "s":
                break
            # elif answer == "c":
            #     done = False
            #     while True:
            #         print("(t)itlegroup/(d)iffgroup/(c)ancel ", end="")
            #         sys.stdout.flush()
            #         answer = readchar()
            #         print()
            #         if answer == "t":
            #             ret = CLOSE_TITLEGROUP
            #             answer = "d"
            #         if answer == "d":
            #             for pr in diff_prs:
            #                 print(f"Closing for {pr.base.repo.full_name}...")
            #                 pr.edit(state="closed")
            #         elif answer == "c":
            #             break
            #         else:
            #             print("Invalid answer")
            #
            #     if done:
            #         break
            elif answer == "q":
                return "quit"
            else:
                print("Invalid answer")

        clear()
        return None

    async def merge(self, pr: PullRequest) -> None:
        assert pr.base.repo.owner is not None  # noqa: S101
        if pr.user.login != self.login:
            await self.gh.rest.pulls.async_create_review(
                owner=pr.base.repo.owner.login,
                repo=pr.base.repo.name,
                pull_number=pr.number,
                event="APPROVE",
            )

        try:
            await self.gh.rest.pulls.async_merge(
                owner=pr.base.repo.owner.login,
                repo=pr.base.repo.name,
                pull_number=pr.number,
                merge_method="squash",
            )
        except RequestFailed as err:
            self.warnings.append(f"Failed to merge {pr.html_url}: {err}")
            return

        await self.delete_branch(pr)

    async def delete_branch(self, pr: PullRequest, *, force: bool = False) -> None:
        assert pr.base.repo.owner is not None  # noqa: S101
        assert pr.head.repo is not None  # noqa: S101
        assert pr.head.repo.owner is not None  # noqa: S101
        if not force:
            remaining_pulls = [  # type: ignore[var-annotated]
                x
                async for x in self.gh.rest.paginate(
                    self.gh.rest.pulls.async_list,
                    owner=pr.base.repo.owner.login,
                    repo=pr.base.repo.name,
                    head=f"{pr.head.repo.owner.login}:{pr.head.ref}",
                )
            ]
            if len(remaining_pulls) > 0:
                self.warnings.append(
                    f"Head branch of PR {pr.html_url} is referenced "
                    f"by open pull requests, didn't delete it"
                )
        await self.gh.rest.git.async_delete_ref(
            owner=pr.head.repo.owner.login,
            repo=pr.head.repo.name,
            ref=f"heads/{pr.head.ref}",
        )


def print_diff(diff: str) -> None:
    try:
        subprocess.run(["delta"], input=diff.encode(), check=True)  # noqa: S607
    except FileNotFoundError:
        print()
        print(diff)
