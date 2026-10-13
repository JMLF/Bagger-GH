# Extending the tool

Pick the lightest option that works:

| you want to… | how | code? |
|---|---|---|
| search for another library or pattern | add a `[[search.queries]]` block | no |
| change a threshold or keyword list | edit the `[heuristics.<name>]` section | no |
| reuse a heuristic with other settings | a new section with `type = "<existing type>"` | no |
| filter on any existing column | a `type = "field"` heuristic | no |
| write new filtering logic | a **plugin** file with a `Heuristic` subclass | yes, a small file |
| filter on repo data not collected yet | add a metadata field in `pipeline.py` / `storage.py` | yes |
| data that needs extra API calls (README, contributors…) | extend `step_enrich` | yes |

The first four are covered in [heuristics.md](heuristics.md). This page covers the rest.

---

## 1. Add a search heuristic

Append a block to `config.toml`:

```toml
[[search.queries]]
name = "optuna_study"
library = "optuna"
query = '"optuna.create_study" language:Python'
```

Then check it and add it to an existing run:

```bash
uv run ml-repo-sampler check                        # warns about length/operator limits
uv run ml-repo-sampler run -r runs/main -q optuna_study   # searches only the new query
uv run ml-repo-sampler filter -r runs/main          # evaluate with all enabled queries
```

When you run `-q optuna_study`, `enrich` and `filter` also consider only that query. Run
`filter` without `-q` afterwards so the new hits are combined with the others. For multiplicity
in particular, `libraries` comes from all enabled queries.

Good practice for the paper: keep one query per detection pattern, and use `library` to group
patterns of the same library. `queries.csv` then documents precisely what each pattern found.

---

## 2. Write a new heuristic type

### Where to put it

- **In a plugin file** (recommended for study-specific logic). Any `.py` file listed in
  `[run] plugins` is imported when the config loads, and its `@register`-ed classes become
  available as `type`s:

  ```toml
  [run]
  plugins = ["plugins/library_groups.py"]   # relative to the config file
  ```

- **In `src/ml_repo_sampler/heuristics.py`** if it is generally useful. It is then built-in and
  listed by `ml-repo-sampler list`. Plugin types appear in `list` too.

### Anatomy

A complete, working example is [`plugins/library_groups.py`](../plugins/library_groups.py).
It keeps repos whose hits cover both a *training* library and an *orchestration* library:

```python
from typing import Any

from ml_repo_sampler.config import ConfigError
from ml_repo_sampler.heuristics import Heuristic, register


@register                                   # makes it available as type = "library_groups"
class LibraryGroups(Heuristic):
    type_name = "library_groups"            # the `type` used in config.toml
    stage = "aggregate"                     # "hit" | "aggregate" | "repo"
    description = "Kept hits must cover libraries from at least `min_groups` of the `groups`"
    defaults = {                            # every accepted parameter, with its default
        "groups": {
            "training": ["scikit-learn", "pytorch", "tensorflow", "xgboost", "lightgbm"],
            "orchestration": ["mlflow", "dvc", "kubeflow", "airflow"],
        },
        "min_groups": 2,
    }

    def validate(self) -> None:             # optional: check or normalize self.params once
        if not 1 <= self.params["min_groups"] <= len(self.params["groups"]):
            raise ConfigError(f"[heuristics.{self.name}] 'min_groups' out of range")

    def groups_found(self, record: dict[str, Any]) -> list[str]:
        libs = set(record.get("libraries") or [])
        return [g for g, members in self.params["groups"].items() if libs & set(members)]

    def check(self, record: dict[str, Any]) -> bool:   # required: True = passes
        return len(self.groups_found(record)) >= self.params["min_groups"]

    def extra_columns(self) -> list[str]:   # optional: extra output columns...
        return [f"{self.name}_groups"]

    def annotate(self, record: dict[str, Any]) -> dict[str, Any]:   # ...and their values
        return {f"{self.name}_groups": self.groups_found(record)}
```

Use it in the config:

```toml
[heuristics.training_and_orchestration]
type = "library_groups"
action = "flag"          # try it as a flag first
min_groups = 2
[heuristics.training_and_orchestration.groups]
training = ["scikit-learn", "pytorch", "tensorflow", "xgboost", "lightgbm"]
orchestration = ["mlflow", "dvc", "kubeflow", "airflow"]
```

The output then has `h_training_and_orchestration` and `training_and_orchestration_groups`
columns.

### The contract

- **`type_name`**: unique across built-ins and plugins. A duplicate silently replaces the
  earlier class.
