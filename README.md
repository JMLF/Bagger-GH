# ml-repo-sampler

CLI to sample GitHub repositories that contain ML pipelines. It collects their URIs and metadata
using configurable search and filtering heuristics (see [`process.md`](process.md)). Every output
is a CSV file, and each run also writes a log.

## Documentation

| page | content |
|---|---|
| [docs/how-it-works.md](docs/how-it-works.md) | data flow, the 3 heuristic stages, each step in detail, resuming, rate limits, code map |
| [docs/configuration.md](docs/configuration.md) | every config key (`[run]`, `[github]`, `[search]`, `[sampling]`), token file, full CLI reference |
| [docs/heuristics.md](docs/heuristics.md) | every search and filtering heuristic with **all its settings**, available columns, recipes |
| [docs/extending.md](docs/extending.md) | adding queries, writing a new heuristic (plugin), collecting new fields, extra API calls |

## Setup

```bash
uv sync
cp secrets.example.toml secrets.toml     # then put your GitHub token(s) in it (git-ignored)
uv run ml-repo-sampler check             # validates config + tokens, shows rate limits
```

The token can also come from the environment: `export GITHUB_TOKEN=ghp_xxx`. Use commas to
give several tokens, which are used in rotation.

## Usage

```bash
uv run ml-repo-sampler list                       # queries, heuristics (on/off), available types
uv run ml-repo-sampler run                        # search -> enrich -> filter, new run dir
uv run ml-repo-sampler run -r runs/my-run         # resume an interrupted run
```

tqdm progress bars show the search (queries and pages), the enrichment (repos) and any
rate-limit waits. Log messages print above the bars. Turn them off with `--no-progress` or
`[run] progress = false`; they are also hidden automatically when output is not a terminal.

You can also run the steps one at a time:

```bash
uv run ml-repo-sampler search -r runs/my-run      # 1. code/repository search   -> hits.csv
uv run ml-repo-sampler enrich -r runs/my-run      # 2. GET /repos for pre-selected repos -> repos_metadata.csv
uv run ml-repo-sampler filter -r runs/my-run      # 3. offline: apply heuristics -> candidates.csv
```

### Selecting heuristics

- **In the config:** `enabled = true/false` on each `[[search.queries]]` and `[heuristics.*]` entry.
- **On the CLI, for one run:** `-q NAME` runs only the listed queries (repeatable).
  `-e NAME` / `-d NAME` enables or disables a heuristic or query.

`filter` makes no API calls. You can try different heuristic settings on the same run:

```bash
uv run ml-repo-sampler filter -r runs/my-run -d created_window -e language
```

If you loosen an aggregate-stage heuristic, some repos may not have metadata yet. Run `enrich`
again: it only fetches the missing repos. `enrich --all` fetches every repo that was found.

## Heuristics

**Search heuristics** (`[[search.queries]]`) have a `name`, a `library` label, a `query`, a
`type` (`code` or `repositories`) and optional `partitions`.

GitHub returns at most **1000 results per query**, and popular queries go far beyond that. In a
live test, `"from sklearn.pipeline import Pipeline"` reported 248k results. Partitions such as
`["size:<1000", "size:1000..5000", "size:>5000"]` run the query once per slice. `queries.csv`
records `total_count` and `capped` so you can see what was missed.

`auto_partition = true` does this for you. It splits the query on a `size:` range (code) or a
`created:` range (repositories), recursively, until every slice has at most 1000 results.
This collects every result, not just the first 1000. It costs about `total_count / 100`
search calls (see [docs/heuristics.md](docs/heuristics.md#automatic-partitioning-auto_partition)).

Legacy code search also needs at least one search term besides qualifiers. That is why the
defaults use `entry_points filename:MLproject` instead of `filename:MLproject` alone.

**Filtering heuristics** (`[heuristics.<name>]`, where `type` defaults to `<name>`) run at one of
three stages:

| type | stage | purpose (process.md) |
|---|---|---|
| `path_noise` | hit | n°1: path contains example/tutorial/test/docs… (whole-word match) |
| `multiplicity` | aggregate | n°2: ≥ N distinct files OR ≥ M distinct libraries |
| `exclude_forks` | aggregate | n°4: forks (known from search results, so no API call needed) |
| `min_stars` | repo | n°3: minimum stars |
| `min_commits` | repo | more than one commit (needs `[github] count_commits = true`) |
| `topics` | repo | n°5: negative topics/keywords; positive topics are reported |
| `created_window` / `pushed_window` | repo | n°6: study window / recent activity |
| `not_archived`, `language` | repo | extra |
| `field` | aggregate or repo | generic rule `field <op> value` on any output column |

- `action = "exclude"` drops the items that fail the heuristic.
- `action = "flag"` keeps them and only marks them in the `flagged_by` column.
- Only repos that pass the hit and aggregate heuristics are enriched, which saves API quota.

All settings of each heuristic are documented in [docs/heuristics.md](docs/heuristics.md).

To add a new heuristic, write a `Heuristic` subclass in a plugin file and list it in
`[run] plugins`. [`plugins/library_groups.py`](plugins/library_groups.py) is a working example,
and [docs/extending.md](docs/extending.md) explains how.

## Outputs (`runs/<name>-<timestamp>/`)

| file | content |
|---|---|
| `queries.csv` | one row per (query, partition): total_count, fetched, capped, status |
| `hits.csv` | raw search hits: query, library, repo, file path, URLs |
| `hits_evaluated.csv` | hits + hit-heuristic columns |
| `repos_aggregated.csv` | per repo: nb_hits, distinct files/libs, libraries, files, pre-selection |
| `repos_metadata.csv` | enrichment cache: stars, topics, fork/parent, dates, license, clone_url… |
| `repos_evaluated.csv` | every repo with all `h_<heuristic>` columns, `excluded_by`, `flagged_by`, `selected` |
| `candidates.csv` | selected repos only |
| `sample.csv` | random sample of candidates if `[sampling] size > 0` (seeded) |
| `funnel.csv` | how many hits/repos each heuristic removed, in order (for the paper) |
| `run.log` | full DEBUG log of every command run on this directory |
| `configs/` | copy of the config and the effective selection for each command |

Search resumes at the level of (query, partition), and enrichment resumes repo by repo.

## Development

```bash
uv run pytest
```
