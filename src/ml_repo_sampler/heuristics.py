"""Filtering heuristics.

Every heuristic is a small class registered under a ``type`` name. A heuristic is
evaluated at one *stage*:

- ``hit``       : on each code-search hit (a file), before aggregation per repository;
- ``aggregate`` : on the per-repository aggregation of hits (no extra API call needed);
- ``repo``      : on the repository metadata fetched during enrichment.

``check(record)`` returns True when the record passes. With ``action = "exclude"`` a
failing record is dropped; with ``action = "flag"`` it is kept and only annotated.

To add a heuristic: subclass :class:`Heuristic`, set ``type_name``/``stage``/``defaults``,
implement ``check`` and decorate it with ``@register``. It is then usable from the
config file as ``[heuristics.<name>] type = "<type_name>"``.
"""

from __future__ import annotations

import copy
import importlib.util
import operator
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, ClassVar

from .config import ConfigError, HeuristicConfig

STAGES = ("hit", "aggregate", "repo")
REGISTRY: dict[str, type[Heuristic]] = {}

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def register(cls: type[Heuristic]) -> type[Heuristic]:
    REGISTRY[cls.type_name] = cls
    return cls


def tokenize(text: str) -> set[str]:
    return {t for t in _TOKEN_SPLIT.split((text or "").lower()) if t}


