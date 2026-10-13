"""The three steps of the collection process plus final filtering.

1. ``search``  : run the search heuristics (GitHub code/repository search) -> hits.csv
2. ``enrich``  : fetch repository metadata for repos passing hit/aggregate heuristics
                 -> repos_metadata.csv (incremental, acts as a cache)
3. ``evaluate``: apply all enabled heuristics offline -> hits_evaluated.csv,
                 repos_aggregated.csv, repos_evaluated.csv, candidates.csv, funnel.csv, sample.csv

Each step is resumable: completed queries and already-fetched repositories are skipped.
"""

from __future__ import annotations

import json
import math
import logging
import random
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import storage as st
from .config import Config, SearchQuery
from .github import GitHubClient, InvalidQueryError
from .heuristics import Heuristic, build_heuristics, by_stage, evaluate
from .progress import bar

log = logging.getLogger(__name__)

SEARCH_CAP = 1000  # GitHub never returns more than 1000 results per search query
# auto_partition splits above this total_count: GitHub's total_count is an estimate, and a slice
# reporting 876 results was seen returning the full 1000
SPLIT_THRESHOLD = 800
MAX_QUERY_LEN = 256
MAX_OPERATORS = 5


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_dir(cfg: Config) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return cfg.run.output_dir / f"{cfg.run.name}-{stamp}"


