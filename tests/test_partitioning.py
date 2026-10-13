"""Adaptive partitioning: slice arithmetic, config validation and the search walk."""

import re
from datetime import date

import pytest

from ml_repo_sampler import pipeline
from ml_repo_sampler import storage as st
from ml_repo_sampler.config import ConfigError, load_config
from ml_repo_sampler.partitioning import Slice


def test_numeric_split_is_geometric_and_covers_range():
    lo, hi = Slice("size", 0, 1_000_000).split()
    assert (lo.lo, hi.hi) == (0, 1_000_000) and lo.hi + 1 == hi.lo
    assert lo.hi < 2000  # geometric midpoint, not 500000
    assert Slice("size", 7, 8).split() == (Slice("size", 7, 7), Slice("size", 8, 8))
    assert Slice("size", 7, 7).split() is None


def test_date_slice():
    root = Slice.root("created", "2023-01-01", date(2023, 1, 31))
    assert root.qualifier == "created:2023-01-01..2023-01-31"
    a, b = root.split()
    assert a.qualifier == "created:2023-01-01..2023-01-16" and b.qualifier == "created:2023-01-17..2023-01-31"


def write_cfg(tmp_path, body):
    path = tmp_path / "config.toml"
    path.write_text(body)
    return load_config(path)


def test_config_validation(tmp_path):
    q = '[[search.queries]]\nname = "a"\nquery = "x language:Python"\n'
    assert write_cfg(tmp_path, q + "auto_partition = true\n").queries[0].auto_partition == "size"
    assert write_cfg(tmp_path, "[search]\nauto_partition = true\n" + q).queries[0].auto_partition == "size"
    assert write_cfg(tmp_path, q).queries[0].root_slice() is None
    repo = write_cfg(tmp_path, q + 'type = "repositories"\nauto_partition = true\n'
                                   "auto_partition_range = [2023-01-01, 2024-01-01]\n").queries[0]
    assert repo.root_slice().qualifier == "created:2023-01-01..2024-01-01"
    with pytest.raises(ConfigError, match="not supported"):
        write_cfg(tmp_path, q + 'auto_partition = "stars"\n')
    default_skips = write_cfg(tmp_path, "[search]\nauto_partition = true\n" + q + 'partitions = ["size:<100"]\n')
    assert default_skips.queries[0].auto_partition is None
    with pytest.raises(ConfigError, match="already use"):
        write_cfg(tmp_path, q + 'auto_partition = true\npartitions = ["size:<100"]\n')


class SizedSearch:
    """Fake code search: file i has size i bytes; ``dense`` puts 1500 files at size 42."""

    def __init__(self, n_files, dense=False):
        self.files = [(i, f"f{i}.py") for i in range(n_files)]
        if dense:
            self.files += [(42, f"dense{i}.py") for i in range(1500)]
        self.calls = []
        self.request_count = 0

    def get(self, path, params=None):
        self.calls.append(params["q"])
        m = re.search(r"size:(\d+)\.\.(\d+)", params["q"])
        lo, hi = (int(m[1]), int(m[2])) if m else (0, 10**9)
        match = [p for s, p in self.files if lo <= s <= hi]
        start = (params["page"] - 1) * params["per_page"]
        items = [{"name": p, "path": p, "sha": "x", "html_url": "",
                  "repository": {"full_name": f"o/{p}", "html_url": "", "fork": False}}
                 for p in match[:1000][start:start + params["per_page"]]]
        return {"total_count": len(match), "incomplete_results": False, "items": items}


@pytest.fixture
def auto_cfg(tmp_path):
    return write_cfg(tmp_path, '[[search.queries]]\nname = "a"\nquery = "x"\n'
                               "auto_partition = true\nauto_partition_range = [0, 9999]\n")


