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
                    process_diff_group(title, diff, diff_prs)
                    # if ret == CLOSE_TITLEGROUP:
                    #     for


# CLOSE_TITLEGROUP = object()


def process_diff_group(
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
        check_status(pr)

    print_header()

    try:
        subprocess.run(["delta"], input=diff.encode(), check=True)  # noqa: S607
    except FileNotFoundError:
        print()
        print(diff)
    print()

    ret = None

    while True:
        # print("(a)ccept/(o)pen/(s)kip/(c)lose/(q)uit ", end="")
        print("(a)ccept/(o)pen/(s)kip/(q)uit ", end="")
        sys.stdout.flush()
        answer = readchar()
        print()
        if answer == "a":
            for pr in diff_prs:
                print(f"Merging for {pr.base.repo.full_name}...")
                pr.create_review(event="APPROVE")
                pr.merge(merge_method="squash", delete_branch=True)

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
    return ret


def check_status(pr: PullRequest) -> None:
    while True:
        state = list(pr.get_commits())[-1].get_combined_status().state
        if state == "success":
            return
        elif state == "pending":  # noqa: RET505
            # TODO(GideonBear): When we have pre-commit lite  # noqa: FIX002, TD003
            #  running we can remove this, as we then always have status checks then
            if len(list(list(pr.get_commits())[-1].get_statuses())) == 0:
                return
            print(f"Status check: {state}. Sleeping 5s...")
            sleep(5)
        else:
            print(f"Status check: {state}! Opening...")
            webbrowser.open(pr.html_url)
            sys.exit(1)


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
