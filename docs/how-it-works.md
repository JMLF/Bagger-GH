# How it works

This page explains the data flow, what each step reads and writes, and how resuming and rate
limits are handled. For the settings see [configuration.md](configuration.md). For the heuristics
see [heuristics.md](heuristics.md).

## Overview

```
                   config.toml
                        │
  ┌─────────────────────┼──────────────────────────────────────────────┐
  │ 1. search  (API)    ▼                                              │
  │   [[search.queries]] ──► GET /search/code | /search/repositories   │
  │                           (paginated, ≤ 1000 results per query)    │
  │                        ──► hits.csv, queries.csv                   │
  ├────────────────────────────────────────────────────────────────────┤
  │ 2. enrich  (API)                                                   │
  │   hits.csv ─► hit heuristics ─► aggregate per repo                 │
  │            ─► aggregate heuristics ─► "pre-selected" repos         │
  │            ─► GET /repos/{owner}/{name}  (only missing ones)       │
  │                        ──► repos_metadata.csv  (cache)             │
  ├────────────────────────────────────────────────────────────────────┤
  │ 3. filter  (offline, no API call)                                  │
  │   hits.csv + repos_metadata.csv                                    │
  │     ─► hit heuristics      ─► hits_evaluated.csv                   │
  │     ─► aggregate + agg. heuristics ─► repos_aggregated.csv         │
  │     ─► join metadata + repo heuristics ─► repos_evaluated.csv      │
  │                                        ─► candidates.csv           │
  │                                        ─► funnel.csv, sample.csv   │
  └────────────────────────────────────────────────────────────────────┘
```

`ml-repo-sampler run` runs the three steps in order. Each step is also a command of its own
(`search`, `enrich`, `filter`).

## The three stages of heuristics

Every filtering heuristic belongs to one **stage**. The stage decides what data the heuristic
sees and when it runs:

| stage | evaluated on | data available | when |
|---|---|---|---|
| `hit` | each search result (one file in one repo) | query name, library, repo name, file path, fork flag | before aggregation |
| `aggregate` | each repository, built from its kept hits | `nb_hits`, `nb_distinct_files`, `nb_distinct_libs`, `libraries`, `files`, `is_fork`… | before enrichment |
| `repo` | each pre-selected repository, after enrichment | everything above + stars, topics, dates, language, license… | after enrichment |

The order matters for the API quota. Only repos that pass **all** `exclude` heuristics of the
`hit` and `aggregate` stages are enriched, and enrichment costs one API call per repo. Filters
that need no metadata (such as forks and multiplicity) therefore run early.

### Exclude vs flag

Each heuristic has an `action`:

- `exclude` removes the item if the heuristic fails. The heuristic's name is added to the
  `excluded_by` column, and the repo gets `selected = false`.
- `flag` never removes anything. The heuristic's name is added to `flagged_by`. Use this to
  measure a heuristic's effect before you trust it, or to keep a signal for later manual review.

In every case each heuristic gets its own boolean column `h_<name>` (`true` means passed). You
can always see why a repo was kept or dropped.

## Step details

### 1. Search

For each enabled query and each of its partitions (see below), the tool calls the search API
and follows pagination:

- `per_page` (max 100) results per page, up to `max_pages` pages.
- It stops early when a page is not full, or when 1000 results have been fetched. It does not
  stop at `total_count`, which GitHub only estimates.

Each result becomes one row of `hits.csv`. For code search, a row is a *file* that matched,
with its path. For repository search, a row is a *repo* with an empty path. When a query
finishes, one row is appended to `queries.csv` with:

- `total_count`: how many results GitHub says exist;
- `items_fetched`: how many were actually downloaded;
- `capped`: true when some results could not be reached;
- `incomplete_results`: true when GitHub timed out on its side.

**The 1000-result cap.** GitHub returns at most 1000 results per search, whatever the
pagination. Popular queries (sklearn, torch) report hundreds of thousands of matches. A
`partition` is extra text appended to the query (usually a `size:` range). Each partition is a
separate search with its own 1000-result cap. The tool warns about each capped query in the log.
With `auto_partition`, the tool splits a capped query on a `size:` (or `created:`…) range,
recursively, until every slice is under the cap. Each slice is one row of `queries.csv`, and a
range that was split has `status = split`.

The search API also checks queries:

- A query may be at most 256 characters, with at most 5 `AND`/`OR`/`NOT` operators. `check`
  and `search` warn when a query breaks these limits.
- Code search needs at least one real search term besides qualifiers. `filename:dvc.yaml` alone
  is rejected (HTTP 422). The query is then recorded with `status = invalid` and the run
  continues.

### 2. Enrich

This step first does the same offline work as `filter` (hit heuristics, then aggregation, then
aggregate heuristics) to find the pre-selected repos. It then calls `GET /repos/{full_name}`
only for repos that are not yet in `repos_metadata.csv`.

- A repo that no longer exists or is blocked (404/410/451/403 "access blocked") is stored with
  `metadata_status = unavailable`. It will not be retried unless you pass `--retry-unavailable`.
- `--all` enriches every repo that was found, not only the pre-selected ones. This is useful
  before loosening aggregate heuristics.
