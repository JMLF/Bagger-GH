"""Command line interface: ``ml-repo-sampler <command> -c config.toml``."""

from __future__ import annotations

import logging
import sys
from importlib import resources
from pathlib import Path
from typing import Annotated

import typer

from . import pipeline, progress
from .config import Config, ConfigError, load_config, load_tokens
from .github import GitHubClient, GitHubError
from .heuristics import REGISTRY, build_heuristics
from .storage import LOG_FILE

app = typer.Typer(
    help="Sample GitHub repositories containing ML pipelines using configurable search/filter heuristics.",
    no_args_is_help=True,
    add_completion=False,
)
log = logging.getLogger("ml_repo_sampler")

ConfigOpt = Annotated[Path, typer.Option("--config", "-c", help="TOML configuration file.")]
RunDirOpt = Annotated[
    Path | None,
    typer.Option("--run-dir", "-r", help="Run directory. Reuse an existing one to resume or re-filter."),
]
QueryOpt = Annotated[
    list[str] | None, typer.Option("--query", "-q", help="Only run these search queries (repeatable).")
]
EnableOpt = Annotated[
    list[str] | None, typer.Option("--enable", "-e", help="Enable a heuristic/query by name (repeatable).")
]
DisableOpt = Annotated[
    list[str] | None, typer.Option("--disable", "-d", help="Disable a heuristic/query by name (repeatable).")
]
VerboseOpt = Annotated[bool, typer.Option("--verbose", "-v", help="Show DEBUG logs on the console.")]
NoProgressOpt = Annotated[
    bool, typer.Option("--no-progress", help="Disable the tqdm progress bars (also: [run] progress = false).")
]


