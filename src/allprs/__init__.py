from __future__ import annotations

import operator
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
from allprs.utils import clear as clear, group_by, print_line


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from github.PullRequest import PullRequest


pr_queries: list[tuple[str, Callable[[str], bool]]] = [
    ("author:app/pre-commit-ci", lambda _b: True),
    ("author:app/renovate", lambda _b: True),
    ("head:pre-commit-autoupdate", lambda _b: True),
    (
        "author:@me",
        lambda b: b.startswith("all-repos_autofixer_"),
    ),
]


def main() -> None:
    token = (
        subprocess.run(["gh", "auth", "token"], check=True, stdout=subprocess.PIPE)  # noqa: S607
        .stdout.decode()
        .strip()
    )
    auth = Auth.Token(token)
    with Github(auth=auth) as g:
        for pr_query, head_check in pr_queries:
            clear()
            print(f"Querying {pr_query}...")
            all_prs: Iterator[PullRequest] = (
                pr.repository.get_pull(pr.number)
                for pr in g.search_issues(
                    f"is:pr state:open {config.repo_query} {pr_query}"
                )
            )
            all_prs = filter(lambda pr: head_check(pr.head), all_prs)  # type: ignore[arg-type]  # mypy bug

            title_groups = group_by(operator.attrgetter("title"), all_prs)
            for title, title_prs in title_groups.items():
                diff_groups = group_by(
                    partial(get_diff, token=token),
                    title_prs,
                )
                for diff, diff_prs in diff_groups.items():
                    process_diff_group(title, diff, diff_prs)


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

    while True:
        print("(a)ccept/(o)pen/(q)uit ", end="")
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
        elif answer in {"q", "\x03"}:
            sys.exit(3)
        else:
            print("Invalid answer")

    clear()


def check_status(pr: PullRequest) -> None:
    while True:
        state = list(pr.get_commits())[-1].get_combined_status().state
        if state == "success":
            return
        elif state == "pending":  # noqa: RET505
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
    return resp.text