def test_search_splits_until_under_cap_and_resumes(auto_cfg, tmp_path):
    client = SizedSearch(5000)
    pipeline.step_search(auto_cfg, tmp_path, client)
    hits = st.read_rows(tmp_path / st.HITS_CSV, st.HIT_SCHEMA)
    assert len({h["path"] for h in hits}) == 5000  # everything, not just 1000
    rows = st.read_rows(tmp_path / st.QUERIES_CSV, st.QUERY_SCHEMA)
    assert not any(r["capped"] for r in rows)
    assert {r["status"] for r in rows} == {"done", "split"}
    assert all(r["total_count"] <= 800 for r in rows if r["status"] == "done")
    assert all(r["items_fetched"] == 0 for r in rows if r["status"] == "split")

    n = len(client.calls)
    pipeline.step_search(auto_cfg, tmp_path, client)
    assert len(client.calls) == n


def test_underestimated_slice_reaching_cap_is_split(auto_cfg, tmp_path):
    client = SizedSearch(3000)
    real_get = client.get

    def lying_get(path, params=None):  # total_count underestimated, as GitHub does
        data = real_get(path, params)
        return {**data, "total_count": min(data["total_count"], 700)}

    client.get = lying_get
    pipeline.step_search(auto_cfg, tmp_path, client)
    hits = st.read_rows(tmp_path / st.HITS_CSV, st.HIT_SCHEMA)
    assert len({h["path"] for h in hits}) == 3000
    rows = st.read_rows(tmp_path / st.QUERIES_CSV, st.QUERY_SCHEMA)
    assert rows[0]["status"] == "split" and rows[0]["items_fetched"] == 1000
    assert not any(r["capped"] for r in rows)


def test_single_value_over_cap_is_fetched_and_flagged(auto_cfg, tmp_path):
    pipeline.step_search(auto_cfg, tmp_path, SizedSearch(100, dense=True))
    rows = st.read_rows(tmp_path / st.QUERIES_CSV, st.QUERY_SCHEMA)
    capped = [r for r in rows if r["capped"]]
    assert [r["partition"] for r in capped] == ["size:42..42"]
    assert capped[0]["items_fetched"] == 1000


def test_max_hits_stops_query_and_resume_counts_existing_hits(tmp_path):
    body = ('[[search.queries]]\nname = "a"\nquery = "x"\n'
            "auto_partition = true\nauto_partition_range = [0, 9999]\nmax_hits = {}\n")
    client = SizedSearch(5000)
    pipeline.step_search(write_cfg(tmp_path, body.format(1250)), tmp_path, client)
    hits = st.read_rows(tmp_path / st.HITS_CSV, st.HIT_SCHEMA)
    assert len({h["path"] for h in hits}) == 1250  # stops mid-slice, at page granularity
    rows = st.read_rows(tmp_path / st.QUERIES_CSV, st.QUERY_SCHEMA)
    assert rows[-1]["status"] == "limited"

    # Resume with the same limit: nothing to fetch
    n = len(client.calls)
    pipeline.step_search(write_cfg(tmp_path, body.format(1250)), tmp_path, client)
    assert len(client.calls) == n

    # Raise the limit: continues where it stopped, the limited slice is fetched again
    pipeline.step_search(write_cfg(tmp_path, body.format(2000)), tmp_path, client)
    hits = st.read_rows(tmp_path / st.HITS_CSV, st.HIT_SCHEMA)
    assert len({h["path"] for h in hits}) == 2000


def test_max_hits_validation(tmp_path):
    q = '[[search.queries]]\nname = "a"\nquery = "x language:Python"\n'
    assert write_cfg(tmp_path, "[search]\nmax_hits = 10\n" + q).queries[0].max_hits == 10
    assert write_cfg(tmp_path, "[search]\nmax_hits = 10\n" + q + "max_hits = 5\n").queries[0].max_hits == 5
    assert write_cfg(tmp_path, q).queries[0].max_hits is None
    with pytest.raises(ConfigError, match="max_hits"):
        write_cfg(tmp_path, q + "max_hits = 0\n")


