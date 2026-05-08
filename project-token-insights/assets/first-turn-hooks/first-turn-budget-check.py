#!/usr/bin/env python3
"""SessionStart/UserPromptSubmit hook: project-token-insights 首轮 token 预算预警。

基于上一次 `first_turn_breakdown.py` 写出的首轮组件估算基线，
与本插件内置/用户覆盖的阈值配置对比；新 session 超阈时先在
SessionStart 写 pending，再在第一条 UserPromptSubmit 前台 block 一次。
resume 由 cache-bust hook 处理，本 hook 跳过。每个 session 仅提示一次。

数据来源：
  - 阈值：内置默认值；可用项目 `.project-token-insights/first-turn-budget.json`
          或 ${CLAUDE_PLUGIN_ROOT}/skills/project-token-insights/config/first-turn-budget.json 覆盖
  - 基线：$CLAUDE_PROJECT_DIR/.project-token-insights/first-turn-baseline.json
  - pending：$CLAUDE_PROJECT_DIR/.project-token-insights/first-turn-pending-<session_id>.json
  - 去重：$CLAUDE_PROJECT_DIR/.project-token-insights/first-turn-warned-<session_id>

任何异常均静默；只有明确超阈的第一条人工 prompt 会被 block 一次。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PLUGIN_ROOT_ENV = "CLAUDE_PLUGIN_ROOT"
PROJECT_DIR_ENV = "CLAUDE_PROJECT_DIR"
DATA_DIRNAME = ".project-token-insights"
BASELINE_FILENAME = "first-turn-baseline.json"
CONFIG_REL = Path("skills") / "project-token-insights" / "config" / "first-turn-budget.json"
SKILL_CONFIG_REL = Path("config") / "first-turn-budget.json"
PROJECT_CONFIG_FILENAME = "first-turn-budget.json"
PENDING_PREFIX = "first-turn-pending-"
WARNED_PREFIX = "first-turn-warned-"

_SYSTEM_PREFIXES = (
    "<task-notification>",
    "<local-command-caveat>",
    "<command-name>",
    "<command-message>",
)

COMPONENT_LABELS = {
    "global_claude_md": "全局 CLAUDE.md",
    "project_claude_md": "项目 CLAUDE.md",
    "plugin_descriptions": "插件描述汇总",
    "skill_descriptions": "Skill 描述汇总",
    "agent_definitions": "Agent 定义汇总",
    "tool_schemas": "工具 schema 汇总",
    "auto_memory": "Auto memory 注入",
    "first_turn_total": "首轮 total",
}

ACTION_HINTS = {
    "global_claude_md": "2.1 清理全局 CLAUDE.md：删除重复规则，把长示例挪到外部文档。",
    "project_claude_md": "2.1 清理项目 CLAUDE.md：只保留当前项目必需规则。",
    "plugin_descriptions": "2.1 清理插件：明确不用的插件删除，偶尔用的插件改为特定项目启用。",
    "skill_descriptions": "2.1 清理 Skills：禁用隐式触发或缩短 description / when_to_use。",
    "agent_definitions": "2.2/2.3 清理 Agent：删除未使用 agent，并保持实验 Agent teams 关闭。",
    "tool_schemas": "2.5 控制冷门工具：启动时用 `--disallowedTools NotebookEdit CronCreate ...` 直接裁掉 schema，或 `--tools` 指定白名单。",
    "auto_memory": "2.4 禁用 Auto memory：项目 settings 设置 autoMemoryEnabled=false，或设置 CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 后重启。",
    "first_turn_total": "2.6 先看 Top 3：运行 /project-token-insights 定位最大组件。",
}


def _fallback_config() -> dict[str, int]:
    return {
        "global_claude_md": 1500,
        "project_claude_md": 3000,
        "plugin_descriptions": 2500,
        "skill_descriptions": 4000,
        "agent_definitions": 3000,
        "tool_schemas": 8000,
        "auto_memory": 1000,
        "first_turn_total": 25000,
    }


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _resolve_config() -> dict[str, int]:
    defaults = _fallback_config()
    candidates: list[Path] = []
    project_override = _project_root() / DATA_DIRNAME / PROJECT_CONFIG_FILENAME
    candidates.append(project_override)
    root = os.environ.get(PLUGIN_ROOT_ENV)
    if root:
        plugin_root = Path(root)
        candidates.append(plugin_root / CONFIG_REL)
        candidates.append(plugin_root / SKILL_CONFIG_REL)
    for candidate in candidates:
        override = _read_json(candidate)
        if not isinstance(override, dict):
            continue
        for k, v in override.items():
            if isinstance(v, (int, float)):
                defaults[k] = int(v)
    return defaults


def _project_root() -> Path:
    return Path(os.environ.get(PROJECT_DIR_ENV) or os.getcwd()).resolve()


def _session_id_from(payload: dict) -> str:
    for key in ("session_id", "sessionId"):
        sid = payload.get(key)
        if isinstance(sid, str) and sid:
            return sid
    return "unknown"


def _safe_state_path(data_dir: Path, prefix: str, session_id: str, suffix: str = "") -> Path | None:
    try:
        candidate = (data_dir / f"{prefix}{session_id}{suffix}").resolve()
        candidate.relative_to(data_dir.resolve())
        return candidate
    except (ValueError, OSError, RuntimeError):
        return None


def _collect_breaches(baseline: dict, thresholds: dict) -> list[tuple[str, str, int, int]]:
    breaches: list[tuple[str, str, int, int]] = []
    for key, label in COMPONENT_LABELS.items():
        est = baseline.get(key)
        lim = thresholds.get(key)
        if not isinstance(est, (int, float)) or not isinstance(lim, (int, float)):
            continue
        est_i, lim_i = int(est), int(lim)
        if lim_i > 0 and est_i > lim_i:
            breaches.append((key, label, est_i, lim_i))
    return breaches


def _build_warning(breaches: list[tuple[str, str, int, int]]) -> str:
    lines = ["[project-token-insights] 首轮 token 超阈预警："]
    hinted: set[str] = set()
    for key, name, est, lim in breaches:
        lines.append(f"  - {name}: 约 {est} tok（阈值 {lim}）")
        hint = ACTION_HINTS.get(key)
        if hint:
            hinted.add(hint)
    if hinted:
        lines.append("  优化方向：")
        for hint in sorted(hinted):
            lines.append(f"  - {hint}")
    lines.append("  运行 `/project-token-insights` 查看首轮组件基线与优化报告。")
    lines.append("  再次发送原 prompt 继续。")
    return "\n".join(lines)


def _event_name(payload: dict) -> str:
    for key in ("hook_event_name", "hookEventName", "hook_event", "hookEvent"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    if "prompt" in payload:
        return "UserPromptSubmit"
    return "SessionStart"


def _emit_continue() -> None:
    sys.stdout.write(json.dumps({"continue": True}))


def _handle_session_start(payload: dict) -> int:
    session_id = _session_id_from(payload)
    if str(payload.get("source", "")).lower() == "resume":
        return 0

    data_dir = _project_root() / DATA_DIRNAME
    marker = _safe_state_path(data_dir, WARNED_PREFIX, session_id)
    pending = _safe_state_path(data_dir, PENDING_PREFIX, session_id, ".json")
    if marker is None or pending is None or marker.exists():
        return 0

    baseline = _read_json(data_dir / BASELINE_FILENAME)
    if not isinstance(baseline, dict):
        return 0

    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        thresholds = _resolve_config()
        breaches = _collect_breaches(baseline, thresholds)
        if breaches:
            pending.write_text(
                json.dumps({"reason": _build_warning(breaches)}, ensure_ascii=False),
                encoding="utf-8",
            )
    except OSError:
        pass
    return 0


def _handle_user_prompt_submit(payload: dict) -> int:
    prompt = str(payload.get("prompt", "")).lstrip()
    if any(prompt.startswith(prefix) for prefix in _SYSTEM_PREFIXES):
        _emit_continue()
        return 0

    session_id = _session_id_from(payload)
    data_dir = _project_root() / DATA_DIRNAME
    marker = _safe_state_path(data_dir, WARNED_PREFIX, session_id)
    pending = _safe_state_path(data_dir, PENDING_PREFIX, session_id, ".json")
    if marker is None or pending is None:
        _emit_continue()
        return 0
    if marker.exists() or not pending.exists():
        _emit_continue()
        return 0

    state = _read_json(pending)
    reason = state.get("reason") if isinstance(state, dict) else None
    if not isinstance(reason, str) or not reason:
        _emit_continue()
        return 0

    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        marker.write_text("", encoding="utf-8")
        pending.unlink(missing_ok=True)
    except OSError:
        pass

    sys.stdout.write(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    return 0


def _main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}

    if _event_name(payload) == "UserPromptSubmit":
        return _handle_user_prompt_submit(payload)
    return _handle_session_start(payload)


def main() -> int:
    try:
        return _main()
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
