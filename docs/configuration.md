# Configuration and CLI reference

The configuration is a single TOML file (`config.toml` by default; choose another with `-c`).
Relative paths inside it are resolved from the directory of the config file. `ml-repo-sampler
init DIR` writes a fully commented default.

The heuristic sections (`[[search.queries]]` and `[heuristics.*]`) are documented in
[heuristics.md](heuristics.md).

## `[run]`

| key | default | meaning |
|---|---|---|
| `name` | `"ml-pipelines"` | prefix of new run directories: `<output_dir>/<name>-<timestamp>` |
| `output_dir` | `"runs"` | parent directory of run directories |
| `log_level` | `"INFO"` | console log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). `run.log` always records DEBUG |
| `progress` | `true` | show tqdm progress bars. They are hidden automatically when stderr is not a terminal |

## `[github]`

| key | default | meaning |
|---|---|---|
| `token_file` | none | TOML file holding the token(s), see below |
| `token_env` | `"GITHUB_TOKEN"` | environment variable checked **first**. Comma-separated values give several tokens |
| `api_url` | `"https://api.github.com"` | change for GitHub Enterprise |
| `per_page` | `100` | results per search page (1–100) |
| `max_pages` | `10` | pages per query. With 100 per page, 10 reaches the 1000-result cap. Lower it for quick tests |
| `search_min_interval` | `6.5` | minimum seconds between two search calls (code search allows 10/min) |
| `core_min_interval` | `0.0` | minimum seconds between two `/repos` calls |
| `timeout` | `30` | HTTP timeout in seconds |
| `max_retries` | `6` | retries for 408, 5xx and network errors |
| `secondary_strikes` | `3` | secondary rate limits per token (within `secondary_window`) before all tokens cool down. A secondary limit benches only the token that hit it, and the request moves on to the next token |
| `secondary_window` | `900` | seconds over which secondary rate limits are counted |
| `secondary_cooldown` | `3600` | seconds all tokens are benched when every token hit `secondary_strikes` secondary limits |
| `count_commits` | `false` | during `enrich`, one extra call per repo (`/repos/{name}/commits`) to store `commit_count`; needed by `min_commits`. Cached repos without it are filled on the next `enrich` |

### Token file

```toml
# secrets.toml (git-ignored)
[github]
token = "ghp_xxx"
# or several tokens, used in rotation:
tokens = ["ghp_first", "ghp_second"]
```

`token` and `tokens` can be combined. A classic token with no scopes, or a fine-grained token
with public read-only access, is enough: the tool only reads public data. Code search always
requires a token.

## `[search]`

| key | default | meaning |
|---|---|---|
| `default_partitions` | `[]` | partitions used by every query that does not set its own `partitions` |
| `auto_partition` | `false` | default `auto_partition` for every query that does not set its own (see [heuristics.md](heuristics.md#automatic-partitioning-auto_partition)) |

## `[sampling]`

| key | default | meaning |
|---|---|---|
| `size` | `0` | write `sample.csv` with a random sample of this many candidates (0 turns it off) |
| `seed` | `42` | random seed, so the same candidates always give the same sample |

Sampling runs at the end of every `filter` or `run`. If there are fewer candidates than `size`,
all of them are written and a warning is logged.

## CLI

```
ml-repo-sampler [COMMAND] [OPTIONS]
```

| command | API calls | what it does |
|---|---|---|
| `init [DIR] [--force]` | no | write `config.toml` and `secrets.example.toml` |
| `list` | no | show every query and heuristic with its on/off state and parameters, plus all heuristic types |
| `check` | `/rate_limit` only (free) | validate the config, warn about query limits, show each token's quota |
| `run` | yes | search → enrich → filter |
| `search` | yes | step 1 only |
| `enrich` | yes | step 2 only (needs `-r`) |
| `filter` | no | step 3 only (needs `-r`) |

Common options:

| option | commands | meaning |
|---|---|---|
| `-c, --config PATH` | all | config file (default `config.toml`) |
| `-r, --run-dir DIR` | run, search, enrich, filter | use or resume this run directory. Without it, `run` and `search` create a new one |
| `-q, --query NAME` | run, search, enrich, filter | run **only** these queries (repeatable). Overrides `enabled` |
| `-e, --enable NAME` | run, search, enrich, filter | enable a heuristic **or** a query (repeatable) |
| `-d, --disable NAME` | run, search, enrich, filter | disable a heuristic or a query (repeatable) |
| `-v, --verbose` | all | DEBUG logs on the console |
| `--no-progress` | run, search, enrich | no progress bars |
| `--enrich-all` / `--all` | run / enrich | enrich every repo that was found, not only the pre-selected ones |
| `--retry-unavailable` | enrich | fetch again the repos previously recorded as unavailable |

Overrides are applied in this order: `-q`, then `-e`, then `-d`. Unknown names are rejected
with an error, so a typo cannot silently change a run.

Exit codes: `0` success, `1` GitHub error or bad token in `check`, `2` configuration error,
`130` interrupted.

## Typical sessions

```bash
# quick trial: 2 queries, 1 page each
uv run ml-repo-sampler run -q mlflow_project -q dvc_pipeline   # after setting max_pages = 1

# full collection, resumable
uv run ml-repo-sampler run -r runs/main
# ...interrupted or rate limited? same command again:
uv run ml-repo-sampler run -r runs/main

# explore filters offline
uv run ml-repo-sampler filter -r runs/main -d created_window
uv run ml-repo-sampler filter -r runs/main -e language -e not_archived

# add a query to an existing run: add it to config.toml, then
uv run ml-repo-sampler run -r runs/main         # only the new query is searched
```

To compare configurations side by side, keep one config file per variant
(`config.strict.toml`, `config.loose.toml`). Copy the run directory before each `filter`,
because `filter` overwrites its outputs.
