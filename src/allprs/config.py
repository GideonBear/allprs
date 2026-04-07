from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Literal, NoReturn


def error(s: str) -> NoReturn:
    print(f"ERROR: {s}")
    sys.exit(2)


path = Path.home() / ".allprs.json"
if path.exists():
    with path.open() as file:
        data = json.load(file)
else:
    data = {}


repo_query = data.pop("repo_query", "user:@me archived:false")
repo_query_extend = data.pop("repo_query_extend", None)
if repo_query_extend:
    repo_query += f" {repo_query_extend}"

pr_queries = data.pop(
    "pr_queries",
    [
        {"query": "author:app/pre-commit-ci"},
        {"query": "author:app/renovate"},
        {"query": "author:app/dependabot"},
        {"query": "author:@me", "head_branch_regex": "^all-repos_autofix_.*$"},
    ],
)
pr_queries.extend(data.pop("pr_queries_extend", ()))

type Action = Literal["accept", "close", "open", "skip", "quit"]
keybinds: dict[str, Action] = {
    "a": "accept",
    "c": "close",
    "o": "open",
    "s": "skip",
    "q": "quit",
}
orig_values = set(keybinds.values())
kb: str
val: str | None
for kb, val in data.pop("keybinds", {}).items():
    if val is None:
        keybinds.pop(kb)
    elif val not in orig_values:
        error(f"found unrecognized action for keybind '{kb}': '{val}'")
    else:
        keybinds[kb] = val


if data:
    error(f"found extra configuration key(s): {', '.join(data)}")