STRATIFIED = ('[[search.queries]]\nname = "a"\nquery = "x"\nauto_partition = true\n'
              'auto_partition_range = [0, 9999]\nsampling = "stratified"\n')


def sample(tmp_path, max_hits, client):
    cfg = write_cfg(tmp_path, STRATIFIED + (f"max_hits = {max_hits}\n" if max_hits else ""))
    pipeline.step_search(cfg, tmp_path, client)
    return sorted(int(h["path"][1:-3]) for h in pipeline.prepare(cfg, tmp_path).hits)  # file sizes


def assert_proportional(tmp_path, sizes, fraction):
    """Each leaf band [lo..hi] holds round(fraction x its file count) of the sample."""
    cfg = write_cfg(tmp_path, STRATIFIED + f"max_hits = {round(fraction * 5000)}\n")
    leaves, pending = pipeline.stratified_leaves(cfg.queries[0], *pipeline._latest_rows(tmp_path), 100)
    assert not pending and len(leaves) >= 5
    for partition in leaves:
        lo, hi = map(int, partition.removeprefix("size:").split(".."))
        n_files = len(range(lo, min(hi, 4999) + 1))
        assert sum(lo <= s <= hi for s in sizes) == round(fraction * n_files)


def test_stratified_sample_spreads_over_range_and_grows_on_resume(tmp_path):
    client = SizedSearch(5000)
    sizes = sample(tmp_path, 500, client)
    assert abs(len(sizes) - 500) <= 25 and len(set(sizes)) == len(sizes)
    assert_proportional(tmp_path, sizes, 0.1)  # spread over the whole range, not the lowest sizes
    assert len(client.calls) < 50  # far fewer calls than a full collection

    n = len(client.calls)
    assert sample(tmp_path, 500, client) == sizes  # resume: same sample, no API call
    assert len(client.calls) == n

    bigger = sample(tmp_path, 2000, client)  # higher max_hits: re-uses the tree, fetches more
    assert abs(len(bigger) - 2000) <= 60
    assert_proportional(tmp_path, bigger, 0.4)

    n = len(client.calls)
    smaller = sample(tmp_path, 300, client)  # lower max_hits: strata merge, served from stored hits
    assert len(client.calls) == n
    assert abs(len(smaller) - 300) <= 20
    assert_proportional(tmp_path, smaller, 0.06)


def test_stratified_lower_max_hits_after_a_large_sample_needs_no_call(tmp_path):
    client = SizedSearch(5000)
    sample(tmp_path, 2000, client)
    n = len(client.calls)
    smaller = sample(tmp_path, 400, client)  # strata never used before: their probe page is kept
    assert len(client.calls) == n
    assert_proportional(tmp_path, smaller, 0.08)


def test_stratified_with_max_hits_above_total_collects_everything(tmp_path):
    assert sample(tmp_path, 10_000, SizedSearch(5000)) == list(range(5000))
    rows = st.read_rows(tmp_path / st.QUERIES_CSV, st.QUERY_SCHEMA)
    assert {r["status"] for r in rows} == {"done", "split"}


def test_stratified_raised_above_total_completes_previous_sample(tmp_path):
    client = SizedSearch(5000)
    sample(tmp_path, 500, client)
    assert sample(tmp_path, 10_000, client) == list(range(5000))
    assert sample(tmp_path, None, client) == list(range(5000))  # no max_hits: everything


def test_stratified_needs_auto_partition(tmp_path):
    with pytest.raises(ConfigError, match="stratified"):
        write_cfg(tmp_path, '[[search.queries]]\nname = "a"\nquery = "x"\nsampling = "stratified"\n')
    q = '[[search.queries]]\nname = "a"\nquery = "x"\n'
    assert write_cfg(tmp_path, '[search]\nsampling = "stratified"\n' + q).queries[0].sampling == "ordered"