- Rows are flushed every 25 repos, so an interrupted enrichment loses at most 25 calls.

### 3. Filter / evaluate

Everything in this step is recomputed from `hits.csv` and `repos_metadata.csv`:

1. Hits of queries that are **disabled in the current config** are ignored. Disabling a query
   therefore removes its contribution without deleting data.
2. Hit heuristics run on each hit. Hits that fail an `exclude` hit heuristic are counted in
   `nb_hits_excluded` but do not count toward `nb_hits`, the distinct files or the libraries.
3. Hits are grouped by repo. Duplicate `(query, repo, path)` triples are counted once, since
   they can come from overlapping partitions or a resumed query.
4. A repo left with 0 kept hits is excluded with the reason `all_hits_excluded`.
5. Aggregate heuristics run next. Repos without metadata are excluded with the reason
   `metadata_not_fetched` or `metadata_unavailable`.
6. Repo heuristics run last. `selected` is true when `excluded_by` is empty.

Because this step makes no API calls, you can change thresholds and rerun `filter` as often as
you like. Each run overwrites the evaluation CSVs in the run directory.

### The funnel

`funnel.csv` (also printed in the log) reports how many items each heuristic removed:

- Hit-stage rows count **hits**. Each hit heuristic is counted independently on all hits, then
  a `hits_kept` row gives the combined result.
- Aggregate- and repo-stage rows count **repos**. They are *sequential* in config order: each
  heuristic sees only the repos that survived the previous ones.
- `failed` is how many items failed the heuristic. `remaining` is how many are left after it;
  for a `flag` heuristic nothing is removed.

This is the table to report in a paper's methodology section.

## Run directory and resuming

A run directory is `<output_dir>/<run.name>-<YYYYmmdd-HHMMSS>/`, unless you pass `-r DIR`. Any
command pointed at an existing run directory **adds to it**:

- `search` skips `(query, partition)` pairs already marked `done` or `invalid` in `queries.csv`.
  With `auto_partition`, ranges marked `split` are split again without calling the API.
  A query interrupted halfway was never marked done, so it is rerun. The duplicate hits it leaves
  are removed during aggregation.
- A query with `max_hits` counts the hits it already has in `hits.csv`, so a resumed run stops
  it as soon as the limit is reached. The slice where it stopped is marked `limited`. That slice
  is not `done`, so raising `max_hits` later fetches it again and continues from there.
- A `stratified` query rebuilds its slice tree from `queries.csv` for the current `max_hits`.
  It only calls the API for new slices, or for slices that need more items than they hold.
  `filter` keeps only the hits of the current strata (see [sampling.md](sampling.md)).
- `enrich` skips repos already in `repos_metadata.csv`.
- `filter` rewrites all evaluation outputs.

You can grow a run over time. Add a query to the config (or enable one) and rerun `run -r DIR`:
only the new query is searched and only the new repos are enriched.

Every command also appends to `run.log`, which always holds DEBUG messages including each API
call. It saves the config file and the effective selection (after `-q/-e/-d`) under
`configs/<timestamp>-<command>.*`, so each output can be traced back to the settings that
produced it.

## Rate limits and errors

The GitHub client (`github.py`):

- **Rotates tokens** round-robin and tracks each token's remaining quota per resource
  (`core`, `search`, `code_search`) from the `X-RateLimit-*` headers.
- **Waits for the reset** when every token is exhausted for a resource. A countdown progress
  bar is shown during long waits.
- **Paces searches** with `search_min_interval` seconds between calls (code search allows 10
  calls per minute per user). Tokens from the *same account* share this limit, so extra tokens
  only help if they belong to different accounts.
- **Retries** secondary rate limits (`Retry-After`, or 60 s × attempt), 5xx errors and network
  errors with exponential backoff, up to `max_retries` times.
- **Removes a token** that gets `401 Bad credentials` and carries on with the others.

If a step still fails, the error is logged and the command exits with code 1. Rerun the same
command with `-r DIR` to continue where it stopped. Ctrl-C is handled the same way.

The cost of a full run is:

- search: about `queries × partitions × max_pages` calls, at 10 per minute (about 6.5 s each).
  With `auto_partition`, about `total_count / 100` calls per query, plus one per split;
- enrich: one call per pre-selected repo, 5000 per hour per account.

## Code map

| module | role |
|---|---|
| `cli.py` | Typer commands, logging setup, CLI overrides |
| `config.py` | TOML loading/validation, `SearchQuery`, `HeuristicConfig`, token loading |
| `github.py` | REST client: token rotation, rate limits, retries |
| `partitioning.py` | `Slice`: qualifier ranges and how they are split by `auto_partition` |
| `pipeline.py` | `step_search`, `step_enrich`, `prepare` (hit + aggregate stages), `step_evaluate`, funnel, sampling |
| `heuristics.py` | `Heuristic` base class, the registry and every built-in heuristic |
| `storage.py` | CSV schemas (column names and types), read/write/append helpers |
| `progress.py` | tqdm bars and the logging handler that prints above them |
| `templates/` | the default `config.toml` and `secrets.example.toml` used by `init` |
