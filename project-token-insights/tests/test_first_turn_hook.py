#!/usr/bin/env python3
"""first-turn hook behavior.

Coverage:
  - resume SessionStart is ignored; cache-bust hooks own that case
  - over-budget warnings are shown as a visible UserPromptSubmit block
  - warning text includes concrete 2.1-2.6 optimization directions
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
from pathlib import Path


_HOOK_PATH = (
    Path(__file__).resolve().parent.parent
    / "assets" / "first-turn-hooks" / "first-turn-budget-check.py"
)


def _load_hook():
    spec = importlib.util.spec_from_file_location("first_turn_budget_check", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_hook = _load_hook()


def _seed_baseline(project_root: Path, baseline: dict) -> None:
    data_dir = project_root / ".project-token-insights"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "first-turn-baseline.json").write_text(
        json.dumps(baseline),
        encoding="utf-8",
    )


def _run_hook(tmp_path: Path, monkeypatch, payload: dict) -> str:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _hook.main()
    assert rc == 0
    return buf.getvalue()


def test_resume_session_is_ignored(tmp_path, monkeypatch):
    _seed_baseline(tmp_path, {"first_turn_total": 50_000})

    out = _run_hook(tmp_path, monkeypatch, {
        "session_id": "resume-session",
        "source": "resume",
    })

    assert out == ""
    assert not (tmp_path / ".project-token-insights" / "first-turn-warned-resume-session").exists()
    assert not (tmp_path / ".project-token-insights" / "first-turn-pending-resume-session.json").exists()


def test_warning_includes_directional_optimization_hints(tmp_path, monkeypatch):
    _seed_baseline(tmp_path, {
        "project_claude_md": 5_000,
        "agent_definitions": 4_000,
        "tool_schemas": 9_000,
        "auto_memory": 1_500,
        "first_turn_total": 30_000,
    })

    out = _run_hook(tmp_path, monkeypatch, {
        "session_id": "cold-session",
        "source": "startup",
    })

    pending = tmp_path / ".project-token-insights" / "first-turn-pending-cold-session.json"
    assert out == ""
    assert pending.exists()

    out = _run_hook(tmp_path, monkeypatch, {
        "session_id": "cold-session",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "hi",
    })

    payload = json.loads(out)
    assert payload["decision"] == "block"
    text = payload["reason"]
    assert "首轮 token 超阈预警" in text
    assert "2.1 清理项目 CLAUDE.md" in text
    assert "2.2/2.3 清理 Agent" in text
    assert "2.4 禁用 Auto memory" in text
    assert "2.5 控制冷门工具" in text
    assert "首轮组件基线与优化报告" in text
    assert "再次发送原 prompt 继续" in text
    assert (tmp_path / ".project-token-insights" / "first-turn-warned-cold-session").exists()
    assert not pending.exists()

    out = _run_hook(tmp_path, monkeypatch, {
        "session_id": "cold-session",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "hi",
    })
    assert json.loads(out)["continue"] is True


def test_project_local_threshold_override_is_used(tmp_path, monkeypatch):
    _seed_baseline(tmp_path, {
        "project_claude_md": 5_000,
        "first_turn_total": 20_000,
    })
    (tmp_path / ".project-token-insights" / "first-turn-budget.json").write_text(
        json.dumps({
            "project_claude_md": 9_999,
            "first_turn_total": 99_999,
        }),
        encoding="utf-8",
    )

    out = _run_hook(tmp_path, monkeypatch, {
        "session_id": "below-overridden-threshold",
        "source": "startup",
    })

    assert out == ""
    assert not (tmp_path / ".project-token-insights" / "first-turn-warned-below-overridden-threshold").exists()
    assert not (tmp_path / ".project-token-insights" / "first-turn-pending-below-overridden-threshold.json").exists()


def test_system_prompt_submit_does_not_consume_pending_warning(tmp_path, monkeypatch):
    _seed_baseline(tmp_path, {"agent_definitions": 10_000})

    _run_hook(tmp_path, monkeypatch, {
        "session_id": "system-prefix-session",
        "source": "startup",
    })

    pending = tmp_path / ".project-token-insights" / "first-turn-pending-system-prefix-session.json"
    assert pending.exists()

    out = _run_hook(tmp_path, monkeypatch, {
        "session_id": "system-prefix-session",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "<task-notification>done</task-notification>",
    })

    assert json.loads(out)["continue"] is True
    assert pending.exists()
    assert not (tmp_path / ".project-token-insights" / "first-turn-warned-system-prefix-session").exists()
