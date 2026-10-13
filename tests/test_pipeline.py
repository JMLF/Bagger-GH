"""End-to-end run of search -> enrich -> evaluate against a fake GitHub API."""

import shutil
from importlib import resources

import pytest

from ml_repo_sampler import pipeline
from ml_repo_sampler import storage as st
from ml_repo_sampler.config import ConfigError, load_config


def code_item(repo, path, fork=False):
    return {"name": path.split("/")[-1], "path": path, "sha": "abc", "html_url": f"https://github.com/{repo}/blob/main/{path}",
            "repository": {"full_name": repo, "html_url": f"https://github.com/{repo}", "fork": fork}}


SEARCH = {
    "sklearn_pipeline": [code_item("good/proj", "src/train.py"), code_item("good/proj", "src/features.py"),
                         code_item("toy/one", "main.py"), code_item("noisy/repo", "examples/a.py"),
                         code_item("noisy/repo", "tests/test_b.py"), code_item("fork/proj", "a.py", fork=True),
                         code_item("fork/proj", "b.py", fork=True), code_item("course/ml-course", "a.py"),
                         code_item("course/ml-course", "b.py"), code_item("old/proj", "a.py"), code_item("old/proj", "b.py")],
    "xgboost": [code_item("toy/one", "main.py"), code_item("good/proj", "src/train.py")],
}

REPOS = {
    "good/proj": {"stars": 10, "created": "2024-01-01T00:00:00Z", "topics": ["mlops"]},
    "toy/one": {"stars": 0, "created": "2024-01-01T00:00:00Z", "topics": []},
    "course/ml-course": {"stars": 50, "created": "2024-01-01T00:00:00Z", "topics": []},
    "old/proj": {"stars": 50, "created": "2020-01-01T00:00:00Z", "topics": []},
}


class FakeClient:
    def __init__(self):
        self.calls = []
        self.request_count = 0

    def get(self, path, params=None):
        self.calls.append((path, params))
        if path == "/search/code":
            name = next(k for k in SEARCH if k.split("_")[0] in params["q"].lower())
            items = SEARCH[name]
            return {"total_count": len(items), "incomplete_results": False, "items": items}
        name = path.removeprefix("/repos/")
        if name not in REPOS:
            return None
        r = REPOS[name]
        return {"full_name": name, "html_url": f"https://github.com/{name}", "clone_url": f"https://github.com/{name}.git",
                "stargazers_count": r["stars"], "topics": r["topics"], "fork": False, "created_at": r["created"],
                "pushed_at": "2026-01-01T00:00:00Z", "language": "Python", "size": 100, "owner": {"type": "User"}}

    def count(self, path, params=None):
        self.calls.append((path, params))
        return COMMITS.get(path.removeprefix("/repos/").removesuffix("/commits"))

COMMITS = {"good/proj": 120, "toy/one": 1, "course/ml-course": 30, "old/proj": 5}


@pytest.fixture
def cfg(tmp_path):
    src = resources.files("ml_repo_sampler") / "templates" / "config.toml"
    path = tmp_path / "config.toml"
    shutil.copyfile(src, path)
    c = load_config(path)
    c.apply_overrides(only_queries=["sklearn_pipeline", "xgboost"])
    return c