def record_config(cfg: Config, run_dir: Path, command: str) -> None:
    """Keep a copy of the config file and of the effective selection for reproducibility."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    cfg_dir = run_dir / "configs"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / f"{stamp}-{command}.toml").write_text(cfg.raw_text, encoding="utf-8")
    (cfg_dir / f"{stamp}-{command}.effective.json").write_text(
        json.dumps(cfg.to_dict(), indent=2, default=str), encoding="utf-8"
    )


# ------------------------------------------------------------------------------ search


def check_query(full_query: str) -> list[str]:
    problems = []
    if len(full_query) > MAX_QUERY_LEN:
        problems.append(f"query is {len(full_query)} chars (> {MAX_QUERY_LEN})")
    n_ops = len(re.findall(r"\b(AND|OR|NOT)\b", full_query))
    if n_ops > MAX_OPERATORS:
        problems.append(f"query uses {n_ops} AND/OR/NOT operators (> {MAX_OPERATORS})")
    return problems


def _hit_from_item(q: SearchQuery, partition: str, item: dict[str, Any]) -> dict[str, Any]:
    if q.type == "code":
        repo = item.get("repository", {})
        return {
            "query_name": q.name,
            "library": q.library,
            "query_type": q.type,
            "partition": partition,
            "full_name": repo.get("full_name"),
            "repo_html_url": repo.get("html_url"),
            "repo_is_fork": repo.get("fork"),
            "path": item.get("path"),
            "file_name": item.get("name"),
            "sha": item.get("sha"),
            "file_html_url": item.get("html_url"),
        }
    return {
        "query_name": q.name,
        "library": q.library,
        "query_type": q.type,
        "partition": partition,
        "full_name": item.get("full_name"),
        "repo_html_url": item.get("html_url"),
        "repo_is_fork": item.get("fork"),
    }


def run_query(client: GitHubClient, cfg: Config, q: SearchQuery, partition: str, full_query: str,
              label: str | None = None, split_if_capped: bool = False, max_items: int | None = None,
              fraction_for: Callable[[int], float] | None = None):
    """Run one (query, partition), following pagination. Returns (hits, stats).

    With ``split_if_capped``, stop after the first page if ``total_count`` exceeds
    SPLIT_THRESHOLD, and also mark the query as split if it reaches the cap anyway: the stats get
    ``status = "split"``, as the caller will split the range (duplicate hits are removed later).
    With ``max_items``, keep only that many items and stop paging (``status = "limited"``).
    With ``fraction_for`` (stratified sampling: total_count -> fraction to keep), the split and
    the number of items to keep follow ``stratified_plan`` (``status = "sampled"`` if partial).
    """
    endpoint = "/search/code" if q.type == "code" else "/search/repositories"
    per_page, max_pages = cfg.github.per_page, cfg.github.max_pages
    stats: dict[str, Any] = {
        "query_name": q.name, "library": q.library, "type": q.type, "partition": partition,
        "full_query": full_query, "status": "done", "total_count": 0, "incomplete_results": False,
        "pages_fetched": 0, "items_fetched": 0, "capped": False, "started_at": _now(), "error": "",
    }
    hits: list[dict[str, Any]] = []
    pages = bar(desc=f"  {label or q.name}", unit="page", total=max_pages, leave=False)
    page = 1
    while page <= max_pages:
        try:
            data = client.get(endpoint, {"q": full_query, "per_page": per_page, "page": page})
        except InvalidQueryError as e:
            stats.update(status="invalid", error=str(e))
            log.error("[%s] query rejected by GitHub: %s", q.name, e)
            break
        if data is None:
            break
        stats["pages_fetched"] += 1
        stats["total_count"] = data.get("total_count", 0)
        stats["incomplete_results"] |= bool(data.get("incomplete_results"))
        if page == 1 and fraction_for is not None:
            plan = stratified_plan(stats["total_count"], fraction_for(stats["total_count"]),
                                   per_page, split_if_capped)
            if plan == "split":
                # keep the probe page: a lower max_hits may later make this slice a stratum
                items = data.get("items", [])
                hits.extend(_hit_from_item(q, partition, it) for it in items)
                stats.update(status="split", items_fetched=len(items))
                break
            max_items = plan
        elif split_if_capped and stats["total_count"] > SPLIT_THRESHOLD:
            stats["status"] = "split"
            break
        items = data.get("items", [])
        if max_items is not None and stats["items_fetched"] + len(items) >= max_items:
            items = items[:max_items - stats["items_fetched"]]
            stats["status"] = "limited" if fraction_for is None else "sampled"
        hits.extend(_hit_from_item(q, partition, it) for it in items)
        stats["items_fetched"] += len(items)
        # Real number of pages is known once total_count is known (capped at 1000 results)
        pages.total = min(max_pages, max(page, math.ceil(min(stats["total_count"], SEARCH_CAP) / per_page)))
        pages.update(1)
        pages.set_postfix(hits=stats["items_fetched"], total=stats["total_count"])
        log.debug("[%s%s] page %d: %d items (total_count=%d)", q.name,
                  f" {partition}" if partition else "", page, len(items), stats["total_count"])
        # total_count is only an estimate (a slice reporting 136 can return 200 items), so keep
        # paging until a short page rather than stopping at total_count
        if stats["status"] != "done" or len(items) < per_page or stats["items_fetched"] >= SEARCH_CAP:
            break
        page += 1
    pages.close()
    reached_cap = stats["items_fetched"] >= SEARCH_CAP
    if split_if_capped and reached_cap and stats["status"] == "done":
        stats["status"] = "split"
    stats["capped"] = stats["status"] == "done" and (reached_cap or stats["total_count"] > stats["items_fetched"])
    stats["finished_at"] = _now()
    return hits, stats


def _search_job(client: GitHubClient, cfg: Config, run_dir: Path, q: SearchQuery, partition: str,
                full_query: str, label: str, split_if_capped: bool = False, max_items: int | None = None,
                fraction_for: Callable[[int], float] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run one (query, partition) and append its hits and stats to the run files."""
    for problem in check_query(full_query):
        log.warning("%s: %s", label, problem)
    log.info("%s: searching %s %r", label, q.type, full_query)
    hits, stats = run_query(client, cfg, q, partition, full_query, label, split_if_capped, max_items,
                            fraction_for)
    st.append_rows(run_dir / st.HITS_CSV, list(st.HIT_SCHEMA), hits)
    st.append_rows(run_dir / st.QUERIES_CSV, list(st.QUERY_SCHEMA), [stats])
    if stats["status"] == "split":
        log.info("%s: total_count=%d, %d fetched: splitting", label, stats["total_count"], stats["items_fetched"])
        return hits, stats
    log.info("%s: %d hits %s / total_count=%d%s", label, stats["items_fetched"],
             "sampled" if stats["status"] == "sampled" else "fetched", stats["total_count"],
             " (incomplete_results)" if stats["incomplete_results"] else "")
    if stats["capped"]:
        hint = ("the slice cannot be split further" if split_if_capped
                else "consider 'auto_partition = true' or 'partitions'")
        log.warning("%s: only %d of %d results are reachable (GitHub caps at %d); %s.",
                    label, stats["items_fetched"], stats["total_count"], SEARCH_CAP, hint)
    return hits, stats


