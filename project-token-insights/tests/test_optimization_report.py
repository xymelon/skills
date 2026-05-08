#!/usr/bin/env python3
"""Optimization report recommendation wording."""

from __future__ import annotations

import sys
from pathlib import Path


_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

import optimization_report as report  # noqa: E402


def test_unused_agents_report_includes_definition_source():
    rec = report.recommend_unused_agents(
        [
            {"name": "project-reviewer", "tokens": 100, "source": "project"},
            {"name": "user-reviewer", "tokens": 100, "source": "user"},
            {"name": "plugin-reviewer", "tokens": 100, "source": "plugin", "plugin": "ccforge"},
        ],
    )

    joined = "\n".join(rec["findings"] + rec["actions"])
    assert "项目 .claude/agents" in joined
    assert "用户 ~/.claude/agents" in joined
    assert "插件 ccforge" in joined
    assert ".claude/agents/*.md" in joined


def test_unused_agents_report_lists_all_entries_and_summary():
    agents = [
        {"name": f"a-{i}", "tokens": 50 + i, "source": "project"}
        for i in range(30)
    ]
    agents.append({"name": "general-purpose", "tokens": 20, "source": "builtin"})

    rec = report.recommend_unused_agents(agents)

    name_lines = [f for f in rec["findings"] if f.startswith("a-")]
    summary = rec["findings"][0]
    assert "共扫到 31 个 agent 定义" in summary
    assert "30 个属于项目/用户/插件可调整范围" in summary
    assert "不能自动判定最近是否未使用" in "\n".join(rec["findings"])
    assert "内置 agent" in "\n".join(rec["findings"])
    assert len(name_lines) == 30
    # 按 tokens 降序排列
    assert name_lines[0].startswith("a-29")
    assert name_lines[-1].startswith("a-0:")


def test_skill_inventory_lists_all_entries_with_source_breakdown():
    skills = [
        {"name": "p1", "tokens": 30, "full_tokens": 300, "source": "project"},
        {"name": "u1", "tokens": 80, "full_tokens": 500, "source": "user"},
        {"name": "g1", "tokens": 50, "full_tokens": 400, "source": "plugin", "plugin": "ccforge"},
        {"name": "init", "tokens": 20, "full_tokens": 20, "source": "builtin", "kind": "builtin_command"},
        {"name": "ralph-loop:help", "tokens": 10, "full_tokens": 100, "source": "plugin", "plugin": "ralph-loop", "kind": "plugin_command"},
    ]

    rec = report.recommend_skill_inventory(skills)

    joined = "\n".join(rec["findings"])
    assert rec["current_tokens"] == 190
    assert "共扫到 5 个模型可见 skill/command 入口" in rec["findings"][0]
    assert "项目 .claude/skills 1 个" in rec["findings"][0]
    assert "用户 ~/.claude/skills 1 个" in rec["findings"][0]
    assert "插件 ccforge skills 1 个" in rec["findings"][0]
    assert "Claude Code 内置入口 1 个" in rec["findings"][0]
    assert "插件 ralph-loop commands 1 个" in rec["findings"][0]
    # 按 tokens 降序
    body = rec["findings"][1:]
    assert body[0].startswith("u1:")
    assert body[1].startswith("g1:")
    assert body[2].startswith("p1:")
    # metadata / 全文 都有
    assert "metadata" in joined and "全文" in joined


def test_skill_inventory_empty_gives_low_confidence():
    rec = report.recommend_skill_inventory([])
    assert rec["confidence"] == "低"
    assert "未扫描到启用的 skill 定义" in rec["findings"][0]


def test_agent_teams_reports_default_disabled_when_env_missing():
    rec = report.recommend_agent_team_cost(settings_sources=[{}])

    joined = "\n".join(rec["findings"] + rec["actions"])
    assert "默认关闭" in joined
    assert report.AGENT_TEAMS_ENV in joined
    assert "调研未找到官方禁用 agent team 开关" not in joined


def test_agent_teams_reports_enabled_env_in_project_settings():
    rec = report.recommend_agent_team_cost(
        settings_sources=[
            {},
            {"env": {report.AGENT_TEAMS_ENV: "1"}},
        ],
    )

    joined = "\n".join(rec["findings"] + rec["actions"])
    assert rec["confidence"] == "高"
    assert "项目 settings" in joined
    assert "移除" in joined


def test_agent_teams_reports_enabled_env_var(monkeypatch):
    monkeypatch.setenv(report.AGENT_TEAMS_ENV, "1")

    rec = report.recommend_agent_team_cost(settings_sources=[{}])

    joined = "\n".join(rec["findings"] + rec["actions"])
    assert rec["confidence"] == "高"
    assert "当前环境变量" in joined


def test_auto_memory_disabled_by_project_setting(monkeypatch):
    monkeypatch.delenv(report.AUTO_MEMORY_DISABLE_ENV, raising=False)
    rec = report.recommend_auto_memory(
        [{"autoMemoryEnabled": False}],
        {"auto_memory": 100},
    )

    joined = "\n".join(rec["findings"] + rec["actions"])
    assert rec["estimated_savings_tok"] == 0
    assert "已通过 settings" in joined


def test_auto_memory_recommends_project_local_setting_when_enabled(monkeypatch):
    monkeypatch.delenv(report.AUTO_MEMORY_DISABLE_ENV, raising=False)
    rec = report.recommend_auto_memory(
        [{}],
        {"auto_memory": 100},
    )

    joined = "\n".join(rec["actions"])
    assert rec["estimated_savings_tok"] == 100
    assert ".claude/settings.local.json" in joined
    assert "autoMemoryEnabled" in joined


def test_cold_tools_permissions_deny_wording_is_conservative():
    rec = report.recommend_cold_tools(
        {"NotebookEdit": 260, "CronCreate": 180, "Read": 220},
    )

    joined = "\n".join(rec["actions"])
    assert "--disallowedTools" in joined
    assert "--tools" in joined
    assert report.CRON_DISABLE_ENV in joined
    assert "官方未明确保证" in joined
    assert "不会减少首轮 schema token" not in joined
