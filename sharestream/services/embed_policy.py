"""Social-embed (og:video) policy decisions.

Decides whether a share's og:video should be the FULL video or the short Stash
preview clip, combining the per-share override with the configured defaults.
"""
from __future__ import annotations

from sharestream.config import (
    EMBED_MAX_FULL_DURATION,
    EMBED_MAX_FULL_SIZE_MB,
    EMBED_MODE,
    VALID_EMBED_MODES,
)


def normalize_embed_mode(mode):
    """Return a valid per-share embed mode, or None meaning 'use config default'."""
    if not mode:
        return None
    m = str(mode).strip().lower()
    return m if m in VALID_EMBED_MODES else None


def should_embed_full(share_mode, duration_seconds, size_bytes) -> bool:
    """Decide whether the og:video should be the FULL video (True) or the
    short preview clip (False), given the per-share override (or None for the
    configured default) and the video's duration/size."""
    mode = normalize_embed_mode(share_mode) or EMBED_MODE
    if mode == 'full':
        return True
    if mode == 'preview':
        return False

    def _num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    duration_seconds = _num(duration_seconds)
    size_bytes = _num(size_bytes)

    # dynamic: embed full only when small on BOTH configured axes. If a limit
    # is set but the value is unknown, err toward the (smaller) preview.
    if EMBED_MAX_FULL_DURATION is not None:
        if duration_seconds is None or duration_seconds > EMBED_MAX_FULL_DURATION:
            return False
    if EMBED_MAX_FULL_SIZE_MB is not None:
        if size_bytes is None or size_bytes > EMBED_MAX_FULL_SIZE_MB * 1024 * 1024:
            return False
    return True
