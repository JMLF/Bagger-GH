"""Minimal GitHub REST client with token rotation, rate-limit waiting and retries."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import requests
from requests.utils import parse_header_links

from .config import GitHubConfig
from .progress import bar

log = logging.getLogger(__name__)


class GitHubError(Exception):
    pass


class InvalidQueryError(GitHubError):
    """HTTP 422: the search query was rejected by GitHub."""


@dataclass
class _TokenState:
    token: str
    # resource -> (remaining, reset epoch seconds)
    limits: dict[str, tuple[int, float]] = field(default_factory=dict)
    # secondary rate limit: the token is benched (for every resource) until this epoch
    blocked_until: float = 0.0
    # epochs of the recent secondary rate limits hit by this token
    secondary_hits: list[float] = field(default_factory=list)

    def available_at(self, resource: str) -> float:
        now = time.time()
        remaining, reset = self.limits.get(resource, (1, 0.0))
        primary = reset if remaining <= 0 and now < reset else 0.0
        return max(primary, self.blocked_until if now < self.blocked_until else 0.0)

    @property
    def masked(self) -> str:
        return f"{self.token[:4]}…{self.token[-4:]}" if len(self.token) > 12 else "****"


def resource_for(path: str) -> str:
    if path.startswith("/search/code"):
        return "code_search"
    if path.startswith("/search/"):
        return "search"
    return "core"


def _message(resp: requests.Response) -> str:
    try:
        return str(resp.json().get("message", ""))
    except ValueError:
        return resp.text[:200]


class GitHubClient:
    def __init__(
        self,
        tokens: list[str],
        cfg: GitHubConfig,
        sleep: Callable[[float], None] = time.sleep,
        session: requests.Session | None = None,
    ):
        if not tokens:
            raise GitHubError("No GitHub token configured")
        self.cfg = cfg
        self._tokens = [_TokenState(t) for t in tokens]
        self._next = 0
        self._last_request: dict[str, float] = {}
        self._sleep = sleep
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ml-repo-sampler",
            }
        )
        # Sould probaly change the user agent at some point to not get flagged as a bot
        self.request_count = 0

    @property
    def token_count(self) -> int:
        return len(self._tokens)

    # -- token / pacing management ------------------------------------------------

    def _wait(self, seconds: float, reason: str) -> None:
        """Sleep, showing a countdown bar for long waits (rate limits) so the run does not look stuck."""
        if seconds < 10:
            self._sleep(seconds)
            return
        with bar(desc=reason, total=int(seconds), unit="s", leave=False) as countdown:
            remaining = seconds
            while remaining > 0:
                step = min(1.0, remaining)
                self._sleep(step)
                countdown.update(step)
                remaining -= step

    def _pick_token(self, resource: str) -> _TokenState:
        while True:
            now = time.time()
            n = len(self._tokens)
            for i in range(n):
                st = self._tokens[(self._next + i) % n]
                if st.available_at(resource) <= now:
                    self._next = (self._next + i + 1) % n  # round robin
                    return st
            wake = min(st.available_at(resource) for st in self._tokens) + 1
            wait = max(wake - now, 1.0)
            log.warning("No token available for '%s' (all %d rate limited); sleeping %.0fs", resource, n, wait)
            self._wait(wait, f"Rate limit ({resource})")

    def _throttle(self, resource: str) -> None:
        interval = self.cfg.core_min_interval if resource == "core" else self.cfg.search_min_interval
        if interval <= 0:
            return
        elapsed = time.time() - self._last_request.get(resource, 0.0)
        if elapsed < interval:
            self._sleep(interval - elapsed)
        self._last_request[resource] = time.time()

    def _secondary_limited(self, st: _TokenState, retry_after: str | None) -> float:
        """Bench a token hit by a secondary rate limit so the next request rotates to another one.

        If every token got ``secondary_strikes`` secondary limits within ``secondary_window``
        seconds, all of them are benched for ``secondary_cooldown`` seconds. Returns the bench time.
        """
        now = time.time()
        st.secondary_hits = [t for t in st.secondary_hits if now - t < self.cfg.secondary_window] + [now]
        if retry_after and retry_after.isdigit():
            wait = float(retry_after)
        else:
            wait = float(min(60 * len(st.secondary_hits), 600))
        st.blocked_until = max(st.blocked_until, now + wait)
        if all(
            sum(now - t < self.cfg.secondary_window for t in tok.secondary_hits) >= self.cfg.secondary_strikes
            for tok in self._tokens
        ):
            wait = self.cfg.secondary_cooldown
            log.warning("All %d token(s) keep hitting secondary rate limits; cooling down %.0fs",
                        len(self._tokens), wait)
            for tok in self._tokens:
                tok.blocked_until = max(tok.blocked_until, now + wait)
                tok.secondary_hits.clear()
        return wait

    @staticmethod
    def _update_limits(st: _TokenState, resp: requests.Response, fallback_resource: str) -> None:
        h = resp.headers
        if "X-RateLimit-Remaining" in h and "X-RateLimit-Reset" in h:
            resource = h.get("X-RateLimit-Resource", fallback_resource)
            try:
                st.limits[resource] = (int(h["X-RateLimit-Remaining"]), float(h["X-RateLimit-Reset"]))
            except ValueError:
                pass

    # -- requests -------------------------------------------------------------------

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any | None:
        """GET an API path. Returns parsed JSON, or None if the resource is unavailable (404/410/451/blocked)."""
        resp = self._request(path, params)
        return None if resp is None else resp.json()

    def count(self, path: str, params: dict[str, Any] | None = None) -> int | None:
        """Number of items of a paginated list endpoint (e.g. commits), in a single call.

        Asks for 1 item per page and reads the page number of the ``rel="last"`` Link header.
        Returns 0 for an empty repository (409), None if the resource is unavailable.
        """
        resp = self._request(path, {**(params or {}), "per_page": 1}, conflict_ok=True)
        if resp is None:
            return None
        if resp.status_code == 409:  # "Git Repository is empty"
            return 0
        for link in parse_header_links(resp.headers.get("Link", "")):
            if link.get("rel") == "last":
                match = re.search(r"[?&]page=(\d+)", link["url"])
                if match:
                    return int(match.group(1))
        return len(resp.json() or [])  # 0 or 1 item: no pagination links

    def _request(self, path: str, params: dict[str, Any] | None = None,
                 conflict_ok: bool = False) -> requests.Response | None:
        """GET with retries. Returns the 200 response (or the 409 one with ``conflict_ok``),
        None if the resource is unavailable."""
        resource = resource_for(path)
        url = path if path.startswith("http") else self.cfg.api_url.rstrip("/") + path
        attempt = 0
        while True:
            st = self._pick_token(resource)
            self._throttle(resource)
            try:
                resp = self.session.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {st.token}"},
                    timeout=self.cfg.timeout,
                )
            except requests.RequestException as e:
                attempt += 1
                if attempt > self.cfg.max_retries:
                    raise GitHubError(f"Network error on {path}: {e}") from e
                wait = min(2**attempt, 120)
                log.warning("Network error on %s (%s); retry %d in %ds", path, e, attempt, wait)
                self._wait(wait, "Network retry")
                continue

            self.request_count += 1
            self._update_limits(st, resp, resource)
            code = resp.status_code
            log.debug("GET %s %s -> %d", path, params or "", code)

            if code == 200 or (code == 409 and conflict_ok):
                return resp
            if code in (404, 410, 451):
                log.info("GET %s -> %d (%s)", path, code, _message(resp))
                return None
            if code == 401:
                log.error("Token %s rejected (401 Bad credentials); removing it from the pool", st.masked)
                self._tokens.remove(st)
                if not self._tokens:
                    raise GitHubError("All GitHub tokens were rejected (401)")
                self._next = 0
                continue
            if code == 422:
                raise InvalidQueryError(f"{path} {params}: {_message(resp)}")
            if code in (403, 429):
                msg = _message(resp)
                retry_after = resp.headers.get("Retry-After")
                if resp.headers.get("X-RateLimit-Remaining") == "0" and not retry_after:
                    # Primary limit: _pick_token waits for the reset. Guard against clock skew.
                    if st.available_at(resource) == 0.0:
                        self._sleep(5)
                    continue
                if retry_after or "rate limit" in msg.lower() or code == 429:
                    # No retry cap: _pick_token rotates to another token, or waits until one is free.
                    wait = self._secondary_limited(st, retry_after)
                    log.warning("Secondary rate limit on %s with token %s; benched for %.0fs", path, st.masked, wait)
                    continue
                log.info("GET %s -> 403 (%s)", path, msg)  # e.g. repository access blocked
                return None
            if code == 408 or code >= 500:  # request timeout / server error
                # If 408, should devide the number of elements per page (server cut the connection if the request take more than 10s on their side)
                attempt += 1
                if attempt > self.cfg.max_retries:
                    raise GitHubError(f"HTTP {code} on {path} after {attempt - 1} retries")
                wait = min(2**attempt, 120)
                log.warning("HTTP %d on %s; retry %d in %ds", code, path, attempt, wait)
                self._wait(wait, f"HTTP {code} retry")
                continue
            raise GitHubError(f"Unexpected HTTP {code} on {path}: {_message(resp)}")

    def rate_limits(self) -> list[dict[str, Any]]:
        """Query /rate_limit for every token (this endpoint does not consume quota)."""
        out = []
        for st in self._tokens:
            resp = self.session.get(
                self.cfg.api_url.rstrip("/") + "/rate_limit",
                headers={"Authorization": f"Bearer {st.token}"},
                timeout=self.cfg.timeout,
            )
            row: dict[str, Any] = {"token": st.masked, "status": resp.status_code}
            if resp.status_code == 200:
                res = resp.json().get("resources", {})
                print(res)
                for name in ("core", "search", "code_search"):
                    r = res.get(name, {})
                    row[name] = f"{r.get('remaining', '?')}/{r.get('limit', '?')}"
            else:
                row["error"] = _message(resp)
            out.append(row)
        return out
