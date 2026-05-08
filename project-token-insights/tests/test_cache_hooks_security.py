#!/usr/bin/env python3
"""Tests for cache reminder hook security and behavior.

Coverage:
  - _safe_state_path (all three hook files) — path traversal prevention
  - get_cached_tokens (cache-resume-detect) — transcript bounds check
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import helpers — hook files use hyphens, not valid identifiers
# ---------------------------------------------------------------------------

_HOOKS_DIR = (
    Path(__file__).resolve().parent.parent
    / "assets" / "cache-hooks"
)


def _load_hook(filename: str):
    """Load a hyphenated hook script as a module via importlib."""
    module_name = filename.replace("-", "_").removesuffix(".py")
    spec = importlib.util.spec_from_file_location(
        module_name, _HOOKS_DIR / filename
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Load once at module import so parametrize can reference the functions
_expiry_warn = _load_hook("cache-expiry-warn.py")
_resume_detect = _load_hook("cache-resume-detect.py")
_warn_stop = _load_hook("cache-warn-stop.py")

# ---------------------------------------------------------------------------
# Parametrize _safe_state_path across all three hook modules
# ---------------------------------------------------------------------------

_HOOK_MODULES = [
    pytest.param(_expiry_warn, id="cache-expiry-warn"),
    pytest.param(_resume_detect, id="cache-resume-detect"),
    pytest.param(_warn_stop, id="cache-warn-stop"),
]


# ---------------------------------------------------------------------------
# _safe_state_path — path traversal prevention
# ---------------------------------------------------------------------------

class TestSafeStatePath:
    """_safe_state_path must confine state files within cache_dir.

    A crafted session_id that traverses out of cache_dir would let an
    attacker read or overwrite arbitrary files (e.g. ~/.claude/settings.json).
    Returning None on traversal prevents that write/read from ever happening.
    """

    @pytest.mark.parametrize("mod", _HOOK_MODULES)
    def test_normal_session_id_returns_path_inside_cache_dir(self, mod, tmp_path):
        result = mod._safe_state_path(tmp_path, "", "abc123")
        assert result is not None
        assert result.is_relative_to(tmp_path)
        assert result.name == "abc123.json"

    @pytest.mark.parametrize("mod", _HOOK_MODULES)
    def test_normal_session_id_with_prefix(self, mod, tmp_path):
        result = mod._safe_state_path(tmp_path, "resume-pending-", "abc123")
        assert result is not None
        assert result.is_relative_to(tmp_path)
        assert result.name == "resume-pending-abc123.json"

    @pytest.mark.parametrize("mod", _HOOK_MODULES)
    def test_traversal_dotdot_returns_none(self, mod, tmp_path):
        # Prevents overwriting ~/.claude/settings.json via crafted session_id
        result = mod._safe_state_path(tmp_path, "", "../../.claude/settings")
        assert result is None

    @pytest.mark.parametrize("mod", _HOOK_MODULES)
    def test_traversal_dotdot_with_prefix_returns_none(self, mod, tmp_path):
        # "resume-pending-../../evil" resolves to <cache_dir>/evil — still inside,
        # because the prefix contributes a pseudo-directory level that absorbs one "..".
        # A genuine escape requires 3 levels of "../".  Verify the guard catches it.
        result = mod._safe_state_path(tmp_path, "resume-pending-", "../../../evil")
        assert result is None

    @pytest.mark.parametrize("mod", _HOOK_MODULES)
    def test_two_dotdot_with_prefix_stays_inside(self, mod, tmp_path):
        # "resume-pending-../../evil" lands at <cache_dir>/evil — guard correctly
        # allows it because the resolved path is still within cache_dir.
        result = mod._safe_state_path(tmp_path, "resume-pending-", "../../evil")
        assert result is not None
        assert result.is_relative_to(tmp_path)

    @pytest.mark.parametrize("mod", _HOOK_MODULES)
    def test_absolute_component_returns_none(self, mod, tmp_path):
        # On POSIX, joining an absolute string replaces the base path entirely
        result = mod._safe_state_path(tmp_path, "", "/etc/passwd")
        assert result is None

    @pytest.mark.parametrize("mod", _HOOK_MODULES)
    def test_null_byte_in_session_id_returns_none(self, mod, tmp_path):
        # Null bytes in filenames are rejected by the OS; resolve() raises on some
        # platforms and produces an out-of-bounds path on others.  Either way,
        # the result must be None or a valid in-bounds path — never a path outside.
        try:
            result = mod._safe_state_path(tmp_path, "", "abc\x00../../evil")
        except (ValueError, OSError):
            return  # raising is also an acceptable guard
        if result is not None:
            assert result.is_relative_to(tmp_path)

    @pytest.mark.parametrize("mod", _HOOK_MODULES)
    def test_empty_session_id_returns_path_inside_cache_dir(self, mod, tmp_path):
        # Empty session_id is weird but harmless — cache_dir/".json" stays inside.
        result = mod._safe_state_path(tmp_path, "", "")
        assert result is not None
        assert result.is_relative_to(tmp_path)

    @pytest.mark.parametrize("mod", _HOOK_MODULES)
    def test_returned_path_has_json_suffix(self, mod, tmp_path):
        result = mod._safe_state_path(tmp_path, "pfx-", "sess99")
        assert result is not None
        assert result.suffix == ".json"


# ---------------------------------------------------------------------------
# get_cached_tokens — transcript bounds check in cache-resume-detect.py
# ---------------------------------------------------------------------------

class TestGetCachedTokens:
    """get_cached_tokens must refuse to read files outside ~/.claude.

    If the bounds check is bypassed, a malicious transcript_path could read
    arbitrary files on disk (e.g. SSH keys) by supplying an attacker-controlled
    path as the transcript location.
    """

    def _write_transcript(self, path: Path, cache_creation: int, cache_read: int) -> None:
        """Write a minimal JSONL transcript with one assistant turn."""
        entry = {
            "message": {
                "role": "assistant",
                "usage": {
                    "cache_creation_input_tokens": cache_creation,
                    "cache_read_input_tokens": cache_read,
                },
            }
        }
        path.write_text(json.dumps(entry) + "\n")

    def test_path_inside_claude_dir_returns_tokens(self, tmp_path, monkeypatch):
        # Redirect _CLAUDE_DIR so we don't depend on the real ~/.claude existing
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        monkeypatch.setattr(_resume_detect, "_CLAUDE_DIR", claude_dir)

        transcript = claude_dir / "projects" / "session.jsonl"
        transcript.parent.mkdir(parents=True)
        self._write_transcript(transcript, cache_creation=5000, cache_read=1000)

        result = _resume_detect.get_cached_tokens(str(transcript))
        assert result == 6000  # cache_creation + cache_read

    def test_path_outside_claude_dir_returns_zero(self, tmp_path, monkeypatch):
        # Prevents reading /tmp/evil.jsonl (or any attacker-supplied path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        monkeypatch.setattr(_resume_detect, "_CLAUDE_DIR", claude_dir)

        evil = tmp_path / "evil.jsonl"
        self._write_transcript(evil, cache_creation=9999, cache_read=9999)

        result = _resume_detect.get_cached_tokens(str(evil))
        assert result == 0

    def test_symlink_escaping_claude_dir_returns_zero(self, tmp_path, monkeypatch):
        # Symlink inside ~/.claude pointing outside must not be followed
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        outside = tmp_path / "real_data.jsonl"
        self._write_transcript(outside, cache_creation=8888, cache_read=0)

        link = claude_dir / "escape_link.jsonl"
        link.symlink_to(outside)
        monkeypatch.setattr(_resume_detect, "_CLAUDE_DIR", claude_dir)

        result = _resume_detect.get_cached_tokens(str(link))
        assert result == 0

    def test_nonexistent_path_inside_claude_dir_returns_zero(self, tmp_path, monkeypatch):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        monkeypatch.setattr(_resume_detect, "_CLAUDE_DIR", claude_dir)

        missing = claude_dir / "no-such-session.jsonl"
        result = _resume_detect.get_cached_tokens(str(missing))
        assert result == 0

    def test_empty_transcript_returns_zero(self, tmp_path, monkeypatch):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        monkeypatch.setattr(_resume_detect, "_CLAUDE_DIR", claude_dir)

        transcript = claude_dir / "empty.jsonl"
        transcript.write_text("")
        result = _resume_detect.get_cached_tokens(str(transcript))
        assert result == 0

    def test_returns_last_assistant_turn_tokens(self, tmp_path, monkeypatch):
        # get_cached_tokens should track the latest value, not the first
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        monkeypatch.setattr(_resume_detect, "_CLAUDE_DIR", claude_dir)

        transcript = claude_dir / "multi_turn.jsonl"
        lines = [
            json.dumps({"message": {"role": "assistant", "usage": {
                "cache_creation_input_tokens": 100, "cache_read_input_tokens": 0
            }}}),
            json.dumps({"message": {"role": "user", "content": "hello"}}),
            json.dumps({"message": {"role": "assistant", "usage": {
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 4000
            }}}),
        ]
        transcript.write_text("\n".join(lines) + "\n")

        result = _resume_detect.get_cached_tokens(str(transcript))
        assert result == 4000  # last assistant turn, not first


# ---------------------------------------------------------------------------
# _safe_state_path — exception resilience (OSError / RuntimeError)
# ---------------------------------------------------------------------------

class TestSafeStatePathExceptions:
    """_safe_state_path must return None (not crash) for any malformed input."""

    @pytest.mark.parametrize("mod", _HOOK_MODULES)
    def test_returns_none_not_raises_on_oserror(self, mod, tmp_path, monkeypatch):
        """Simulate Path.resolve() raising OSError (e.g., path too long)."""
        import pathlib

        def bad_resolve(self, strict=False):
            raise OSError("simulated OS-level resolve failure")

        monkeypatch.setattr(pathlib.Path, "resolve", bad_resolve)
        result = mod._safe_state_path(tmp_path, "", "abc123")
        assert result is None

    @pytest.mark.parametrize("mod", _HOOK_MODULES)
    def test_returns_none_not_raises_on_runtime_error(self, mod, tmp_path, monkeypatch):
        """Simulate Path.resolve() raising RuntimeError (e.g., infinite symlink loop)."""
        import pathlib

        def bad_resolve(self, strict=False):
            raise RuntimeError("simulated infinite symlink loop")

        monkeypatch.setattr(pathlib.Path, "resolve", bad_resolve)
        result = mod._safe_state_path(tmp_path, "", "abc123")
        assert result is None


# ---------------------------------------------------------------------------
# get_cached_tokens — None and encoding edge cases
# ---------------------------------------------------------------------------

class TestGetCachedTokensEdgeCases:
    def test_none_transcript_path_returns_zero(self, monkeypatch, tmp_path):
        """get_cached_tokens(None) must not raise TypeError."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        monkeypatch.setattr(_resume_detect, "_CLAUDE_DIR", claude_dir)
        # None passed as transcript_path — cast to str("") then bounds check returns 0
        result = _resume_detect.get_cached_tokens(None)  # type: ignore[arg-type]
        assert result == 0

    def test_utf8_transcript_is_read_correctly(self, tmp_path, monkeypatch):
        """Transcripts with non-ASCII content must not crash the reader."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        monkeypatch.setattr(_resume_detect, "_CLAUDE_DIR", claude_dir)

        transcript = claude_dir / "utf8_session.jsonl"
        entry = {
            "message": {
                "role": "assistant",
                "usage": {
                    "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": 50,
                },
                "content": "こんにちは世界",  # non-ASCII
            }
        }
        transcript.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        result = _resume_detect.get_cached_tokens(str(transcript))
        assert result == 150


# ---------------------------------------------------------------------------
# _cache_warn_dir — T10 迁移后的路径断言
# ---------------------------------------------------------------------------

class TestCacheWarnDir:
    """T10 把 cache-warn 目录从 ~/.claude-memory/cache-warn 迁到
    $CLAUDE_PROJECT_DIR/.project-token-insights/cache-warn。三个 hook
    必须一致使用 _cache_warn_dir()，且根据环境变量定位项目。"""

    @pytest.mark.parametrize("mod", _HOOK_MODULES)
    def test_uses_project_data_dir(self, mod, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        result = mod._cache_warn_dir()
        expected = tmp_path.resolve() / ".project-token-insights" / "cache-warn"
        assert result == expected

    @pytest.mark.parametrize("mod", _HOOK_MODULES)
    def test_fallback_to_cwd_when_env_missing(self, mod, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        result = mod._cache_warn_dir()
        expected = tmp_path.resolve() / ".project-token-insights" / "cache-warn"
        assert result == expected

    @pytest.mark.parametrize("mod", _HOOK_MODULES)
    def test_legacy_claude_memory_path_not_used(self, mod, tmp_path, monkeypatch):
        """回归：严禁再出现 ~/.claude-memory/cache-warn 的硬编码。"""
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        result = mod._cache_warn_dir()
        assert ".claude-memory" not in str(result)
        assert ".project-token-insights" in str(result)

    @pytest.mark.parametrize("mod", _HOOK_MODULES)
    def test_no_module_level_CACHE_WARN_DIR_constant(self, mod):
        """T10 移除了模块级常量；保留是路径回归隐患。"""
        assert not hasattr(mod, "CACHE_WARN_DIR")


# ---------------------------------------------------------------------------
# Stop hook captures last-turn cache size (new: concrete rebuild cost)
# ---------------------------------------------------------------------------

class TestStopHookCachedTokenCapture:
    """cache-warn-stop.py 需要把最近一轮 assistant 的 cache_creation+cache_read
    存进 state.last_cached_tokens；cache-expiry-warn.py 再用它生成具体数字的
    idle-gap 警告。若 transcript 缺失 / 出界 / 为 0，应保留之前已知的值而非清零。"""

    def _seed_transcript(self, path: Path, tokens: int) -> None:
        entry = {
            "message": {
                "role": "assistant",
                "usage": {
                    "cache_creation_input_tokens": tokens,
                    "cache_read_input_tokens": 0,
                },
            }
        }
        path.write_text(json.dumps(entry) + "\n", encoding="utf-8")

    def _run_stop(self, tmp_path, monkeypatch, payload):
        """Invoke cache-warn-stop.main() directly; returns the state JSON dict."""
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        # Point _CLAUDE_DIR at tmp_path/.claude so transcript bounds pass
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(exist_ok=True)
        monkeypatch.setattr(_warn_stop, "_CLAUDE_DIR", claude_dir)

        import io
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        _warn_stop.main()

        state_file = (tmp_path / ".project-token-insights" / "cache-warn"
                      / f"{payload['session_id']}.json")
        if not state_file.exists():
            return None
        return json.loads(state_file.read_text())

    def test_records_tokens_from_transcript(self, tmp_path, monkeypatch):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        transcript = claude_dir / "session.jsonl"
        self._seed_transcript(transcript, tokens=370_514)

        state = self._run_stop(tmp_path, monkeypatch, {
            "session_id": "s1",
            "transcript_path": str(transcript),
        })
        assert state is not None
        assert state["last_cached_tokens"] == 370_514
        assert "last_stop_time" in state

    def test_missing_transcript_defaults_to_zero(self, tmp_path, monkeypatch):
        state = self._run_stop(tmp_path, monkeypatch, {"session_id": "s2"})
        assert state is not None
        assert state["last_cached_tokens"] == 0

    def test_transcript_outside_claude_dir_returns_zero(self, tmp_path, monkeypatch):
        evil = tmp_path / "evil.jsonl"
        self._seed_transcript(evil, tokens=999_999)
        state = self._run_stop(tmp_path, monkeypatch, {
            "session_id": "s3",
            "transcript_path": str(evil),
        })
        assert state["last_cached_tokens"] == 0  # bounds check blocked it

    def test_preserves_prior_value_when_new_read_is_zero(self, tmp_path, monkeypatch):
        """一次性读不到（hook 早于 transcript 落盘）不应抹掉已知值。"""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        transcript = claude_dir / "session.jsonl"
        self._seed_transcript(transcript, tokens=123_456)

        # First call: populate state with real number
        first = self._run_stop(tmp_path, monkeypatch, {
            "session_id": "s4", "transcript_path": str(transcript),
        })
        assert first["last_cached_tokens"] == 123_456

        # Second call: no transcript_path → should keep 123_456, not overwrite with 0
        second = self._run_stop(tmp_path, monkeypatch, {"session_id": "s4"})
        assert second["last_cached_tokens"] == 123_456

    def test_keeps_warned_gaps_across_rewrites(self, tmp_path, monkeypatch):
        """Existing warned_gaps must survive the new field addition."""
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        state_dir = tmp_path / ".project-token-insights" / "cache-warn"
        state_dir.mkdir(parents=True)
        (state_dir / "s5.json").write_text(json.dumps({
            "session_id": "s5",
            "last_stop_time": "2026-01-01T00:00:00+00:00",
            "warned_gaps": ["bucket-1"],
            "last_cached_tokens": 42,
        }))
        state = self._run_stop(tmp_path, monkeypatch, {"session_id": "s5"})
        assert state["warned_gaps"] == ["bucket-1"]
        assert state["last_cached_tokens"] == 42  # preserved


# ---------------------------------------------------------------------------
# Expiry warn embeds concrete token count when available
# ---------------------------------------------------------------------------

class TestExpiryWarnTokenCount:
    """cache-expiry-warn.py 的 idle-gap 警告文案：当 state.last_cached_tokens
    >= 1000 时，应该带“约 370k token 缓存上下文”之类具体数字；<1000 或缺字段时
    回退到通用文案。"""

    def _seed_state(self, tmp_path, monkeypatch, last_cached_tokens, gap_minutes=10):
        from datetime import datetime, timedelta, timezone
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        state_dir = tmp_path / ".project-token-insights" / "cache-warn"
        state_dir.mkdir(parents=True, exist_ok=True)
        old = (datetime.now(timezone.utc) - timedelta(minutes=gap_minutes)).isoformat()
        payload = {
            "session_id": "sx",
            "last_stop_time": old,
            "warned_gaps": [],
        }
        if last_cached_tokens is not None:
            payload["last_cached_tokens"] = last_cached_tokens
        (state_dir / "sx.json").write_text(json.dumps(payload))

    def _run_expiry(self, tmp_path, monkeypatch, prompt="hello"):
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
            "session_id": "sx", "prompt": prompt,
        })))

        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _expiry_warn.main()
        out = buf.getvalue().strip()
        return json.loads(out) if out else {}

    def test_warning_includes_token_count_when_large(self, tmp_path, monkeypatch):
        self._seed_state(tmp_path, monkeypatch, last_cached_tokens=370_514, gap_minutes=10)
        result = self._run_expiry(tmp_path, monkeypatch)
        assert result.get("decision") == "block"
        reason = result.get("reason", "")
        assert "约 371k token 缓存上下文" in reason, reason
        assert "从头重建" in reason

    def test_warning_falls_back_when_tokens_missing(self, tmp_path, monkeypatch):
        self._seed_state(tmp_path, monkeypatch, last_cached_tokens=None, gap_minutes=10)
        result = self._run_expiry(tmp_path, monkeypatch)
        assert result.get("decision") == "block"
        reason = result.get("reason", "")
        assert "完整上下文" in reason
        assert "token 缓存上下文" not in reason

    def test_warning_falls_back_when_tokens_below_threshold(self, tmp_path, monkeypatch):
        # 999 tokens 没意义，应当不拼接具体数字
        self._seed_state(tmp_path, monkeypatch, last_cached_tokens=999, gap_minutes=10)
        result = self._run_expiry(tmp_path, monkeypatch)
        assert result.get("decision") == "block"
        reason = result.get("reason", "")
        assert "完整上下文" in reason
        assert "token 缓存上下文" not in reason

    def test_resume_warning_is_chinese_and_includes_token_count(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        state_dir = tmp_path / ".project-token-insights" / "cache-warn"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "resume-pending-sx.json").write_text(json.dumps({
            "cached_tokens": 123_456,
        }))
        result = self._run_expiry(tmp_path, monkeypatch)
        assert result.get("decision") == "block"
        reason = result.get("reason", "")
        assert "检测到恢复会话" in reason
        assert "约 123k token 缓存上下文" in reason
        assert "/compact" in reason

    def test_dedupe_still_works_with_token_field(self, tmp_path, monkeypatch):
        """重复提交同一 gap 仍应只 block 一次；新字段不破坏去重。"""
        self._seed_state(tmp_path, monkeypatch, last_cached_tokens=200_000, gap_minutes=10)
        first = self._run_expiry(tmp_path, monkeypatch)
        assert first.get("decision") == "block"
        second = self._run_expiry(tmp_path, monkeypatch)
        assert second.get("continue") is True

    def test_token_count_preserved_when_state_rewrites(self, tmp_path, monkeypatch):
        """首次 block 会回写 state 加入 warned_gaps；last_cached_tokens 不得丢失。"""
        self._seed_state(tmp_path, monkeypatch, last_cached_tokens=200_000, gap_minutes=10)
        self._run_expiry(tmp_path, monkeypatch)  # trigger rewrite
        state_file = tmp_path / ".project-token-insights" / "cache-warn" / "sx.json"
        saved = json.loads(state_file.read_text())
        assert saved["last_cached_tokens"] == 200_000
        assert saved["warned_gaps"], "gap bucket should be recorded"


class TestFormatTokens:
    """Sanity check on the pretty-printer used by expiry-warn."""

    @pytest.mark.parametrize("n,expected", [
        (0, "0"),
        (999, "999"),
        (1_000, "1k"),
        (12_345, "12k"),
        (370_514, "371k"),
    ])
    def test_format(self, n, expected):
        assert _expiry_warn._format_tokens(n) == expected