def _setup_logging(level: str, run_dir: Path | None, verbose: bool) -> None:
    log.setLevel(logging.DEBUG)
    log.propagate = False
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    console = progress.TqdmLoggingHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else level)
    console.setFormatter(fmt)
    log.addHandler(console)
    if run_dir is not None:
        run_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(run_dir / LOG_FILE, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        log.addHandler(fh)


def _load(config: Path, queries=None, enable=None, disable=None) -> Config:
    try:
        cfg = load_config(config)
        cfg.apply_overrides(queries, enable, disable)
        build_heuristics(cfg.heuristics)
    except ConfigError as e:
        typer.secho(f"Configuration error: {e}", fg="red", err=True)
        raise typer.Exit(2) from e
    return cfg


def _client(cfg: Config) -> GitHubClient:
    try:
        return GitHubClient(load_tokens(cfg.github), cfg.github)
    except (ConfigError, GitHubError) as e:
        log.error("%s", e)
        raise typer.Exit(2) from e


def _start(cfg: Config, run_dir: Path | None, command: str, verbose: bool, must_exist: bool = False,
           no_progress: bool = False) -> Path:
    progress.set_enabled(cfg.run.progress and not no_progress)
    if run_dir is None:
        if must_exist:
            typer.secho("--run-dir is required for this command", fg="red", err=True)
            raise typer.Exit(2)
        run_dir = pipeline.new_run_dir(cfg)
    elif must_exist and not run_dir.is_dir():
        typer.secho(f"Run directory not found: {run_dir}", fg="red", err=True)
        raise typer.Exit(2)
    _setup_logging(cfg.run.log_level, run_dir, verbose)
    pipeline.record_config(cfg, run_dir, command)
    log.info("=== %s | config=%s | run_dir=%s", command, cfg.path, run_dir)
    log.info("Queries enabled: %s", ", ".join(q.name for q in cfg.enabled_queries()) or "none")
    log.info("Heuristics enabled: %s", ", ".join(f"{h.name}({h.action})" for h in cfg.enabled_heuristics()) or "none")
    return run_dir


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except GitHubError as e:
        log.error("GitHub error: %s (re-run the same command with --run-dir to resume)", e)
        raise typer.Exit(1) from e
    except KeyboardInterrupt:
        log.warning("Interrupted (re-run the same command with --run-dir to resume)")
        raise typer.Exit(130) from None


@app.command()
def init(
    directory: Annotated[Path, typer.Argument(help="Where to write config.toml and secrets.example.toml")] = Path("."),
    force: Annotated[bool, typer.Option("--force", help="Overwrite existing files")] = False,
) -> None:
    """Write a default configuration (heuristics from process.md) and a token file template."""
    directory.mkdir(parents=True, exist_ok=True)
    templates = resources.files("ml_repo_sampler") / "templates"
    for src, dst in (("config.toml", "config.toml"), ("secrets.example.toml", "secrets.example.toml")):
        target = directory / dst
        if target.exists() and not force:
            typer.echo(f"skip {target} (exists, use --force)")
            continue
        target.write_text((templates / src).read_text(encoding="utf-8"), encoding="utf-8")
        typer.echo(f"wrote {target}")
    typer.echo("Next: cp secrets.example.toml secrets.toml, add your token, then `ml-repo-sampler check`.")


@app.command("list")
def list_(config: ConfigOpt = Path("config.toml")) -> None:
    """List the search queries and heuristics defined in the config, and all available heuristic types."""
    cfg = _load(config)
    typer.secho("Search queries", bold=True)
    for q in cfg.queries:
        mark = typer.style("on " if q.enabled else "off", fg="green" if q.enabled else "red")
        parts = f"  partitions={q.partitions}" if q.partitions else ""
        if q.auto_partition:
            parts += f"  auto_partition={q.root_slice().qualifier}"
        typer.echo(f"  [{mark}] {q.name:<22} {q.type:<12} {q.library:<16} {q.query}{parts}")
    typer.secho("\nHeuristics (in config)", bold=True)
    for h, obj in zip(cfg.heuristics, build_heuristics(cfg.heuristics), strict=True):
        mark = typer.style("on " if h.enabled else "off", fg="green" if h.enabled else "red")
        typer.echo(f"  [{mark}] {h.name:<22} type={h.type:<15} stage={obj.stage:<9} action={h.action:<7} {obj.params}")
    typer.secho("\nAvailable heuristic types", bold=True)
    for name, cls in sorted(REGISTRY.items()):
        typer.echo(f"  {name:<15} ({cls.stage}) {cls.description}")


@app.command()
def check(config: ConfigOpt = Path("config.toml"), verbose: VerboseOpt = False) -> None:
    """Validate the config and the GitHub token(s); print remaining rate limits."""
    cfg = _load(config)
    _setup_logging(cfg.run.log_level, None, verbose)
    typer.echo(f"Config OK: {len(cfg.enabled_queries())}/{len(cfg.queries)} queries, "
               f"{len(cfg.enabled_heuristics())}/{len(cfg.heuristics)} heuristics enabled")
    for q in cfg.enabled_queries():
        for _, fq in q.expanded():
            for problem in pipeline.check_query(fq):
                typer.secho(f"  warning: {q.name}: {problem}", fg="yellow")
    client = _client(cfg)
    ok = True
    for row in client.rate_limits():
        if row["status"] == 200:
            typer.echo(f"Token {row['token']}: core={row['core']} search={row['search']} "
                       f"code_search={row['code_search']}")
        else:
            ok = False
            typer.secho(f"Token {row['token']}: HTTP {row['status']} {row.get('error', '')}", fg="red")
    raise typer.Exit(0 if ok else 1)


@app.command()
def run(
    config: ConfigOpt = Path("config.toml"),
    run_dir: RunDirOpt = None,
    query: QueryOpt = None,
    enable: EnableOpt = None,
    disable: DisableOpt = None,
    enrich_all: Annotated[bool, typer.Option("--enrich-all", help="Enrich every repo found, not only pre-selected ones.")] = False,
    verbose: VerboseOpt = False,
    no_progress: NoProgressOpt = False,
) -> None:
    """Full pipeline: search -> enrich -> filter. Resumes if --run-dir already exists."""
    cfg = _load(config, query, enable, disable)
    run_dir = _start(cfg, run_dir, "run", verbose, no_progress=no_progress)
    client = _client(cfg)
    _guard(pipeline.step_search, cfg, run_dir, client)
    _guard(pipeline.step_enrich, cfg, run_dir, client, enrich_all)
    pipeline.step_evaluate(cfg, run_dir)
    log.info("Done (%d API requests). Outputs in %s", client.request_count, run_dir)


@app.command()
def search(
    config: ConfigOpt = Path("config.toml"),
    run_dir: RunDirOpt = None,
    query: QueryOpt = None,
    enable: EnableOpt = None,
    disable: DisableOpt = None,
    verbose: VerboseOpt = False,
    no_progress: NoProgressOpt = False,
) -> None:
    """Step 1: run the search queries (hits.csv, queries.csv)."""
    cfg = _load(config, query, enable, disable)
    run_dir = _start(cfg, run_dir, "search", verbose, no_progress=no_progress)
    client = _client(cfg)
    _guard(pipeline.step_search, cfg, run_dir, client)
    log.info("Search done (%d API requests). Next: enrich --run-dir %s", client.request_count, run_dir)


@app.command()
def enrich(
    config: ConfigOpt = Path("config.toml"),
    run_dir: RunDirOpt = None,
    query: QueryOpt = None,
    enable: EnableOpt = None,
    disable: DisableOpt = None,
    enrich_all: Annotated[bool, typer.Option("--all", help="Enrich every repo found, not only pre-selected ones.")] = False,
    retry_unavailable: Annotated[bool, typer.Option("--retry-unavailable", help="Re-fetch repos previously unavailable.")] = False,
    verbose: VerboseOpt = False,
    no_progress: NoProgressOpt = False,
) -> None:
    """Step 2: fetch repository metadata (repos_metadata.csv) for repos passing hit/aggregate heuristics."""
    cfg = _load(config, query, enable, disable)
    run_dir = _start(cfg, run_dir, "enrich", verbose, must_exist=True, no_progress=no_progress)
    client = _client(cfg)
    _guard(pipeline.step_enrich, cfg, run_dir, client, enrich_all, retry_unavailable)
    log.info("Enrich done (%d API requests). Next: filter --run-dir %s", client.request_count, run_dir)


@app.command("filter")
def filter_(
    config: ConfigOpt = Path("config.toml"),
    run_dir: RunDirOpt = None,
    query: QueryOpt = None,
    enable: EnableOpt = None,
    disable: DisableOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Step 3 (offline, no API call): apply the enabled heuristics and write candidates/funnel/sample CSVs."""
    cfg = _load(config, query, enable, disable)
    run_dir = _start(cfg, run_dir, "filter", verbose, must_exist=True)
    pipeline.step_evaluate(cfg, run_dir)


def main() -> None:
    app()