def stratified_fraction(max_hits: int | None, root_total: int) -> float:
    """Share of the results a stratified query keeps: 1.0 (everything) if max_hits is not lower."""
    if not max_hits or root_total <= max_hits:
        return 1.0
    return max_hits / root_total


def stratified_plan(total: int, fraction: float, per_page: int, splittable: bool) -> str | int | None:
    """Decision for one slice of a stratified query: ``"split"``, or the number of items to keep
    (``None``: all of them).

    A slice is split while it is over the split threshold and its share (``fraction x total``)
    is more than one page, so every leaf takes its share from a range small enough to keep the
    sample spread over the whole range. With ``fraction = 1`` this is a full collection.
    """
    if splittable and total > SPLIT_THRESHOLD and (fraction >= 1 or fraction * total > per_page):
        return "split"
    return None if fraction >= 1 else round(fraction * total)


def _row_total(row: dict[str, Any]) -> int:
    # a slice that reached the cap holds more than its (estimated) total_count says
    return max(row["total_count"], row["items_fetched"])


def _leaf_satisfied(fetch: dict[str, Any] | None, target: int | None) -> bool:
    """Whether the last fetch of a slice holds the items its stratum needs (its first ones)."""
    if fetch is None:
        return False
    return fetch["status"] == "done" or (target is not None and fetch["items_fetched"] >= target)


def _latest_rows(run_dir: Path) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    """Last queries.csv row of every (query, partition), and its last row that fetched hits.

    Every fetch returns the first results of the slice and appends them to hits.csv, so the last
    fetch of a slice is its last ``items_fetched`` hits there.
    """
    latest, fetches = {}, {}
    for r in st.read_rows(run_dir / st.QUERIES_CSV, st.QUERY_SCHEMA):
        key = (r["query_name"], r["partition"] or "")
        latest[key] = r
        if r["status"] in ("done", "sampled") or (r["status"] != "invalid" and r["items_fetched"]):
            fetches[key] = r
    return latest, fetches


def stratified_leaves(q: SearchQuery, latest: dict[tuple[str, str], dict[str, Any]], fetches: dict[tuple[str, str], dict[str, Any]],
                      per_page: int) -> tuple[dict[str, int | None], int]:
    """Walk the slice tree of a stratified query from queries.csv, without calling the API.

    Returns ``{partition: items to keep (None: all)}`` for the leaves of the tree under the
    current ``max_hits``, and the number of slices still to search (0 when complete).
    """
    root = q.root_slice()
    root_row = latest.get((q.name, root.qualifier))
    if root_row is None:
        return {}, 1
    fraction = stratified_fraction(q.max_hits, _row_total(root_row))
    leaves: dict[str, int | None] = {}
    pending = 0
    stack = [root]
    while stack:
        piece = stack.pop()
        row = latest.get((q.name, piece.qualifier))
        if row is None:
            pending += 1
            continue
        if row["status"] == "invalid":
            continue
        halves = piece.split()
        plan = stratified_plan(_row_total(row), fraction, per_page, halves is not None)
        if plan == "split":
            stack.extend(reversed(halves))
            continue
        fetch = fetches.get((q.name, piece.qualifier))
        if not _leaf_satisfied(fetch, plan):
            pending += 1
        if fetch is not None:
            leaves[piece.qualifier] = plan
    return leaves, pending


