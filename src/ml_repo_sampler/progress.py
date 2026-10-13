"""tqdm progress bars that cooperate with logging.

Console log records are written with ``tqdm.write`` so they appear above the bars
instead of breaking them. Bars are disabled globally with :func:`set_enabled`
(``--no-progress`` / ``[run] progress = false``) and automatically when stderr is
not a terminal (e.g. output redirected to a file, CI).
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from tqdm import tqdm

_enabled = True


def set_enabled(value: bool) -> None:
    global _enabled
    _enabled = value


def bar(**kwargs: Any) -> tqdm:
    """Create a progress bar; ``disable=None`` lets tqdm hide it when not attached to a TTY."""
    kwargs.setdefault("dynamic_ncols", True)
    kwargs.setdefault("file", sys.stderr)
    kwargs["disable"] = None if _enabled else True
    return tqdm(**kwargs)


class TqdmLoggingHandler(logging.StreamHandler):
    """Logging handler that prints through tqdm so active bars are redrawn below the message."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            tqdm.write(self.format(record), file=self.stream)
        except Exception:  # noqa: BLE001 - same contract as logging.Handler.emit
            self.handleError(record)
