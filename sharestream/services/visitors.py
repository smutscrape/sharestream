"""First-visit logging.

Emits one INFO log the first time a given (ip, share_id) pair is seen, so share
traffic is observable without spamming a line per request. The dedup set is
per-process and in-memory (a benign analytics aid); housed here so the mutable
state stays out of routers.
"""
from __future__ import annotations

import logging
from threading import Lock

logger = logging.getLogger(__name__)

_seen: set[tuple[str, str]] = set()
_lock = Lock()


def log_first_visit(request, share_id: str, kind: str = "share") -> None:
    """Log a first visit for (client ip, share_id). ``kind='tag'`` tweaks wording."""
    if request is None:
        return
    ip = request.client.host
    with _lock:
        if (ip, share_id) not in _seen:
            if kind == "tag":
                logger.info(f"Visitor {ip} requested tag share {share_id}")
            else:
                logger.info(f"Visitor {ip} requested {share_id}")
            _seen.add((ip, share_id))