def _search_stratified(client: GitHubClient, cfg: Config, run_dir: Path, q: SearchQuery, base_query: str,
                       latest: dict[tuple[str, str], dict[str, Any]], fetches: dict[tuple[str, str], dict[str, Any]], jobs) -> int:
    """Walk the slice tree of a stratified query, reusing queries.csv rows: slices are only
    fetched when they are new, or when a higher max_hits asks for more items than they hold."""
    per_page = cfg.github.per_page
    root = q.root_slice()
    root_row = latest.get((q.name, root.qualifier))
    fraction = stratified_fraction(q.max_hits, _row_total(root_row)) if root_row else None
    n_hits = 0
    stack = [root]
    while stack:
        piece = stack.pop()
        partition = piece.qualifier
        label = f"{q.name} [{partition}]"
        jobs.set_postfix_str(label)
        halves = piece.split()
        row = latest.get((q.name, partition))
        plan = None
        if row is not None and row["status"] != "invalid":
            plan = stratified_plan(_row_total(row), fraction, per_page, halves is not None)
        if row is not None and (row["status"] == "invalid"
                                or (plan != "split" and _leaf_satisfied(fetches.get((q.name, partition)), plan))):
            log.debug("%s: already done, skipping", label)
        elif plan != "split":
            if fraction is None:  # root probe: the fraction follows from its total_count
                def fraction_for(total: int) -> float:
                    return stratified_fraction(q.max_hits, total)
            else:
                def fraction_for(total: int, f: float = fraction) -> float:
                    return f
            _, stats = _search_job(client, cfg, run_dir, q, partition, f"{base_query} {partition}", label,
                                   split_if_capped=halves is not None, fraction_for=fraction_for)
            if fraction is None:
                fraction = stratified_fraction(q.max_hits, stats["total_count"])
                log.info("%s: stratified sampling keeps %.4g of total_count=%d (max_hits=%s)",
                         q.name, fraction, stats["total_count"], q.max_hits)
            if stats["status"] != "split":  # a split keeps its probe page, which is not in the sample
                n_hits += stats["items_fetched"]
            plan = stats["status"]
        if plan == "split":
            stack.extend(reversed(halves))
            jobs.total += len(halves)
            jobs.refresh()
        jobs.update(1)
    return n_hits


def _hit_key(hit: dict[str, Any]) -> tuple[str, str]:
    """Identity of a hit within a query, as used to remove duplicates during aggregation."""
    return hit["full_name"] or "", hit.get("path") or ""


def step_search(cfg: Config, run_dir: Path, client: GitHubClient) -> None:
    """Run every (query, partition) job. With ``auto_partition``, a job whose results exceed the
    cap is split on its qualifier range, recursively; ``split`` rows in queries.csv record the
    splits so that a resumed run walks the same tree without calling the API again.

    A query with ``max_hits`` stops once it has that many distinct hits, counting the hits
    already in the run directory. Its partially fetched slice is ``limited`` (not done), so
    raising ``max_hits`` later fetches it again.

    A ``stratified`` query instead takes from every slice a share proportional to its
    total_count (see ``stratified_plan``)."""
    latest, fetches = _latest_rows(run_dir)
    status = {k: r["status"] for k, r in latest.items() if r["status"] in ("done", "invalid", "split")}
    todo = [(q, p, fq) for q in cfg.enabled_queries() for p, fq in q.expanded()]
    limited = {q.name for q, _, _ in todo if q.max_hits and q.sampling == "ordered"}
    found: dict[str, set[tuple[str, str]]] = {name: set() for name in limited}
    if limited:
        for h in st.read_rows(run_dir / st.HITS_CSV, st.HIT_SCHEMA):
            if h["query_name"] in limited and h["full_name"]:
                found[h["query_name"]].add(_hit_key(h))
    log.info("Search step: %d enabled quer(ies), %d (query, partition) jobs%s",
             len(cfg.enabled_queries()), len(todo),
             ", some split automatically" if any(q.auto_partition for q, _, _ in todo) else "")

    jobs = bar(desc="Search", unit="query", total=len(todo))
    n_hits = 0
    for q, base_partition, base_query in todo:
        if q.sampling == "stratified":
            n_hits += _search_stratified(client, cfg, run_dir, q, base_query, latest, fetches, jobs)
            continue
        root = q.root_slice()
        stack = [root]  # [None] when auto partitioning is off
        while stack:
            if q.name in limited and len(found[q.name]) >= q.max_hits:
                log.info("%s: %d distinct hits, max_hits=%d reached; skipping the rest of the query",
                         q.name, len(found[q.name]), q.max_hits)
                jobs.update(len(stack))
                break
            piece = stack.pop()
            partition = f"{base_partition} {piece.qualifier}".strip() if piece else base_partition
            full_query = f"{base_query} {piece.qualifier}" if piece else base_query
            label = f"{q.name}{f' [{partition}]' if partition else ''}"
            jobs.set_postfix_str(label)
            state = status.get((q.name, partition))
            if state in ("done", "invalid"):
                log.debug("%s: already done, skipping", label)
            else:
                halves = piece.split() if piece else None
                if state is None:
                    # a slice that cannot be split (single value) fetches what it can reach
                    max_items = q.max_hits - len(found[q.name]) if q.name in limited else None
                    hits, stats = _search_job(client, cfg, run_dir, q, partition, full_query, label,
                                              split_if_capped=halves is not None, max_items=max_items)
                    if q.name in limited and stats["status"] != "split":
                        found[q.name].update(_hit_key(h) for h in hits if h["full_name"])
                    state = stats["status"]
                    n_hits += stats["items_fetched"]
                if state == "split":
                    stack.extend(reversed(halves))  # walk the range in increasing order
                    jobs.total += len(halves)
                    jobs.refresh()
            jobs.update(1)
    jobs.close()
    log.info("Search step done: %d new hits", n_hits)