- **`stage`**: decides which record `check` receives (column lists are in
  [heuristics.md](heuristics.md#available-columns)):
  - `hit`: one search hit (`path`, `library`, `full_name`, …). Excluded hits don't count in
    aggregation.
  - `aggregate`: one repo built from its kept hits (`nb_hits`, `libraries`, `files`, `is_fork`, …).
    It runs before enrichment, so it costs no API call.
  - `repo`: aggregate columns plus metadata (`stars`, `topics`, `created_at`, …). It is only
    called when metadata was fetched successfully.
- **`defaults`**: parameters not listed here are rejected when the config loads. Values come
  from TOML: strings, numbers, booleans, dates (`datetime.date`), lists and tables (dicts).
  `self.params` holds the defaults merged with the config values.
- **`validate()`**: raise `ConfigError` with a clear message. It runs when the config loads,
  so mistakes are caught before any API call.
- **`check(record)`**: must be a pure function of the record and the params, with **no API
  calls**. Records come from CSV files, so handle missing values: `None` for scalars, `[]` for
  lists. Return `True` when the record is *acceptable*. `action` then decides what happens
  when it returns `False`.
- **`annotate` / `extra_columns`**: must return the same keys. Lists are written `;`-joined.
- `self.name` is the config section name. Use it in column names so that two instances of the
  same type do not collide.

### Testing a heuristic

```python
from ml_repo_sampler.config import HeuristicConfig
from ml_repo_sampler.heuristics import build_heuristics, load_plugins
from pathlib import Path

load_plugins([Path("plugins/library_groups.py")])
h = build_heuristics([HeuristicConfig("g", "library_groups", True, "exclude", {})])[0]
assert h.check({"libraries": ["scikit-learn", "mlflow"]})
assert not h.check({"libraries": ["scikit-learn"]})
```

See `tests/test_heuristics.py`, and `tests/test_pipeline.py::test_plugin_heuristic` for an
end-to-end version. Run everything with `uv run pytest`.

---

## 3. Collect a new repository field

`GET /repos/{owner}/{name}` returns more than what is stored (`has_wiki`, `has_discussions`,
`network_count`, `visibility`, …). To keep a field so heuristics can use it:

1. Declare the column and its type in `METADATA_SCHEMA` in `src/ml_repo_sampler/storage.py`:
   ```python
   "has_discussions": bool,
   ```
2. Fill it in `extract_metadata` in `src/ml_repo_sampler/pipeline.py`:
   ```python
   "has_discussions": data.get("has_discussions"),
   ```
3. Use it, for example with a `field` heuristic (`field = "has_discussions"`, `op = "=="`,
   `value = true`) or in a custom heuristic.

Repos already in an existing `repos_metadata.csv` keep an **empty** value for the new column.
The file is upgraded to the new header automatically. To fill it for them, move
`repos_metadata.csv` out of the run directory and run `enrich` again: everything is re-fetched,
at one call per repo.

Supported types in schemas: `str`, `int`, `bool`, `list`. Anything else can be stored as a
`str`.

---

## 4. Heuristics that need extra API calls

Some useful signals need more requests: README content, number of contributors, commits,
presence of `requirements.txt`, CI workflows… Heuristics must stay offline, so **collect the
data during enrichment and filter on it afterwards**:

1. Add the column(s) to `METADATA_SCHEMA`, as in section 3.
2. In `step_enrich` (`pipeline.py`), after `extract_metadata`, make the extra call(s) with the
   same `client` and store the result:

   ```python
   meta = extract_metadata(name, client.get(f"/repos/{name}"))
   if meta["metadata_status"] == "ok":
       # 1 extra call per repo: list the root directory
       root = client.get(f"/repos/{name}/contents/") or []
       names = {e["name"] for e in root if isinstance(e, dict)}
       meta["has_requirements"] = bool(names & {"requirements.txt", "pyproject.toml", "environment.yml"})
   ```

   `client.get` already handles tokens, rate limits, retries, and returns `None` for
   404/451/blocked. Prefer calls that return small responses.
3. Filter on the new column with `field` or a custom `repo`-stage heuristic.

Each extra call multiplies the enrichment cost: *N* calls per repo means *N* × the time,
against a 5000 per hour per account `core` quota. Make the extra call only for repos that
already pass the cheap filters, or add a config switch for it.

---

## 5. Other extension points

| to change… | look at |
|---|---|
| a new search backend (e.g. GraphQL, another forge) | `SEARCH_TYPES` in `config.py`; `run_query` and `_hit_from_item` in `pipeline.py`. A search just has to produce rows following `HIT_SCHEMA` |
| how hits are grouped into repos | `aggregate_hits` in `pipeline.py` (add a column to `AGGREGATED_SCHEMA` and compute it there) |
| the columns of the output CSVs | `eval_cols` in `step_evaluate` |
| how the final sample is drawn (e.g. stratified by library) | the end of `step_evaluate` |
| rate-limit and retry behaviour | `GitHubClient.get` in `github.py` |
| the progress bars | `progress.py`, and the `bar(...)` calls in `pipeline.py` / `github.py` |

When you change the pipeline, `tests/test_pipeline.py::FakeClient` shows how to run the
steps end to end against canned API responses, without the network.
