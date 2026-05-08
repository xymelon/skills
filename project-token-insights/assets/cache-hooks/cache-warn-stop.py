#!/usr/bin/env python3
"""
Stop hook: record the time Claude last finished responding + last-turn cache size.

Called after every Claude turn. Writes
$CLAUDE_PROJECT_DIR/.project-token-insights/cache-warn/<session_id>.json
with:
  - last_stop_time: ISO timestamp (used by cache-expiry-warn.py for idle-gap detection)
  - last_cached_tokens: cache_creation + cache_read from the final assistant turn,
                       read from `transcript_path` supplied in the hook payload;
                       used by cache-expiry-warn.py to print a concrete rebuild
                       cost ("约 370k token 缓存上下文") instead of a generic message.

Preserves any existing warned_gaps list so the warn-once-per-gap logic survives
across multiple turns in the same session.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_CLAUDE_DIR = Path.home() / ".claude"


def _cache_warn_dir() -> Path:
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    return Path(root).resolve() / ".project-token-insights" / "cache-warn"


def _safe_state_path(cache_dir: Path, prefix: str, session_id: str) -> Path | None:
    """Return resolved path only if it stays within cache_dir; else None."""
    try:
        candidate = (cache_dir / f"{prefix}{session_id}.json").resolve()
        candidate.relative_to(cache_dir.resolve())
        return candidate
    except (ValueError, OSError, RuntimeError):
        return None


def get_cached_tokens(transcript_path) -> int:
    """Read last assistant usage from transcript JSONL; return 0 on any failure
    or when the path escapes ~/.claude. Mirrors cache-resume-detect.get_cached_tokens
    (hooks are installed as flat project-local files — no shared import)."""
    path = Path(str(transcript_path or ""))
    try:
        resolved = path.resolve()
        resolved.relative_to(_CLAUDE_DIR.resolve())
    except (ValueError, OSError, RuntimeError):
        return 0
    if not path.exists():
        return 0

    tokens = 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = entry.get("message", {})
            if msg.get("role") == "assistant":
                usage = msg.get("usage", {})
                t = int(usage.get("cache_creation_input_tokens", 0) or 0) + int(
                    usage.get("cache_read_input_tokens", 0) or 0
                )
                if t:
                    tokens = t  # latest non-zero assistant usage wins
    except OSError:
        pass
    return tokens


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        session_id = data.get("session_id", "")
        if not session_id:
            return

        cache_dir = _cache_warn_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)
        state_path = _safe_state_path(cache_dir, "", session_id)
        if state_path is None:
            return

        warned_gaps: list = []
        prior_cached_tokens = 0
        if state_path.exists():
            try:
                existing = json.loads(state_path.read_text())
                warned_gaps = existing.get("warned_gaps", [])
                prior_cached_tokens = int(existing.get("last_cached_tokens", 0) or 0)
            except (json.JSONDecodeError, OSError, ValueError):
                pass

        transcript_path = data.get("transcript_path", "")
        this_turn = get_cached_tokens(transcript_path) if transcript_path else 0
        # If transcript read yielded zero (missing/out-of-bounds/empty), keep the
        # prior value so a late-session flake does not blank out useful info.
        last_cached_tokens = this_turn if this_turn > 0 else prior_cached_tokens

        state_path.write_text(json.dumps({
            "session_id": session_id,
            "last_stop_time": datetime.now(timezone.utc).isoformat(),
            "warned_gaps": warned_gaps,
            "last_cached_tokens": last_cached_tokens,
        }))
    except Exception:
        pass  # Never block the Stop hook


if __name__ == "__main__":
    main()