# --------------------------------------------------------------------- aggregation


@dataclass
class Prepared:
    """Result of the offline hit + aggregate evaluation."""

    hits: list[dict[str, Any]]
    aggregated: list[dict[str, Any]]
    heuristics: list[Heuristic]
    hit_fail_counts: dict[str, int]


def aggregate_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group hits per repository. Hits with ``_excluded`` are counted but not used."""
    repos: dict[str, dict[str, Any]] = {}
    seen: set[tuple[str, str, str]] = set()
    for h in hits:
        key = (h["query_name"], h["full_name"], h.get("path") or "")
        if key in seen:  # same file found again (e.g. overlapping partitions or resumed query)
            continue
        seen.add(key)
        r = repos.setdefault(h["full_name"], {
            "full_name": h["full_name"], "html_url": h.get("repo_html_url"), "is_fork": h.get("repo_is_fork"),
            "nb_hits_total": 0, "nb_hits_excluded": 0, "nb_hits": 0,
            "libraries": set(), "queries": set(), "sources": set(), "files": set(),
        })
        r["nb_hits_total"] += 1
        if h.get("_excluded"):
            r["nb_hits_excluded"] += 1
            continue
        r["nb_hits"] += 1
        r["libraries"].add(h["library"])
        r["queries"].add(h["query_name"])
        r["sources"].add(h["query_type"])
        if h.get("path"):
            r["files"].add(h["path"])
    out = []
    for r in repos.values():
        r["nb_distinct_files"] = len(r["files"])
        r["nb_distinct_libs"] = len(r["libraries"])
        r["nb_queries"] = len(r["queries"])
        for k in ("libraries", "queries", "sources", "files"):
            r[k] = sorted(r[k])
        out.append(r)
    out.sort(key=lambda r: (-r["nb_hits"], r["full_name"]))
    return out


def _select_stratified(cfg: Config, run_dir: Path, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep, for stratified queries, only the hits of the leaves of the current slice tree: from
    the last fetch of each leaf, its first ``share`` items. Hits of slices that were split, or
    of fetches superseded by a later one, are dropped."""
    strat = [q for q in cfg.enabled_queries() if q.sampling == "stratified"]
    if not strat:
        return hits
    latest, fetches = _latest_rows(run_dir)
    keep: dict[str, dict[str, int | None]] = {}
    for q in strat:
        leaves, pending = stratified_leaves(q, latest, fetches, cfg.github.per_page)
        keep[q.name] = leaves
        if pending:
            log.warning("%s: stratified sample incomplete, %d slice(s) left to search (run search again)",
                        q.name, pending)
    by_slice: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for h in hits:
        if h["query_name"] in keep:
            by_slice.setdefault((h["query_name"], h["partition"] or ""), []).append(h)
    selected: dict[str, list[dict[str, Any]]] = {name: [] for name in keep}
    for (name, partition), group in by_slice.items():
        if partition not in keep[name]:
            continue
        n_last = fetches[(name, partition)]["items_fetched"]
        last_fetch = group[-n_last:] if n_last else []
        share = keep[name][partition]
        selected[name].extend(last_fetch if share is None else last_fetch[:share])
    out = [h for h in hits if h["query_name"] not in keep]
    for q in strat:
        n_all = sum(len(g) for (name, _), g in by_slice.items() if name == q.name)
        log.info("%s: stratified sample of %d hit(s) from %d slice(s) (%d other hit(s) not in the sample)",
                 q.name, len(selected[q.name]), len(keep[q.name]), n_all - len(selected[q.name]))
        out.extend(selected[q.name])
    return out