def parse_date(value: Any) -> datetime | None:
    """Parse TOML dates, 'YYYY-MM-DD' strings and GitHub ISO timestamps into aware datetimes."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as e:
        raise ConfigError(f"Invalid date: {value!r}") from e
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class Heuristic:
    type_name: ClassVar[str]
    stage: ClassVar[str]
    description: ClassVar[str] = ""
    defaults: ClassVar[dict[str, Any]] = {}

    def __init__(self, name: str, action: str = "exclude", params: dict[str, Any] | None = None):
        params = params or {}
        unknown = set(params) - set(self.defaults)
        if unknown:
            raise ConfigError(
                f"[heuristics.{name}] unknown parameter(s) for type '{self.type_name}': "
                f"{', '.join(sorted(unknown))} (allowed: {', '.join(self.defaults) or 'none'})"
            )
        self.name = name
        self.action = action
        self.params = {**copy.deepcopy(self.defaults), **params}
        self.validate()

    def validate(self) -> None:
        """Hook to validate/normalize parameters at construction time."""

    @property
    def column(self) -> str:
        return f"h_{self.name}"

    def check(self, record: dict[str, Any]) -> bool:
        raise NotImplementedError

    def annotate(self, record: dict[str, Any]) -> dict[str, Any]:
        """Optional extra output columns."""
        return {}

    def extra_columns(self) -> list[str]:
        return []


# --------------------------------------------------------------------------- hit stage


@register
class PathNoise(Heuristic):
    type_name = "path_noise"
    stage = "hit"
    description = "Hit file path must not contain example/tutorial/test/... keywords"
    defaults = {
        "patterns": ["example", "examples", "tutorial", "tutorials", "demo", "demos",
                     "sample", "samples", "test", "tests", "docs"],
        "match": "token",  # "token": whole words of the path; "substring": raw substring
    }

    def validate(self) -> None:
        if self.params["match"] not in ("token", "substring"):
            raise ConfigError(f"[heuristics.{self.name}] match must be 'token' or 'substring'")
        self.params["patterns"] = [p.lower() for p in self.params["patterns"]]

    def matches(self, path: str) -> list[str]:
        if not path:
            return []
        if self.params["match"] == "token":
            tokens = tokenize(path)
            return [p for p in self.params["patterns"] if p in tokens]
        low = path.lower()
        return [p for p in self.params["patterns"] if p in low]

    def check(self, record: dict[str, Any]) -> bool:
        return not self.matches(record.get("path", ""))


# --------------------------------------------------------------------- aggregate stage


@register
class Multiplicity(Heuristic):
    type_name = "multiplicity"
    stage = "aggregate"
    description = "Repo must have >= N distinct files OR/AND >= M distinct libraries (and >= K hits)"
    defaults = {
        "min_distinct_files": 2,
        "min_distinct_libs": 2,
        "min_hits": 1,
        "combine": "any",  # "any": files OR libs ; "all": files AND libs
        "exempt_repository_search": True,  # repos found by repository search have no files
    }

    def validate(self) -> None:
        if self.params["combine"] not in ("any", "all"):
            raise ConfigError(f"[heuristics.{self.name}] combine must be 'any' or 'all'")

    def check(self, r: dict[str, Any]) -> bool:
        if self.params["exempt_repository_search"] and "repositories" in (r.get("sources") or []):
            return True
        if (r.get("nb_hits") or 0) < self.params["min_hits"]:
            return False
        conds = [
            (r.get("nb_distinct_files") or 0) >= self.params["min_distinct_files"],
            (r.get("nb_distinct_libs") or 0) >= self.params["min_distinct_libs"],
        ]
        return any(conds) if self.params["combine"] == "any" else all(conds)


@register
class ExcludeForks(Heuristic):
    type_name = "exclude_forks"
    stage = "aggregate"
    description = "Repo must not be a fork (fork flag is known from the search results)"
    defaults: ClassVar[dict[str, Any]] = {}

    def check(self, r: dict[str, Any]) -> bool:
        return not r.get("is_fork")


# -------------------------------------------------------------------------- repo stage


@register
class MinStars(Heuristic):
    type_name = "min_stars"
    stage = "repo"
    description = "Repo must have at least `min_stars` stars"
    defaults = {"min_stars": 2}

    def check(self, r: dict[str, Any]) -> bool:
        return (r.get("stars") or 0) >= self.params["min_stars"]


@register
class MinCommits(Heuristic):
    type_name = "min_commits"
    stage = "repo"
    description = "Default branch must have at least `min_commits` commits (needs [github] count_commits = true)"
    defaults = {"min_commits": 2}

    def check(self, r: dict[str, Any]) -> bool:
        return (r.get("commit_count") or 0) >= self.params["min_commits"]


#ToDo: How the repo stage is evaluated? Seems like at the beggining the hit stage is checked then repos are aggregated and aggregate stage is checked.
# When does the repo stage is schecked and how does that change the results?
@register
class Topics(Heuristic):
    type_name = "topics"
    stage = "repo"
    description = "Negative topics/keywords (tutorial, course...) exclude; positive topics are reported"
    defaults = {
        "negative_topics": ["tutorial", "tutorials", "course", "courses", "learning", "bootcamp"],
        "negative_keywords": ["tutorial", "tutorials", "course", "courses", "bootcamp"],
        "keyword_fields": ["name", "description"],
        "positive_topics": ["machine-learning", "mlops", "scikit-learn", "deep-learning",
                            "pytorch", "tensorflow", "ml-pipeline", "data-science"],
        "require_positive": False,
    }

    def validate(self) -> None:
        for key in ("negative_topics", "negative_keywords", "positive_topics"):
            self.params[key] = [v.lower() for v in self.params[key]]
        allowed = {"name", "description", "full_name", "homepage"}
        bad = set(self.params["keyword_fields"]) - allowed
        if bad:
            raise ConfigError(f"[heuristics.{self.name}] keyword_fields must be in {sorted(allowed)}")

    def _text(self, r: dict[str, Any], fld: str) -> str:
        if fld == "name":
            return (r.get("full_name") or "").split("/")[-1]
        return r.get(fld) or ""

    def negative_matches(self, r: dict[str, Any]) -> list[str]:
        topics = {t.lower() for t in (r.get("topics") or [])}
        found = [f"topic:{t}" for t in self.params["negative_topics"] if t in topics]
        tokens: set[str] = set()
        for fld in self.params["keyword_fields"]:
            tokens |= tokenize(self._text(r, fld))
        found += [f"keyword:{k}" for k in self.params["negative_keywords"] if k in tokens]
        return found

    def positive_matches(self, r: dict[str, Any]) -> list[str]:
        topics = {t.lower() for t in (r.get("topics") or [])}
        return [t for t in self.params["positive_topics"] if t in topics]

    def check(self, r: dict[str, Any]) -> bool:
        if self.negative_matches(r):
            return False
        return not self.params["require_positive"] or bool(self.positive_matches(r))

    def extra_columns(self) -> list[str]:
        return [f"{self.name}_negative", f"{self.name}_positive"]

    def annotate(self, r: dict[str, Any]) -> dict[str, Any]:
        return {f"{self.name}_negative": self.negative_matches(r), f"{self.name}_positive": self.positive_matches(r)}


class _DateWindow(Heuristic):
    field_name: ClassVar[str]
    defaults = {"after": None, "before": None}

    def validate(self) -> None:
        self._after = parse_date(self.params["after"])
        self._before = parse_date(self.params["before"])
        if not (self._after or self._before):
            raise ConfigError(f"[heuristics.{self.name}] needs 'after' and/or 'before'")

    def check(self, r: dict[str, Any]) -> bool:
        value = parse_date(r.get(self.field_name))
        if value is None:
            return False
        if self._after and value <= self._after:
            return False
        return not (self._before and value >= self._before)


@register
class CreatedWindow(_DateWindow):
    type_name = "created_window"
    stage = "repo"
    field_name = "created_at"
    description = "created_at strictly between `after` and `before` (dates, either optional)"


@register
class PushedWindow(_DateWindow):
    type_name = "pushed_window"
    stage = "repo"
    field_name = "pushed_at"
    description = "pushed_at (last push) strictly between `after` and `before` -> drops abandoned repos"


@register
class NotArchived(Heuristic):
    type_name = "not_archived"
    stage = "repo"
    description = "Repo must not be archived or disabled"
    defaults: ClassVar[dict[str, Any]] = {}

    def check(self, r: dict[str, Any]) -> bool:
        return not (r.get("archived") or r.get("disabled"))


@register
class Language(Heuristic):
    type_name = "language"
    stage = "repo"
    description = "Primary language (as detected by GitHub) must be in `languages`"
    defaults = {"languages": ["Python", "Jupyter Notebook"]}

    def check(self, r: dict[str, Any]) -> bool:
        allowed = {lang.lower() for lang in self.params["languages"]}
        return (r.get("language") or "").lower() in allowed


_OPS = {
    "==": operator.eq, "!=": operator.ne, ">": operator.gt,
    ">=": operator.ge, "<": operator.lt, "<=": operator.le,
}


@register
class FieldRule(Heuristic):
    type_name = "field"
    stage = "repo"  # overridden per instance by the `stage` parameter
    description = "Generic rule on any column: field <op> value (op: ==, !=, >, >=, <, <=, in, not_in, contains, not_contains, regex)"
    defaults = {"field": None, "op": "==", "value": None, "stage": "repo"}

    def validate(self) -> None:
        if not self.params["field"]:
            raise ConfigError(f"[heuristics.{self.name}] needs 'field'")
        if self.params["stage"] not in ("aggregate", "repo"):
            raise ConfigError(f"[heuristics.{self.name}] stage must be 'aggregate' or 'repo'")
        valid = {*_OPS, "in", "not_in", "contains", "not_contains", "regex"}
        if self.params["op"] not in valid:
            raise ConfigError(f"[heuristics.{self.name}] op must be one of {sorted(valid)}")
        self.stage = self.params["stage"]  # type: ignore[misc]

    def check(self, r: dict[str, Any]) -> bool:
        op, expected = self.params["op"], self.params["value"]
        actual = r.get(self.params["field"])
        if op == "regex":
            return re.search(str(expected), str(actual or "")) is not None
        if op in ("contains", "not_contains"):
            coll = actual if isinstance(actual, list) else str(actual or "")
            found = expected in coll
            return found if op == "contains" else not found
        if op in ("in", "not_in"):
            found = actual in (expected or [])
            return found if op == "in" else not found
        if actual is None:
            return False
        if isinstance(expected, (int, float)) and not isinstance(actual, (int, float)):
            try:
                actual = float(actual)
            except (TypeError, ValueError):
                return False
        if isinstance(expected, (date, datetime)):
            actual, expected = parse_date(actual), parse_date(expected)
        return _OPS[op](actual, expected)


# ----------------------------------------------------------------------------- helpers


def load_plugins(paths: list[Path]) -> None:
    """Import user Python files so that their ``@register``-ed heuristics become available."""
    for path in paths:
        if not path.is_file():
            raise ConfigError(f"[run] plugins: file not found: {path}")
        mod_name = f"ml_repo_sampler_plugin_{path.stem}"
        if mod_name in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(mod_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as e:
            del sys.modules[mod_name]
            raise ConfigError(f"[run] plugins: error while importing {path}: {e}") from e


def build_heuristics(configs: list[HeuristicConfig], enabled_only: bool = False) -> list[Heuristic]:
    out = []
    for hc in configs:
        if enabled_only and not hc.enabled:
            continue
        cls = REGISTRY.get(hc.type)
        if cls is None:
            raise ConfigError(
                f"[heuristics.{hc.name}] unknown type '{hc.type}' (available: {', '.join(sorted(REGISTRY))})"
            )
        out.append(cls(hc.name, hc.action, hc.params))
    return out


def by_stage(heuristics: list[Heuristic], stage: str) -> list[Heuristic]:
    return [h for h in heuristics if h.stage == stage]


def evaluate(heuristics: list[Heuristic], record: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    """Evaluate heuristics on a record.

    Returns (columns, failed_exclude_names, failed_flag_names). ``columns`` holds one
    boolean ``h_<name>`` column per heuristic plus any annotation columns.
    """
    cols: dict[str, Any] = {}
    failed_excl, failed_flag = [], []
    for h in heuristics:
        ok = h.check(record)
        cols[h.column] = ok
        cols.update(h.annotate(record))
        if not ok:
            (failed_excl if h.action == "exclude" else failed_flag).append(h.name)
    return cols, failed_excl, failed_flag
