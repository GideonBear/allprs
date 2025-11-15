from __future__ import annotations

import operator
import re
import subprocess
import sys
import webbrowser
from functools import partial
from time import sleep
from typing import TYPE_CHECKING

import requests
from github import Auth, Github
from readchar import readchar

from allprs import config
from allprs.config import pr_queries
from allprs.utils import clear as clear, group_by, print_line


if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from github.PullRequest import PullRequest


def main() -> None:
    token = (
        subprocess.run(["gh", "auth", "token"], check=True, stdout=subprocess.PIPE)  # noqa: S607
        .stdout.decode()
        .strip()
    )
    auth = Auth.Token(token)
    with Github(auth=auth) as g:
        for pr_query_data in pr_queries:
            pr_query = pr_query_data["query"]
            clear()
            print(f"Querying {pr_query}...")
            all_prs: Iterator[PullRequest] = (
                pr.repository.get_pull(pr.number)
                for pr in g.search_issues(
                    f"is:pr state:open {config.repo_query} {pr_query}"
                )
            )
            if pr_query_data.get("head_branch_regex"):
                all_prs = filter(
                    lambda pr: re.match(
                        pr_query_data.get("head_branch_regex"), pr.head.ref
                    ),
                    all_prs,
                )

            title_groups = group_by(operator.attrgetter("title"), all_prs)
            for title, title_prs in title_groups.items():
                diff_groups = group_by(
                    partial(get_diff, token=token),
                    title_prs,
                )
                for diff, diff_prs in diff_groups.items():
                    process_diff_group(g, title, diff, diff_prs)
                    # if ret == CLOSE_TITLEGROUP:
                    #     for


# CLOSE_TITLEGROUP = object()


def process_diff_group(
    g: Github,
    title: str,
    diff: str,
    diff_prs: Sequence[PullRequest],
) -> None:
    def print_header() -> None:
        clear()
        print(title)
        print(" ".join(pr.base.repo.full_name for pr in diff_prs))
        print_line()

    print_header()

    print("Waiting for status checks...")
    for pr in diff_prs:
        if check_status(pr):
            return

    print_header()

    print_diff(diff)
    print()

    while True:
        # print("(a)ccept/(o)pen/(s)kip/(c)lose/(q)uit ", end="")
        print("(a)ccept/(o)pen/(s)kip/(q)uit ", end="")
        sys.stdout.flush()
        answer = readchar()
        print()
        if answer == "a":
            for pr in diff_prs:
                merge(pr, g)
            break
        elif answer == "o":  # noqa: RET508
            print("Opening random PR from diff group...")
            webbrowser.open(diff_prs[0].html_url)
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
        elif answer in {"q", "\x03"}:
            sys.exit(3)
        else:
            print("Invalid answer")

    clear()


def check_status(pr: PullRequest) -> bool:
    """
    Check the status of a pr.

    Returns:
        true if the PR should be skipped

    """
    while True:
        state = get_status(pr)
        if state == "success":
            return False
        elif state == "pending":  # noqa: RET505
            if len(list(list(pr.get_commits())[-1].get_statuses())) == 0:
                return False
            print(f"Status check: {state}. Sleeping 5s...")
            sleep(5)
        else:
            print(f"Status check: {state}! Opening and skipping...")
            webbrowser.open(pr.html_url)
            return True


def get_status(pr: PullRequest) -> str:
    commit = list(pr.get_commits())[-1]
    status_state = commit.get_combined_status().state
    check_run_state = "success"
    for check_run in commit.get_check_runs():
        conclusion: str | None = check_run.conclusion
        if conclusion == "success":
            pass
        elif conclusion is None and check_run_state in {"success", "pending"}:
            check_run_state = "pending"
        elif conclusion == "failure":
            check_run_state = "failure"
        else:
            msg = f"Unexpected check run conclusion: {conclusion}"
            raise Exception(msg)  # noqa: TRY002

    if status_state == "failure" or check_run_state == "failure":
        state = "failure"
    elif status_state == "pending" or check_run_state == "pending":
        state = "pending"
    elif status_state == "success" and check_run_state == "success":
        state = "success"
    else:
        msg = (
            f"Unexpected check run conclusion or status state: "
            f"{check_run_state}, {status_state}"
        )
        raise Exception(msg)  # noqa: TRY002

    return state


def get_diff(pr: PullRequest, token: str) -> str:
    resp = requests.get(
        pr.url,
        timeout=30,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.diff",
        },
    )
    resp.raise_for_status()
    return fix_diff(resp.text)


def fix_diff(diff: str) -> str:
    return "\n".join(line for line in diff.split("\n") if not line.startswith("index"))


def print_diff(diff: str) -> None:
    try:
        subprocess.run(["delta"], input=diff.encode(), check=True)  # noqa: S607
    except FileNotFoundError:
        print()
        print(diff)


def merge(pr: PullRequest, g: Github) -> None:
    print(f"Merging for {pr.base.repo.full_name}...")
    if pr.user.login == g.get_user().login:
        print("Skipping approval as you authored that PR")
    else:
        pr.create_review(event="APPROVE")
    pr.merge(merge_method="squash", delete_branch=True)