def prepare(cfg: Config, run_dir: Path) -> Prepared:
    """Load hits of enabled queries, apply hit heuristics, aggregate, apply aggregate heuristics."""
    heuristics = build_heuristics(cfg.heuristics, enabled_only=True)
    all_hits = st.read_rows(run_dir / st.HITS_CSV, st.HIT_SCHEMA)
    enabled = {q.name for q in cfg.enabled_queries()}
    hits = [h for h in all_hits if h["query_name"] in enabled and h["full_name"]]
    if len(hits) != len(all_hits):
        log.info("Ignoring %d hit(s) from queries not enabled in the current config", len(all_hits) - len(hits))
    hits = _select_stratified(cfg, run_dir, hits)

    hit_h = by_stage(heuristics, "hit")
    fail_counts = {h.name: 0 for h in hit_h}
    for hit in hits:
        cols, failed_excl, failed_flag = evaluate(hit_h, hit)
        hit.update(cols)
        hit["_excluded"] = bool(failed_excl)
        hit["excluded_by"] = failed_excl
        hit["flagged_by"] = failed_flag
        for name in failed_excl + failed_flag:
            fail_counts[name] += 1

    aggregated = aggregate_hits(hits)
    agg_h = by_stage(heuristics, "aggregate")
    for r in aggregated:
        cols, failed_excl, failed_flag = evaluate(agg_h, r)
        r.update(cols)
        if r["nb_hits"] == 0:
            failed_excl = ["all_hits_excluded", *failed_excl]
        r["pre_selected"] = not failed_excl
        r["excluded_by"] = failed_excl
        r["flagged_by"] = failed_flag
    return Prepared(hits, aggregated, heuristics, fail_counts)


# -------------------------------------------------------------------------- enrich


def extract_metadata(full_name: str, data: dict[str, Any] | None) -> dict[str, Any]:
    if data is None:
        return {"full_name": full_name, "metadata_status": "unavailable", "fetched_at": _now()}
    parent = data.get("parent") or {}
    return {
        "full_name": full_name,
        "metadata_status": "ok",
        "current_full_name": data.get("full_name"),
        "id": data.get("id"),
        "html_url": data.get("html_url"),
        "clone_url": data.get("clone_url"),
        "description": data.get("description"),
        "homepage": data.get("homepage"),
        "language": data.get("language"),
        "topics": data.get("topics") or [],
        "stars": data.get("stargazers_count"),
        "forks_count": data.get("forks_count"),
        "watchers": data.get("subscribers_count"),
        "open_issues": data.get("open_issues_count"),
        "size_kb": data.get("size"),
        "is_fork": data.get("fork"),
        "parent_full_name": parent.get("full_name"),
        "parent_stars": parent.get("stargazers_count"),
        "archived": data.get("archived"),
        "disabled": data.get("disabled"),
        "is_template": data.get("is_template"),
        "license": (data.get("license") or {}).get("spdx_id"),
        "default_branch": data.get("default_branch"),
        "owner_type": (data.get("owner") or {}).get("type"),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "pushed_at": data.get("pushed_at"),
        "fetched_at": _now(),
    }


def _backfill_commit_counts(client: GitHubClient, meta_path: Path, existing: list[dict[str, Any]],
                            targets: set[str]) -> None:
    """Fill commit_count for cached repos fetched before count_commits was turned on."""
    todo = [m for m in existing
            if m["full_name"] in targets and m["metadata_status"] == "ok" and m["commit_count"] is None]
    if not todo:
        return
    log.info("Counting commits of %d already cached repo(s)", len(todo))
    progress = bar(desc="Commits", unit="repo", total=len(todo))
    for i, m in enumerate(todo, 1):
        m["commit_count"] = client.count(f"/repos/{m['full_name']}/commits")
        progress.update(1)
        if i % 100 == 0 or i == len(todo):  # rewrite regularly so the step can be resumed
            st.write_rows(meta_path, list(st.METADATA_SCHEMA), existing)
    progress.close()


