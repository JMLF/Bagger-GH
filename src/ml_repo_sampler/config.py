"""Loading and validation of the TOML configuration and of the GitHub tokens."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .partitioning import DEFAULT_FIELD, Slice, default_range
from .partitioning import validate as validate_partition

SEARCH_TYPES = ("code", "repositories")
SAMPLING_MODES = ("ordered", "stratified")
ACTIONS = ("exclude", "flag")


class ConfigError(Exception):
    """Raised when the configuration (or the token file) is invalid."""


@dataclass
class SearchQuery:
    """One search heuristic: a GitHub search query tagged with the library it detects."""

    name: str
    query: str
    library: str
    type: str = "code"
    enabled: bool = True
    partitions: list[str] = field(default_factory=list)
    # Adaptive partitioning: qualifier to split on ("size", "created"...), or None (off)
    auto_partition: str | None = None
    auto_partition_range: list[Any] | None = None
    # Stop searching this query once it has this many distinct hits (None: no limit)
    max_hits: int | None = None
    # How max_hits is reached: "ordered" walks the partition range in increasing order until the
    # limit; "stratified" takes from every slice a share proportional to its total_count
    sampling: str = "ordered"

    def root_slice(self) -> Slice | None:
        """Full range of the adaptive partitioning, or None if it is off."""
        if not self.auto_partition:
            return None
        rng = self.auto_partition_range or default_range(self.auto_partition, self.type)
        return Slice.root(self.auto_partition, *rng)

    def expanded(self) -> list[tuple[str, str]]:
        """Return ``(partition, full_query)`` pairs, one per partition (or one if none)."""
        parts = self.partitions or [""]
        return [(p, f"{self.query} {p}".strip()) for p in parts]


@dataclass
class HeuristicConfig:
    """Configuration of one filtering heuristic instance."""

    name: str
    type: str
    enabled: bool
    action: str
    params: dict[str, Any]


@dataclass
class GitHubConfig:
    api_url: str = "https://api.github.com"
    token_file: Path | None = None
    token_env: str = "GITHUB_TOKEN"
    timeout: float = 30.0
    max_retries: int = 5
    per_page: int = 100
    max_pages: int = 10
    search_min_interval: float = 6.5
    core_min_interval: float = 0.0
    # secondary rate limits: when every token got `secondary_strikes` of them within
    # `secondary_window` seconds, all tokens are benched for `secondary_cooldown` seconds
    secondary_strikes: int = 3
    secondary_window: float = 900.0
    secondary_cooldown: float = 3600.0
    # enrich: one extra /commits call per repo to store commit_count (used by min_commits)
    count_commits: bool = False


@dataclass
class RunConfig:
    name: str = "ml-pipelines"
    output_dir: Path = Path("runs")
    log_level: str = "INFO"
    progress: bool = True
    plugins: list[Path] = field(default_factory=list)


@dataclass
class SamplingConfig:
    size: int = 0
    seed: int = 42


@dataclass
class Config:
    path: Path
    raw_text: str
    run: RunConfig
    github: GitHubConfig
    queries: list[SearchQuery]
    heuristics: list[HeuristicConfig]
    sampling: SamplingConfig

    def enabled_queries(self) -> list[SearchQuery]:
        return [q for q in self.queries if q.enabled]

    def enabled_heuristics(self) -> list[HeuristicConfig]:
        return [h for h in self.heuristics if h.enabled]

    def apply_overrides(
        self,
        only_queries: list[str] | None = None,
        enable: list[str] | None = None,
        disable: list[str] | None = None,
    ) -> None:
        """Apply CLI selection of queries / heuristics on top of the file configuration."""
        query_names = {q.name for q in self.queries}
        heuristic_names = {h.name for h in self.heuristics}

        if only_queries:
            unknown = set(only_queries) - query_names
            if unknown:
                raise ConfigError(f"Unknown query name(s): {', '.join(sorted(unknown))}")
            for q in self.queries:
                q.enabled = q.name in only_queries

        for names, value in ((enable or [], True), (disable or [], False)):
            unknown = set(names) - heuristic_names - query_names
            if unknown:
                raise ConfigError(f"Unknown heuristic/query name(s): {', '.join(sorted(unknown))}")
            for item in [*self.heuristics, *self.queries]:
                if item.name in names:
                    item.enabled = value

    def to_dict(self) -> dict[str, Any]:
        """Effective configuration (after overrides), without secrets, for the run record."""
        return {
            "config_file": str(self.path),
            "run": {"name": self.run.name, "output_dir": str(self.run.output_dir)},
            "queries": [vars(q) for q in self.queries],
            "heuristics": [vars(h) for h in self.heuristics],
            "sampling": vars(self.sampling),
        }


def _resolve(base: Path, value: str | None) -> Path | None:
    if not value:
        return None
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p)


def _as_str_list(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where} must be a list of strings")
    return value


def load_config(path: str | Path) -> Config:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    raw_text = path.read_text(encoding="utf-8")
    try:
        data = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"Invalid TOML in {path}: {e}") from e
    base = path.parent

    run_d = data.get("run", {})
    run = RunConfig(
        name=run_d.get("name", RunConfig.name),
        output_dir=_resolve(base, run_d.get("output_dir", "runs")),
        log_level=str(run_d.get("log_level", "INFO")).upper(),
        progress=bool(run_d.get("progress", True)),
        plugins=[_resolve(base, p) for p in _as_str_list(run_d.get("plugins", []), "[run] plugins")],
    )

    gh_d = dict(data.get("github", {}))
    known_gh = set(GitHubConfig.__dataclass_fields__)
    unknown = set(gh_d) - known_gh
    if unknown:
        raise ConfigError(f"Unknown [github] key(s): {', '.join(sorted(unknown))}")
    gh_d["token_file"] = _resolve(base, gh_d.get("token_file"))
    github = GitHubConfig(**gh_d)
    if not 1 <= github.per_page <= 100:
        raise ConfigError("[github] per_page must be between 1 and 100")

    search_d = data.get("search", {})
    default_partitions = _as_str_list(search_d.get("default_partitions", []), "[search] default_partitions")
    default_auto = search_d.get("auto_partition", False)
    default_max_hits = search_d.get("max_hits")
    default_sampling = search_d.get("sampling", "ordered")
    queries: list[SearchQuery] = []
    for i, q in enumerate(search_d.get("queries", [])):
        where = f"[[search.queries]] #{i + 1}"
        for key in ("name", "query"):
            if not q.get(key):
                raise ConfigError(f"{where} is missing '{key}'")
        qtype = q.get("type", "code")
        if qtype not in SEARCH_TYPES:
            raise ConfigError(f"{where} ({q['name']}): type must be one of {SEARCH_TYPES}")
        partitions = q["partitions"] if "partitions" in q else default_partitions
        auto = q.get("auto_partition", default_auto)
        auto_field = (DEFAULT_FIELD[qtype] if auto is True else auto) or None
        auto_range = q.get("auto_partition_range")
        max_hits = q.get("max_hits", default_max_hits)
        if max_hits is not None and (isinstance(max_hits, bool) or not isinstance(max_hits, int) or max_hits <= 0):
            raise ConfigError(f"{where} ({q['name']}): max_hits must be a positive integer")
        sampling = q.get("sampling", default_sampling)
        if sampling not in SAMPLING_MODES:
            raise ConfigError(f"{where} ({q['name']}): sampling must be one of {SAMPLING_MODES}")
        if sampling == "stratified" and (not auto_field or partitions):
            if "sampling" in q:
                raise ConfigError(f"{where} ({q['name']}): sampling = \"stratified\" needs auto_partition "
                                  "and no static partitions")
            sampling = "ordered"  # the [search] default does not fit this query
        if auto_field:
            try:
                validate_partition(auto_field, qtype, auto_range, " ".join([q["query"], *partitions]))
            except ValueError as e:
                if "auto_partition" in q:
                    raise ConfigError(f"{where} ({q['name']}): {e}") from e
                auto_field = None  # the [search] default does not fit this query: leave it unsplit
        queries.append(
            SearchQuery(
                name=q["name"],
                query=q["query"],
                library=q.get("library", q["name"]),
                type=qtype,
                enabled=bool(q.get("enabled", True)),
                partitions=_as_str_list(partitions, f"{where} partitions"),
                auto_partition=auto_field,
                auto_partition_range=auto_range,
                max_hits=max_hits,
                sampling=sampling,
            )
        )
    names = [q.name for q in queries]
    if len(names) != len(set(names)):
        raise ConfigError("Search query names must be unique")
    if not queries:
        raise ConfigError("No [[search.queries]] defined")

    heuristics: list[HeuristicConfig] = []
    for name, h in data.get("heuristics", {}).items():
        if not isinstance(h, dict):
            raise ConfigError(f"[heuristics.{name}] must be a table")
        h = dict(h)
        enabled = bool(h.pop("enabled", True))
        action = h.pop("action", "exclude")
        if action not in ACTIONS:
            raise ConfigError(f"[heuristics.{name}] action must be one of {ACTIONS}")
        htype = h.pop("type", name)
        heuristics.append(HeuristicConfig(name=name, type=htype, enabled=enabled, action=action, params=h))

    s_d = data.get("sampling", {})
    sampling = SamplingConfig(size=int(s_d.get("size", 0)), seed=int(s_d.get("seed", 42)))

    cfg = Config(path, raw_text, run, github, queries, heuristics, sampling)
    # Validate heuristic types/params early (import here to avoid a cycle).
    from .heuristics import build_heuristics, load_plugins

    load_plugins(run.plugins)
    build_heuristics(cfg.heuristics)
    return cfg


def load_tokens(github: GitHubConfig) -> list[str]:
    """Tokens come from the env var (comma-separated) or from the TOML token file."""
    env = os.environ.get(github.token_env, "").strip()
    if env:
        return [t.strip() for t in env.split(",") if t.strip()]
    if github.token_file is None:
        raise ConfigError(f"No token: set ${github.token_env} or [github] token_file in the config")
    if not github.token_file.is_file():
        raise ConfigError(
            f"Token file not found: {github.token_file} (copy secrets.example.toml and add your token)"
        )
    try:
        data = tomllib.loads(github.token_file.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"Invalid TOML in token file {github.token_file}: {e}") from e
    gh = data.get("github", {})
    tokens = list(gh.get("tokens", []))
    if gh.get("token"):
        tokens.insert(0, gh["token"])
    tokens = [t.strip() for t in tokens if isinstance(t, str) and t.strip()]
    if not tokens:
        raise ConfigError(f"No token found in {github.token_file} ([github] token = \"...\" or tokens = [...])")
    return tokens
