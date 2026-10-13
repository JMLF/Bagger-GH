# Heuristics reference

There are two kinds of heuristics:

- **Search heuristics** (`[[search.queries]]`) decide which repos are *found*.
- **Filtering heuristics** (`[heuristics.<name>]`) decide which of the found repos are *kept*.

Run `uv run ml-repo-sampler list` to see what is configured and enabled, with the effective
parameters (defaults included).

- [Search heuristics](#search-heuristics)
- [Filtering heuristics: common keys](#filtering-heuristics-common-keys)
- [`path_noise`](#path_noise-stage-hit) · [`multiplicity`](#multiplicity-stage-aggregate) ·
  [`exclude_forks`](#exclude_forks-stage-aggregate) · [`min_stars`](#min_stars-stage-repo) ·
  [`min_commits`](#min_commits-stage-repo) ·
  [`topics`](#topics-stage-repo) · [`created_window` / `pushed_window`](#created_window--pushed_window-stage-repo) ·
  [`not_archived`](#not_archived-stage-repo) · [`language`](#language-stage-repo) ·
  [`field`](#field-stage-aggregate-or-repo)
- [Available columns](#available-columns)
- [Recipes](#recipes)

---

## Search heuristics

Each `[[search.queries]]` block is one search:

```toml
[[search.queries]]
name = "sklearn_pipeline"          # required, unique; used in CSVs and with -q/-e/-d
library = "scikit-learn"           # label counted by multiplicity (default: name)
query = '"from sklearn.pipeline import Pipeline" language:Python'   # required
type = "code"                      # "code" (default) or "repositories"
enabled = true                     # default true
partitions = ["size:<1000", "size:1000..5000", "size:>5000"]        # optional
auto_partition = true              # optional: split automatically until every slice is under the cap
auto_partition_range = [0, 1000000]  # optional: range split by auto_partition
max_hits = 20000                   # optional: stop the query after this many distinct hits
sampling = "stratified"            # optional: how max_hits is reached ("ordered" or "stratified")
```

| key | default | meaning |
|---|---|---|
| `name` | required | unique identifier |
| `query` | required | GitHub search syntax. Use TOML single quotes `'...'` so double quotes stay inside |
| `library` | = `name` | what the query detects. Several queries can share one library (e.g. `import xgboost` and `from xgboost import`); `nb_distinct_libs` counts libraries, not queries |
| `type` | `"code"` | `code` → `GET /search/code`: each result is a file. `repositories` → `GET /search/repositories`: each result is a repo |
| `enabled` | `true` | can be overridden with `-q`, `-e`, `-d` |
| `partitions` | `[search] default_partitions` | each string is appended to `query` and run as a separate search. `[]` means no partitioning even if defaults exist |
| `auto_partition` | `[search] auto_partition` (`false`) | `true` or a qualifier name: split the query on that qualifier until every slice has at most 1000 results (see below). `true` means `size` for code and `created` for repositories |
| `auto_partition_range` | see below | `[low, high]` range split by `auto_partition`. Numbers, or dates for `created`/`pushed` |
| `max_hits` | `[search] max_hits` (none) | target number of distinct hits (repo + path) for the query. How it is reached depends on `sampling` |
| `sampling` | `[search] sampling` (`"ordered"`) | `"ordered"`: walk the slices in increasing order of the partition qualifier and stop at `max_hits`, so a limited query keeps the **lowest** part of the range (e.g. the smallest files). `"stratified"`: take from every slice a share proportional to its size, spread over the whole range (see below). Needs `auto_partition` and no static `partitions` |

### Writing code-search queries

- Supported qualifiers: `language:`, `path:`, `filename:`, `extension:`, `repo:`, `user:`,
  `org:`, `size:` (file size in bytes), `fork:`, `in:file`, `in:path`.
- `stars:`, `created:` and `topic:` are **not** available in code search. Use repo-stage
  heuristics for those, or a `repositories` query.
- There must be at least one search term besides qualifiers: `stages filename:dvc.yaml`
  works, `filename:dvc.yaml` alone does not.
- `"..."` searches for an exact phrase. Code search ignores most punctuation, so
  `"tf.keras.Sequential"` also matches `tf keras Sequential`.
- Only the default branch is indexed, and very large files are skipped (see "Limites connues"
  in `process.md`). Forks are indexed only if they have more stars than their parent.
- Limits: 256 characters and 5 `AND`/`OR`/`NOT` operators. `check` warns when you exceed them.

### Writing repository-search queries

These use the repository qualifiers: `topic:`, `stars:`, `created:`, `pushed:`, `language:`,
`size:`, `archived:`, `in:name,description,readme`. Example:

```toml
[[search.queries]]
name = "topic_mlops"
library = "mlops"
type = "repositories"
query = 'topic:mlops stars:>=2 created:>2022-12-31'
```

A repo found this way has no files. `multiplicity` lets it through by default
(`exempt_repository_search = true`).

### Partitions: getting past the 1000-result cap

For each partition, GitHub returns up to 1000 results. Split along something that divides the
result set roughly evenly:

```toml
partitions = ["size:<500", "size:500..2000", "size:2000..8000", "size:>8000"]   # file size (code)
partitions = ["created:<2024-01-01", "created:>=2024-01-01"]                  # repositories only
partitions = ["path:src", "path:pipelines", "path:training"]                  # by location (code)
```

Check `queries.csv` afterwards: a partition with `capped = true` should be split further.

#### Automatic partitioning (`auto_partition`)

Manual partitions are guesswork. `auto_partition = true` finds them for you:

1. The query runs on the full range, e.g. `size:0..1000000`. Page 1 gives `total_count`.
2. If it is above 800, the range is split in two, and each half runs the same way.
   Nothing else is fetched for that range (`status = split` in `queries.csv`).
3. Otherwise its pages are fetched as usual (`status = done`).

`total_count` is only an estimate: a slice reporting 876 results returned the full 1000. This is
why the threshold is 800, not 1000. A slice that still returns 1000 results is split as well.
Its hits are kept, and the duplicates it shares with its halves are removed during aggregation.

Every result is reachable this way. For example, `"from sklearn.pipeline import Pipeline"`
reports 248k files, and a single byte size (`size:3000..3000`) matches only about 25 of them.
A slice that is still over the cap but cannot be split (a single byte, star or day) is fetched
up to 1000 results and recorded with `capped = true`.

| `auto_partition` | search type | default `auto_partition_range` | split point |
|---|---|---|---|
| `size` | code (file bytes) | `[0, 1000000]` (files over 384 KB are not indexed anyway) | geometric midpoint |
| `size` | repositories (KB) | `[0, 100000000]` | geometric midpoint |
| `stars`, `forks` | repositories | `[0, 10000000]` | geometric midpoint |
| `created`, `pushed` | repositories | `[2008-01-01, today]` | middle day |

Numeric ranges are split at the geometric midpoint because sizes and stars are heavy-tailed.
The query and its `partitions` must not already use the same qualifier. Use
`auto_partition_range` to bound it instead, e.g. `auto_partition_range = [2023-01-01, 2026-06-30]`
for `created`. A query set this way is a config error; a query that only inherits
`[search] auto_partition = true` is left unsplit. Static `partitions` still work: each one is
split independently.

#### Stratified sampling (`sampling = "stratified"`)

With `max_hits` and `sampling = "stratified"`, the query keeps the fraction
`f = max_hits / total_count` of its results, spread over the whole partition range:

1. The first call on the full range gives `total_count`, and so `f`.
2. A slice is split while it has more than 800 results **and** its share `f x total_count` is
   more than one page (`per_page`). This makes strata of about `per_page / f` results.
3. A slice that is not split is a stratum: its first page (already fetched to read its
   `total_count`) gives its first `round(f x total_count)` results.

With 1.6M results and `max_hits = 20000`, this is about 200 strata of 8000 files with 100 hits each,
for about 400 calls instead of about 16000 for a full collection. If `max_hits` is at least
`total_count`, or is not set, `f = 1` and the query collects **everything**, like `auto_partition`
alone. `max_hits` can be changed and the run resumed (`-r DIR`). A higher value needs `search`: it
splits strata further and refetches the ones that need more items. A lower value needs only
`filter`, since the first page of every split slice is kept. `queries.csv` keeps one `sampled`
row per partially taken stratum. `filter` uses only the hits of the current strata.

With `sampling = "ordered"`, `max_hits` only limits the search: `filter` uses every hit already
fetched, so lowering `max_hits` does not shrink the data. See [sampling.md](sampling.md) for the limitations to report.

**Cost.** Collecting everything costs about `total_count / 100` page calls plus a probe per
split, at 10 code-search calls per minute and per token. 248k results take about 3000 calls,
or about 5.5 hours with one token. Add tokens from other accounts to go faster. The search
resumes slice by slice (`-r DIR`), and the splits already made are not probed again.

---

## Filtering heuristics: common keys

```toml
[heuristics.<name>]     # <name> is free; it becomes the column h_<name>
type = "<type>"         # optional: defaults to <name>
enabled = true          # default true
action = "exclude"      # "exclude" (default) or "flag"
# ...type-specific parameters
```

- Because `<name>` and `type` are separate, you can use the **same type several times** with
  different settings:

  ```toml
  [heuristics.min_stars]           # type = "min_stars"
  min_stars = 2

  [heuristics.popular]             # a second, flag-only threshold
  type = "min_stars"
  action = "flag"
  min_stars = 50
  ```

- Unknown types and unknown parameters are rejected when the config loads, and the error lists
  what is allowed.
- Repo- and aggregate-stage heuristics run **in the order they appear in the file**. This only
  changes how `funnel.csv` attributes removals; the final selection is the same.
- A heuristic that is not listed in the config does not run. Defaults apply only to parameters
  you leave out of a section you did write.

---

## `path_noise` (stage: hit)

Rejects search hits whose **file path** looks like an example, tutorial, test or documentation
(noise heuristic n°1). A hit with an empty path (from repository search) always passes.

| parameter | default | meaning |
|---|---|---|
| `patterns` | `["example", "examples", "tutorial", "tutorials", "demo", "demos", "sample", "samples", "test", "tests", "docs"]` | words that mark noise (case-insensitive) |
| `match` | `"token"` | `"token"`: the path is split into words on every non-alphanumeric character (`/ _ - .`) and a pattern must equal a whole word. `"substring"`: a pattern anywhere in the path |

| path | `token` | `substring` |
|---|---|---|
| `tests/test_model.py` | noise | noise |
| `src/latest/train.py` | kept | **noise** (`test` in `latest`) |
| `notebooks/Demo.py` | noise | noise |
| `examples_v2/run.py` | noise | noise |
| `mytests/run.py` | kept | noise |

With `action = "exclude"`, a noisy hit is dropped **before aggregation**: it does not count
toward `nb_hits`, the distinct files or the libraries. A repo whose hits are all noisy is removed
(`all_hits_excluded`). With `action = "flag"`, noisy hits still count and are only marked in
`hits_evaluated.csv`.

Tip: add `"notebook"`, `"notebooks"`, `"homework"`, `"exercise"`, `"exercises"`,
`"assignment"`, `"lab"`, `"benchmark"` or `"scripts"` depending on what you want to study.

## `multiplicity` (stage: aggregate)

A real pipeline rarely shows up as a single isolated hit (noise heuristic n°2). The heuristic
passes when:

```
nb_hits >= min_hits  AND  ( nb_distinct_files >= min_distinct_files  OR/AND  nb_distinct_libs >= min_distinct_libs )
```

| parameter | default | meaning |
|---|---|---|
| `min_distinct_files` | `2` | distinct file paths among kept hits |
| `min_distinct_libs` | `2` | distinct `library` labels among kept hits |
| `min_hits` | `1` | kept hits (distinct `(query, file)` pairs). Always required, whatever `combine` is |
| `combine` | `"any"` | `"any"`: files **or** libs. `"all"`: files **and** libs |
| `exempt_repository_search` | `true` | repos found by a `repositories` query pass automatically |

To require only one criterion, set the other one to something impossible to meet (e.g.
`min_distinct_libs = 999` with `combine = "any"` means "≥ 2 files").

Note: multiplicity counts hits **of the enabled queries**. The more queries you enable, the
more repos reach 2 libraries.

## `exclude_forks` (stage: aggregate)

Rejects repos that are forks (noise heuristic n°4). The fork flag comes with the search results,
so forks are dropped **before** enrichment and cost no API call. No parameters.

Code search only indexes forks that have more stars than their parent, so few forks show up
anyway. To study forks, disable this heuristic. `parent_full_name` and `parent_stars` in
`repos_evaluated.csv` let you compare with the parent.

## `min_stars` (stage: repo)

| parameter | default | meaning |
|---|---|---|
| `min_stars` | `2` | minimum `stargazers_count`, inclusive |

Noise heuristic n°3: removes most personal and toy repos.

## `min_commits` (stage: repo)

| parameter | default | meaning |
|---|---|---|
| `min_commits` | `2` | minimum number of commits on the default branch, inclusive |

Removes repos pushed in a single commit (one-shot uploads, copies of a project). The count comes
from `commit_count`, which is only collected when `[github] count_commits = true`: one extra
`/repos/{name}/commits?per_page=1` call per enriched repo (the total is read from the `Link`
header). An empty repository counts 0 commits. If `commit_count` is empty, the repo fails.

## `topics` (stage: repo)

Noise heuristic n°5: tutorial or course repos are rejected, and ML topics are recorded as a
positive signal.

| parameter | default | meaning |
|---|---|---|
| `negative_topics` | `["tutorial", "tutorials", "course", "courses", "learning", "bootcamp"]` | a repo **topic equal** to one of these fails |
| `negative_keywords` | `["tutorial", "tutorials", "course", "courses", "bootcamp"]` | a **whole word** of the `keyword_fields` equal to one of these fails |
| `keyword_fields` | `["name", "description"]` | where to look for keywords: any of `name` (repo name only), `full_name` (owner/name), `description`, `homepage` |
| `positive_topics` | `["machine-learning", "mlops", "scikit-learn", "deep-learning", "pytorch", "tensorflow", "ml-pipeline", "data-science"]` | recorded in the column `<name>_positive` |
| `require_positive` | `false` | if `true`, the repo also needs at least one positive topic |

The matches are written to two extra columns, `topics_negative` (e.g.
`topic:course;keyword:tutorial`) and `topics_positive`.

Why exact topics and whole words? The example in `process.md` searched for the substring
`"learning"`, which also rejects `machine-learning` and `deep-learning`, the very repos we want.
Here the topic `learning` is rejected but `machine-learning` is not. In a name or description,
`ml-course-2024` contains the word `course`, while `coursework` does not.

## `created_window` / `pushed_window` (stage: repo)

Noise heuristic n°6. `created_window` checks `created_at`; `pushed_window` checks `pushed_at`
(the last push, to drop abandoned repos).

| parameter | default | meaning |
|---|---|---|
| `after` | none | the date must be strictly **after** this |
| `before` | none | the date must be strictly **before** this |

At least one of the two is required. Accepted values: TOML dates (`after = 2022-12-31`),
strings (`"2022-12-31"`) or full timestamps (`"2022-12-31T23:59:59Z"`). A date alone means
midnight UTC, so `after = 2022-12-31` still accepts a repo created on 2022-12-31 at 10:00.
Write `after = 2023-01-01` if you mean "created in 2023 or later". A repo with no date fails.

The default config uses `pushed_window` under the name `recent_activity`, as a `flag`:

```toml
[heuristics.recent_activity]
type = "pushed_window"
action = "flag"
after = 2025-01-01
```

## `not_archived` (stage: repo)

Rejects archived or disabled repos. No parameters. It is off by default: an archived repo is
still a valid, finished ML project.

## `language` (stage: repo)

| parameter | default | meaning |
|---|---|---|
| `languages` | `["Python", "Jupyter Notebook"]` | GitHub's primary `language` must be one of these (case-insensitive) |

Caution: GitHub sets the primary language by bytes of code. A Python pipeline with large
notebooks shows up as `Jupyter Notebook`, and some are `HTML` or `null`.

## `field` (stage: aggregate or repo)

A generic rule, `record[field] <op> value`, on any column (see [Available columns](#available-columns)).
It covers most new ideas without writing code.

| parameter | default | meaning |
|---|---|---|
| `field` | required | column name |
| `op` | `"=="` | one of the operators below |
| `value` | none | value to compare to (number, string, bool, date, list) |
| `stage` | `"repo"` | `"aggregate"` (runs before enrichment, so it can only use aggregate columns) or `"repo"` |

| `op` | passes when |
|---|---|
| `==`, `!=`, `>`, `>=`, `<`, `<=` | comparison. When `value` is a number, the field is converted to a number. When `value` is a TOML date, both sides are compared as dates. An empty field fails |
| `in`, `not_in` | the field is (not) one of the items of the list `value` |
| `contains`, `not_contains` | for a list column (`topics`, `libraries`, `files`, …): `value` is (not) an item of it. For text: `value` is (not) a substring |
| `regex` | Python `re.search(value, field)` finds a match (use `(?i)` for case-insensitive) |

Examples:

```toml
[heuristics.min_size]            # repo is at least 50 KB
type = "field"
field = "size_kb"
op = ">="
value = 50

[heuristics.permissive_license]  # flag repos without an OSI license
type = "field"
action = "flag"
field = "license"
op = "in"
value = ["MIT", "Apache-2.0", "BSD-3-Clause", "BSD-2-Clause", "GPL-3.0", "MPL-2.0"]

[heuristics.has_sklearn]         # before enrichment: must contain a sklearn hit
type = "field"
stage = "aggregate"
field = "libraries"
op = "contains"
value = "scikit-learn"

[heuristics.no_awesome_lists]
type = "field"
field = "full_name"
op = "regex"
value = "(?i)^(?!.*awesome).*$"

[heuristics.org_owned]
type = "field"
action = "flag"
field = "owner_type"
op = "=="
value = "Organization"
```

---

## Available columns

**Hit stage** (`hits.csv`): `query_name`, `library`, `query_type`, `partition`, `full_name`,
`repo_html_url`, `repo_is_fork`, `path`, `file_name`, `sha`, `file_html_url`.

**Aggregate stage** (`repos_aggregated.csv`):

| column | type | meaning |
|---|---|---|
| `full_name`, `html_url` | text | repo |
| `is_fork` | bool | from the search results |
| `nb_hits_total` | int | distinct `(query, file)` hits, noisy ones included |
| `nb_hits_excluded` | int | hits dropped by hit heuristics |
| `nb_hits` | int | kept hits |
| `nb_distinct_files`, `nb_distinct_libs`, `nb_queries` | int | over kept hits |
| `libraries`, `queries`, `sources`, `files` | list | over kept hits. `sources` holds `code` and/or `repositories` |

**Repo stage** adds everything in `repos_metadata.csv`:

| column | type | column | type |
|---|---|---|---|
| `metadata_status` | `ok`/`unavailable` | `current_full_name` | text (differs if renamed) |
| `id` | int | `clone_url`, `homepage` | text |
| `description` | text | `language` | text |
| `topics` | list | `stars` | int |
| `forks_count` | int | `watchers` | int |
| `open_issues` | int | `size_kb` | int |
| `is_fork` | bool | `parent_full_name`, `parent_stars` | text, int |
| `archived`, `disabled`, `is_template` | bool | `license` | SPDX id |
| `default_branch` | text | `owner_type` | `User`/`Organization` |
| `created_at`, `updated_at`, `pushed_at` | ISO date | `fetched_at` | ISO date |
| `commit_count` | int (only with `count_commits`) | | |

In the CSV files, lists are joined with `;`.

---

## Recipes

**Measure a heuristic before using it.** Set `action = "flag"`, run `filter`, then look at
`h_<name>` or `flagged_by` in `repos_evaluated.csv`. Switch to `exclude` once you are satisfied.

**Stricter multiplicity** (at least 3 files *and* 2 libraries):

```toml
[heuristics.multiplicity]
min_distinct_files = 3
min_distinct_libs = 2
combine = "all"
```

**Study window from 2023 to mid-2026:**

```toml
[heuristics.created_window]
after = 2022-12-31
before = 2026-07-01
```

**Only MLOps-style projects** (DVC, MLflow, Kubeflow, Airflow): run with
`-q mlflow_project -q dvc_pipeline -q kubeflow_pipeline -q airflow_dag`, or disable the other
queries in the config.

**Try variants without new API calls:**

```bash
uv run ml-repo-sampler filter -r runs/main                       # baseline
uv run ml-repo-sampler filter -r runs/main -d topics -d min_stars # how many more without them?
```

If you loosen an **aggregate** heuristic (e.g. lower `min_distinct_files`), more repos become
pre-selected and need metadata. Run `enrich -r runs/main` first, then `filter`.
