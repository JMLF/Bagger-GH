import time
from types import SimpleNamespace

import pytest

from ml_repo_sampler.config import GitHubConfig
from ml_repo_sampler.github import GitHubClient, InvalidQueryError


class Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers, self.text = status, body or {}, headers or {}, ""

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses, self.headers, self.tokens = list(responses), {}, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.tokens.append(headers["Authorization"])
        return self.responses.pop(0)


NOW = [0.0]  # fake clock of the client: sleeping advances it, so benched tokens free up


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    NOW[0] = time.time()
    monkeypatch.setattr("ml_repo_sampler.github.time", SimpleNamespace(time=lambda: NOW[0]))


def client(responses, tokens=("t1",), **cfg):
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        NOW[0] += seconds

    c = GitHubClient(list(tokens), GitHubConfig(search_min_interval=0, **cfg), sleep=sleep,
                     session=FakeSession(responses))
    return c, slept


def test_ok_and_unavailable():
    c, _ = client([Resp(200, {"a": 1}), Resp(404), Resp(403, {"message": "Repository access blocked"})])
    assert c.get("/repos/a/b") == {"a": 1}
    assert c.get("/repos/a/c") is None
    assert c.get("/repos/a/d") is None


def test_secondary_rate_limit_retries_after_waiting():
    c, slept = client([Resp(403, {"message": "You have exceeded a secondary rate limit"}, {"Retry-After": "30"}),
                       Resp(200, {"ok": True})])
    assert c.get("/search/code", {"q": "x"}) == {"ok": True}
    assert sum(slept) == pytest.approx(31)  # benched 30s (+1s margin), countdown sleeps 1s steps


def test_primary_rate_limit_rotates_tokens():
    reset = str(int(time.time()) + 3600)
    exhausted = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset, "X-RateLimit-Resource": "core"}
    c, slept = client([Resp(403, {"message": "API rate limit exceeded"}, exhausted), Resp(200, {"ok": 1})],
                      tokens=("t1", "t2"))
    assert c.get("/repos/a/b") == {"ok": 1}
    assert c.session.tokens == ["Bearer t1", "Bearer t2"]
    assert not slept  # second token available, no waiting


def test_bad_token_removed_and_invalid_query():
    c, _ = client([Resp(401), Resp(422, {"message": "Validation Failed"})], tokens=("bad", "good"))
    with pytest.raises(InvalidQueryError):
        c.get("/search/code", {"q": ""})
    assert c.token_count == 1


def test_count_reads_last_page_of_link_header():
    link = ('<https://api.github.com/repositories/1/commits?per_page=1&page=2>; rel="next", '
            '<https://api.github.com/repositories/1/commits?per_page=1&page=347>; rel="last"')
    c, _ = client([Resp(200, [{"sha": "a"}], {"Link": link}), Resp(200, [{"sha": "a"}]),
                   Resp(409, {"message": "Git Repository is empty."}), Resp(404)])
    assert c.count("/repos/a/b/commits") == 347
    assert c.count("/repos/a/c/commits") == 1  # single commit: no Link header
    assert c.count("/repos/a/d/commits") == 0  # empty repository
    assert c.count("/repos/a/e/commits") is None


SECONDARY = (403, {"message": "You have exceeded a secondary rate limit"}, {"Retry-After": "30"})


def test_secondary_rate_limit_rotates_tokens():
    c, slept = client([Resp(*SECONDARY), Resp(200, {"ok": 1})], tokens=("t1", "t2"))
    assert c.get("/search/code", {"q": "x"}) == {"ok": 1}
    assert c.session.tokens == ["Bearer t1", "Bearer t2"]
    assert not slept  # t2 is free, no waiting


def test_secondary_rate_limit_on_every_token_cools_down_all():
    c, slept = client([Resp(*SECONDARY)] * 4 + [Resp(200, {"ok": 1})], tokens=("t1", "t2"),
                      secondary_strikes=2, secondary_cooldown=3600)
    assert c.get("/search/code", {"q": "x"}) == {"ok": 1}
    assert c.session.tokens == ["Bearer t1", "Bearer t2"] * 2 + ["Bearer t1"]
    assert sum(slept) == pytest.approx(31 + 3601)  # both benched 30s, then 1h cooldown


def test_request_timeout_is_retried():
    c, slept = client([Resp(408), Resp(200, {"ok": 1})])
    assert c.get("/repos/a/b") == {"ok": 1}
    assert slept == [2]
