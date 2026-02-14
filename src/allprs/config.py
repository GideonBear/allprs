from __future__ import annotations

import json
import sys
from pathlib import Path


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

if data:
    print(f"ERROR: found extra configuration key(s): {', '.join(data)}")
    sys.exit(2)