def step_enrich(cfg: Config, run_dir: Path, client: GitHubClient, enrich_all: bool = False,
                retry_unavailable: bool = False) -> None:
    prepared = prepare(cfg, run_dir)
    meta_path = run_dir / st.METADATA_CSV
    existing = st.read_rows(meta_path, st.METADATA_SCHEMA)
    if retry_unavailable:
        kept = [m for m in existing if m["metadata_status"] == "ok"]
        if len(kept) != len(existing):
            st.write_rows(meta_path, list(st.METADATA_SCHEMA), kept)
        existing = kept
    known = {m["full_name"] for m in existing}

    targets = [r["full_name"] for r in prepared.aggregated if enrich_all or r["pre_selected"]]
    todo = [name for name in targets if name not in known]
    log.info("Enrich step: %d repos found, %d to enrich (%s), %d already cached, %d to fetch",
             len(prepared.aggregated), len(targets), "all" if enrich_all else "passing hit/aggregate heuristics",
             len(targets) - len(todo), len(todo))

    count_commits = cfg.github.count_commits
    if not count_commits and any(h.type_name == "min_commits" for h in prepared.heuristics):
        log.warning("A min_commits heuristic is enabled but [github] count_commits = false: "
                    "commit_count stays empty and every repo will fail it")
    if count_commits:
        _backfill_commit_counts(client, meta_path, existing, set(targets))

    batch: list[dict[str, Any]] = []
    counts = {"ok": 0, "unavailable": 0}
    progress = bar(desc="Enrich", unit="repo", total=len(todo))
    for i, name in enumerate(todo, 1):
        meta = extract_metadata(name, client.get(f"/repos/{name}"))
        if count_commits and meta["metadata_status"] == "ok":
            meta["commit_count"] = client.count(f"/repos/{name}/commits")
        batch.append(meta)
        counts[meta["metadata_status"]] += 1
        progress.update(1)
        progress.set_postfix(counts)
        if len(batch) >= 25 or i == len(todo):  # flush regularly so the step can be resumed
            st.append_rows(meta_path, list(st.METADATA_SCHEMA), batch)
            batch = []
        if i % 500 == 0:
            log.debug("Enriched %d/%d repositories", i, len(todo))
    progress.close()
    log.info("Enrich step done: %d fetched (%d ok, %d unavailable)", len(todo), counts["ok"], counts["unavailable"])


# ------------------------------------------------------------------------ evaluate


