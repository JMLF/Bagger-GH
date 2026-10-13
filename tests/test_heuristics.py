import pytest

from ml_repo_sampler.config import ConfigError, HeuristicConfig
from ml_repo_sampler.heuristics import build_heuristics, evaluate


def one(type_, action="exclude", **params):
    return build_heuristics([HeuristicConfig(type_, type_, True, action, params)])[0]


@pytest.mark.parametrize(
    "path,noisy",
    [
        ("src/train.py", False),
        ("tests/test_model.py", True),
        ("examples/pipeline.py", True),
        ("latest/train.py", False),  # "test" must not match inside "latest"
        ("notebooks/Demo.py", True),
    ],
)
def test_path_noise_token(path, noisy):
    assert one("path_noise").check({"path": path}) is (not noisy)


def test_path_noise_substring_mode():
    assert one("path_noise", match="substring").check({"path": "latest/train.py"}) is False


def test_multiplicity_any_all():
    r = {"nb_hits": 2, "nb_distinct_files": 2, "nb_distinct_libs": 1, "sources": ["code"]}
    assert one("multiplicity").check(r)
    assert not one("multiplicity", combine="all").check(r)
    assert not one("multiplicity", min_hits=3).check(r)
    assert one("multiplicity").check({"nb_hits": 1, "nb_distinct_files": 0, "sources": ["repositories"]})


def test_topics_negative_topic_is_exact_and_keywords_are_words():
    h = one("topics")
    assert h.check({"full_name": "a/ml-pipeline", "topics": ["machine-learning"], "description": "A machine learning pipeline"})
    assert not h.check({"full_name": "a/x", "topics": ["learning"]})
    assert not h.check({"full_name": "a/ml-course-2024", "topics": []})
    assert one("topics", require_positive=True).check({"full_name": "a/x", "topics": ["mlops"]})
    assert not one("topics", require_positive=True).check({"full_name": "a/x", "topics": []})


def test_date_windows():
    h = one("created_window", after="2022-12-31")
    assert h.check({"created_at": "2023-01-05T10:00:00Z"})
    assert not h.check({"created_at": "2022-06-05T10:00:00Z"})
    assert not h.check({})
    with pytest.raises(ConfigError):
        one("created_window")


def test_field_rule():
    assert one("field", field="size_kb", op=">=", value=50).check({"size_kb": 60})
    assert not one("field", field="size_kb", op=">=", value=50).check({"size_kb": None})
    assert one("field", field="license", op="in", value=["MIT", "Apache-2.0"]).check({"license": "MIT"})
    assert one("field", field="topics", op="contains", value="mlops").check({"topics": ["mlops"]})
    assert one("field", field="description", op="regex", value="(?i)pipeline").check({"description": "ML Pipeline"})


def test_flag_vs_exclude():
    hs = [one("min_stars", action="flag"), one("exclude_forks")]
    cols, excl, flag = evaluate(hs, {"stars": 0, "is_fork": False})
    assert cols == {"h_min_stars": False, "h_exclude_forks": True}
    assert excl == [] and flag == ["min_stars"]


def test_unknown_param_and_type():
    with pytest.raises(ConfigError):
        one("min_stars", minimum=3)
    with pytest.raises(ConfigError):
        one("does_not_exist")


def test_min_commits():
    h = build_heuristics([HeuristicConfig("mc", "min_commits", True, "exclude", {})])[0]
    assert h.check({"commit_count": 2})
    assert not h.check({"commit_count": 1})
    assert not h.check({"commit_count": None})