def test_end_to_end(cfg, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    client = FakeClient()
    pipeline.step_search(cfg, run_dir, client)
    pipeline.step_enrich(cfg, run_dir, client)
    summary = pipeline.step_evaluate(cfg, run_dir)

    enriched = {p for p, _ in client.calls if p.startswith("/repos/")}
    # noisy/repo (all hits noisy) and fork/proj (fork) are dropped before enrichment
    assert enriched == {"/repos/good/proj", "/repos/toy/one", "/repos/course/ml-course", "/repos/old/proj"}

    evaluated = {r["full_name"]: r for r in st.read_rows(run_dir / st.EVALUATED_CSV)}
    assert evaluated["noisy/repo"]["excluded_by"].startswith("all_hits_excluded")
    assert evaluated["fork/proj"]["excluded_by"] == "exclude_forks"
    assert evaluated["toy/one"]["excluded_by"] == "min_stars"  # 2 libs -> passes multiplicity
    assert evaluated["course/ml-course"]["excluded_by"] == "topics"
    assert evaluated["old/proj"]["excluded_by"] == "created_window"
    assert [r["full_name"] for r in st.read_rows(run_dir / st.CANDIDATES_CSV)] == ["good/proj"]
    assert summary["candidates"] == 1

    funnel = st.read_rows(run_dir / st.FUNNEL_CSV)
    assert funnel[-1]["step"] == "selected" and funnel[-1]["remaining"] == "1"

    # Resume: nothing is re-fetched
    n = len(client.calls)
    pipeline.step_search(cfg, run_dir, client)
    pipeline.step_enrich(cfg, run_dir, client)
    assert len(client.calls) == n

    # Offline re-filtering with a heuristic disabled
    cfg.apply_overrides(disable=["min_stars"])
    pipeline.step_evaluate(cfg, run_dir)
    assert {r["full_name"] for r in st.read_rows(run_dir / st.CANDIDATES_CSV)} == {"good/proj", "toy/one"}


def test_overrides_reject_unknown_names(cfg):
    with pytest.raises(ConfigError):
        cfg.apply_overrides(only_queries=["nope"])
    with pytest.raises(ConfigError):
        cfg.apply_overrides(disable=["nope"])


def test_query_checks():
    assert pipeline.check_query("a " * 200)
    assert pipeline.check_query(" OR ".join("abcdefg"))
    assert not pipeline.check_query('"import xgboost" language:Python')


def test_plugin_heuristic(tmp_path):
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(f"""
[run]
plugins = ["{repo_root / 'plugins' / 'library_groups.py'}"]

[[search.queries]]
name = "sklearn_pipeline"
library = "scikit-learn"
query = "x"

[[search.queries]]
name = "mlflow_project"
library = "mlflow"
query = "y"

[heuristics.groups]
type = "library_groups"
[heuristics.groups.groups]
training = ["scikit-learn"]
orchestration = ["mlflow"]
""")
    cfg = load_config(cfg_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    hit = dict.fromkeys(st.HIT_SCHEMA, "")
    hits = [
        {**hit, "query_name": "sklearn_pipeline", "library": "scikit-learn", "query_type": "code", "full_name": "a/both", "path": "t.py"},
        {**hit, "query_name": "mlflow_project", "library": "mlflow", "query_type": "code", "full_name": "a/both", "path": "MLproject"},
        {**hit, "query_name": "sklearn_pipeline", "library": "scikit-learn", "query_type": "code", "full_name": "b/one", "path": "t.py"},
    ]
    st.write_rows(run_dir / st.HITS_CSV, list(st.HIT_SCHEMA), hits)
    pipeline.step_evaluate(cfg, run_dir)
    agg = {r["full_name"]: r for r in st.read_rows(run_dir / st.AGGREGATED_CSV)}
    assert agg["a/both"]["h_groups"] == "true" and agg["a/both"]["groups_groups"] == "training;orchestration"
    assert agg["b/one"]["h_groups"] == "false"


def test_plugin_missing_file(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('[run]\nplugins = ["nope.py"]\n[[search.queries]]\nname = "a"\nquery = "x"\n')
    with pytest.raises(ConfigError, match="not found"):
        load_config(cfg_path)


def test_commit_count_backfill_and_min_commits(cfg, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    client = FakeClient()
    cfg.apply_overrides(disable=["min_stars", "topics", "created_window"])
    pipeline.step_search(cfg, run_dir, client)
    pipeline.step_enrich(cfg, run_dir, client)
    assert not any(p.endswith("/commits") for p, _ in client.calls)

    # Turned on later: cached repos get their commit_count on the next enrich
    cfg.github.count_commits = True
    cfg.apply_overrides(enable=["min_commits"])
    pipeline.step_enrich(cfg, run_dir, client)
    meta = {m["full_name"]: m for m in st.read_rows(run_dir / st.METADATA_CSV, st.METADATA_SCHEMA)}
    assert {n: m["commit_count"] for n, m in meta.items()} == COMMITS

    pipeline.step_evaluate(cfg, run_dir)
    evaluated = {r["full_name"]: r for r in st.read_rows(run_dir / st.EVALUATED_CSV)}
    assert evaluated["toy/one"]["excluded_by"] == "min_commits"
    assert {r["full_name"] for r in st.read_rows(run_dir / st.CANDIDATES_CSV)} == {
        "good/proj", "course/ml-course", "old/proj"}

    n = len(client.calls)
    pipeline.step_enrich(cfg, run_dir, client)  # nothing left to count
    assert len(client.calls) == n
