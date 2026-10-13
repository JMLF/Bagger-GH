"""CSV schemas and helpers. Every output of a run is a CSV file inside the run directory."""

from __future__ import annotations

import csv
from collections.abc import Iterable
from pathlib import Path
from typing import Any

LIST_SEP = ";"

# File names inside a run directory
QUERIES_CSV = "queries.csv"
HITS_CSV = "hits.csv"
HITS_EVALUATED_CSV = "hits_evaluated.csv"
AGGREGATED_CSV = "repos_aggregated.csv"
METADATA_CSV = "repos_metadata.csv"
EVALUATED_CSV = "repos_evaluated.csv"
CANDIDATES_CSV = "candidates.csv"
SAMPLE_CSV = "sample.csv"
FUNNEL_CSV = "funnel.csv"
LOG_FILE = "run.log"

QUERY_SCHEMA: dict[str, type] = {
    "query_name": str,
    "library": str,
    "type": str,
    "partition": str,
    "full_query": str,
    "status": str,
    "total_count": int,
    "incomplete_results": bool,
    "pages_fetched": int,
    "items_fetched": int,
    "capped": bool,
    "started_at": str,
    "finished_at": str,
    "error": str,
}

HIT_SCHEMA: dict[str, type] = {
    "query_name": str,
    "library": str,
    "query_type": str,
    "partition": str,
    "full_name": str,
    "repo_html_url": str,
    "repo_is_fork": bool,
    "path": str,
    "file_name": str,
    "sha": str,
    "file_html_url": str,
}

AGGREGATED_SCHEMA: dict[str, type] = {
    "full_name": str,
    "html_url": str,
    "is_fork": bool,
    "nb_hits_total": int,
    "nb_hits_excluded": int,
    "nb_hits": int,
    "nb_distinct_files": int,
    "nb_distinct_libs": int,
    "nb_queries": int,
    "libraries": list,
    "queries": list,
    "sources": list,
    "files": list,
}

METADATA_SCHEMA: dict[str, type] = {
    "full_name": str,
    "metadata_status": str,
    "current_full_name": str,
    "id": int,
    "html_url": str,
    "clone_url": str,
    "description": str,
    "homepage": str,
    "language": str,
    "topics": list,
    "stars": int,
    "forks_count": int,
    "watchers": int,
    "open_issues": int,
    "size_kb": int,
    "is_fork": bool,
    "parent_full_name": str,
    "parent_stars": int,
    "archived": bool,
    "disabled": bool,
    "is_template": bool,
    "license": str,
    "default_branch": str,
    "owner_type": str,
    "created_at": str,
    "updated_at": str,
    "pushed_at": str,
    "commit_count": int,
    "fetched_at": str,
}


def _to_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, set)):
        items = sorted(value) if isinstance(value, set) else value
        return LIST_SEP.join(str(v) for v in items)
    return str(value)


def _from_cell(value: str | None, typ: type) -> Any:
    if value is None or value == "":
        return [] if typ is list else None
    if typ is bool:
        return value.strip().lower() in ("true", "1", "yes")
    if typ is int:
        try:
            return int(float(value))
        except ValueError:
            return None
    if typ is list:
        return [v for v in value.split(LIST_SEP) if v]
    return value


def write_rows(path: Path, fields: list[str], rows: Iterable[dict[str, Any]]) -> int:
    n = 0
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: _to_cell(row.get(k)) for k in fields})
            n += 1
    return n


def append_rows(path: Path, fields: list[str], rows: Iterable[dict[str, Any]]) -> int:
    new = not path.exists() or path.stat().st_size == 0
    if not new:
        with path.open(newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), [])
        if header != fields:
            # Schema changed (e.g. a metadata field was added): rewrite the file with the new
            # header; old rows get empty values for the new columns.
            with path.open(newline="", encoding="utf-8") as f:
                old = list(csv.DictReader(f))
            with path.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                w.writeheader()
                w.writerows(old)
    n = 0
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        for row in rows:
            w.writerow({k: _to_cell(row.get(k)) for k in fields})
            n += 1
    return n


def read_rows(path: Path, schema: dict[str, type] | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if schema:
        rows = [{k: _from_cell(r.get(k), t) for k, t in schema.items()} for r in rows]
    return rows
