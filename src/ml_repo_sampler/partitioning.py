"""Adaptive partitioning of search queries to get past GitHub's 1000-results cap.

A query whose ``total_count`` is above the cap is split in two on a qualifier range
(``size:`` for code search, ``created:``/``pushed:``/``stars:``/``forks:``/``size:`` for
repository search), recursively, until every slice returns at most 1000 results or cannot
be split any more (a single byte, star or day).

Numeric ranges are split at their geometric midpoint, because file sizes and star counts
are heavy-tailed: a linear split of ``size:0..1000000`` would leave almost every file in the
first half. Date ranges are split at their linear midpoint.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any

# field -> search types that support it
FIELDS: dict[str, tuple[str, ...]] = {
    "size": ("code", "repositories"),
    "stars": ("repositories",),
    "forks": ("repositories",),
    "created": ("repositories",),
    "pushed": ("repositories",),
}
DATE_FIELDS = ("created", "pushed")
DEFAULT_FIELD = {"code": "size", "repositories": "created"}
FIRST_GITHUB_DAY = date(2008, 1, 1)


def default_range(field: str, query_type: str) -> tuple[Any, Any]:
    if field in DATE_FIELDS:
        return FIRST_GITHUB_DAY, date.today()
    if field == "size":
        # code: file size in bytes (files > 384 KB are not indexed); repositories: size in KB
        return (0, 1_000_000) if query_type == "code" else (0, 100_000_000)
    return 0, 10_000_000  # stars, forks


@dataclass(frozen=True)
class Slice:
    """An inclusive range ``lo..hi`` of one qualifier. Dates are stored as ordinals."""

    field: str
    lo: int
    hi: int

    @classmethod
    def root(cls, field: str, lo: Any, hi: Any) -> Slice:
        if field in DATE_FIELDS:
            return cls(field, _to_date(lo).toordinal(), _to_date(hi).toordinal())
        return cls(field, int(lo), int(hi))

    def _fmt(self, v: int) -> str:
        return date.fromordinal(v).isoformat() if self.field in DATE_FIELDS else str(v)

    @property
    def qualifier(self) -> str:
        return f"{self.field}:{self._fmt(self.lo)}..{self._fmt(self.hi)}"

    def split(self) -> tuple[Slice, Slice] | None:
        """Two halves covering the same range, or None if the range is a single value."""
        if self.lo >= self.hi:
            return None
        if self.field in DATE_FIELDS:
            mid = (self.lo + self.hi) // 2
        else:
            mid = int(math.sqrt((self.lo + 1) * (self.hi + 1))) - 1
        mid = min(max(mid, self.lo), self.hi - 1)
        return Slice(self.field, self.lo, mid), Slice(self.field, mid + 1, self.hi)


def _to_date(v: Any) -> date:
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v))


def validate(field: str, query_type: str, rng: list[Any] | None, text: str) -> None:
    """Raise ValueError if ``field``/``rng`` cannot partition a query of this type and text."""
    if field not in FIELDS:
        raise ValueError(f"auto_partition must be true/false or one of {sorted(FIELDS)}, got {field!r}")
    if query_type not in FIELDS[field]:
        raise ValueError(f"auto_partition = {field!r} is not supported by {query_type} search "
                         f"(supported: {[f for f, t in FIELDS.items() if query_type in t]})")
    if f"{field}:" in text:
        raise ValueError(f"the query or its partitions already use '{field}:'; remove it and set "
                         f"auto_partition_range instead")
    if rng is not None:
        if not isinstance(rng, list) or len(rng) != 2:
            raise ValueError("auto_partition_range must be a list [low, high]")
        root = Slice.root(field, *rng)  # raises ValueError on bad values
        if root.lo > root.hi:
            raise ValueError("auto_partition_range: low must be <= high")