def _funnel(prepared: Prepared, repos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def add(stage, step, action, unit, n_in, failed, remaining):
        rows.append({"order": len(rows) + 1, "stage": stage, "step": step, "action": action, "unit": unit,
                     "input": n_in, "failed": failed, "remaining": remaining})

    n_hits = len(prepared.hits)
    add("search", "hits_collected", "", "hits", n_hits, 0, n_hits)
    hit_h = by_stage(prepared.heuristics, "hit")
    for h in hit_h:  # hit heuristics are independent: "failed" counts each on all hits
        add("hit", h.name, h.action, "hits", n_hits, prepared.hit_fail_counts[h.name], "")
    hits_left = sum(not h["_excluded"] for h in prepared.hits)
    add("hit", "hits_kept", "", "hits", n_hits, n_hits - hits_left, hits_left)

    alive = list(repos)
    add("aggregate", "repos_found", "", "repos", len(alive), 0, len(alive))
    survivors = [r for r in alive if r["nb_hits"] > 0]
    add("aggregate", "all_hits_excluded", "exclude", "repos", len(alive), len(alive) - len(survivors), len(survivors))
    alive = survivors

    def sequential(stage, heuristics):
        nonlocal alive
        for h in heuristics:
            n_in = len(alive)
            failed = sum(r.get(h.column) is False for r in alive)
            if h.action == "exclude":
                alive = [r for r in alive if r.get(h.column) is not False]
            add(stage, h.name, h.action, "repos", n_in, failed, len(alive))

    sequential("aggregate", by_stage(prepared.heuristics, "aggregate"))
    with_meta = [r for r in alive if r.get("metadata_status") == "ok"]
    add("repo", "metadata_available", "exclude", "repos", len(alive), len(alive) - len(with_meta), len(with_meta))
    alive = with_meta
    sequential("repo", by_stage(prepared.heuristics, "repo"))
    add("final", "selected", "", "repos", len(alive), 0, len(alive))
    return rows


def step_evaluate(cfg: Config, run_dir: Path) -> dict[str, int]:
    prepared = prepare(cfg, run_dir)
    heuristics = prepared.heuristics
    hit_h, agg_h, repo_h = (by_stage(heuristics, s) for s in ("hit", "aggregate", "repo"))

    # hits_evaluated.csv
    def extras(hs: list[Heuristic]) -> list[str]:
        return [c for h in hs for c in h.extra_columns()]

    hit_cols = list(st.HIT_SCHEMA) + [h.column for h in hit_h] + extras(hit_h) + ["excluded_by", "flagged_by"]
    st.write_rows(run_dir / st.HITS_EVALUATED_CSV, hit_cols, prepared.hits)

    # repos_aggregated.csv
    agg_cols = (list(st.AGGREGATED_SCHEMA) + [h.column for h in agg_h] + extras(agg_h)
                + ["pre_selected", "excluded_by", "flagged_by"])
    st.write_rows(run_dir / st.AGGREGATED_CSV, agg_cols, prepared.aggregated)

    # join metadata and evaluate repo heuristics
    metadata = {m["full_name"]: m for m in st.read_rows(run_dir / st.METADATA_CSV, st.METADATA_SCHEMA)}
    repos = []
    missing = 0
    for agg in prepared.aggregated:
        r = dict(agg)
        meta = metadata.get(r["full_name"])
        excluded_by, flagged_by = list(agg["excluded_by"]), list(agg["flagged_by"])
        if meta is None:
            r["metadata_status"] = "not_fetched"
            missing += agg["pre_selected"]
        else:
            r.update({k: v for k, v in meta.items() if v is not None or k not in r})
        if r["metadata_status"] == "ok":
            cols, failed_excl, failed_flag = evaluate(repo_h, r)
            r.update(cols)
            excluded_by += failed_excl
            flagged_by += failed_flag
        elif agg["pre_selected"]:
            excluded_by.append(f"metadata_{r['metadata_status']}")
        r["excluded_by"], r["flagged_by"] = excluded_by, flagged_by
        r["selected"] = not excluded_by
        repos.append(r)
    if missing:
        log.warning("%d pre-selected repo(s) have no metadata yet: run the 'enrich' command", missing)

    meta_cols = [c for c in st.METADATA_SCHEMA if c not in st.AGGREGATED_SCHEMA]
    eval_cols = (["full_name", "selected", "excluded_by", "flagged_by"]
                 + [c for c in st.AGGREGATED_SCHEMA if c != "full_name"]
                 + meta_cols
                 + [h.column for h in agg_h + repo_h] + extras(agg_h + repo_h))
    st.write_rows(run_dir / st.EVALUATED_CSV, eval_cols, repos)

    candidates = [r for r in repos if r["selected"]]
    st.write_rows(run_dir / st.CANDIDATES_CSV, eval_cols, candidates)

    funnel = _funnel(prepared, repos)
    st.write_rows(run_dir / st.FUNNEL_CSV, ["order", "stage", "step", "action", "unit", "input", "failed", "remaining"],
                  funnel)
    for row in funnel:
        log.info("  funnel %-10s %-24s %-8s failed=%-6s remaining=%s %s", row["stage"], row["step"],
                 row["action"], row["failed"], row["remaining"], row["unit"])

    n_sample = 0
    if cfg.sampling.size > 0:
        rng = random.Random(cfg.sampling.seed)
        sample = rng.sample(candidates, min(cfg.sampling.size, len(candidates)))
        n_sample = st.write_rows(run_dir / st.SAMPLE_CSV, eval_cols, sample)
        if n_sample < cfg.sampling.size:
            log.warning("Sampling: requested %d but only %d candidates", cfg.sampling.size, n_sample)
        log.info("Sampled %d candidates (seed=%d) -> %s", n_sample, cfg.sampling.seed, st.SAMPLE_CSV)

    summary = {"hits": len(prepared.hits), "repos": len(repos),
               "pre_selected": sum(r["pre_selected"] for r in prepared.aggregated),
               "candidates": len(candidates), "sampled": n_sample}
    log.info("Evaluation done: %s -> %s", summary, run_dir / st.CANDIDATES_CSV)
    return summary
