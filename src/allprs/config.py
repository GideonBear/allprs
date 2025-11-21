from __future__ import annotations

import json
from pathlib import Path


path = Path.home() / ".allprs.json"
if path.exists():
    with path.open() as file:
        data = json.load(file)
else:
    data = {}

repo_query = data.get("repo_query", "user:@me archived:false")
repo_query_extend = data.get("repo_query_extend")
if repo_query_extend:
    repo_query += f" {repo_query_extend}"

pr_queries = data.get(
    "pr_queries",
    [
        {"query": "author:app/pre-commit-ci"},
        {"query": "author:app/renovate"},
        {"query": "author:app/dependabot"},
        {"query": "author:@me", "head_branch_regex": "^all-repos_autofix_.*$"},
    ],
)
pr_queries_extend = data.get("pr_queries_extend")
if pr_queries_extend:
    pr_queries.extend(pr_queries_extend)
