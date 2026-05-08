#!/usr/bin/env python3
"""T6 首轮组件重建与估算的断言测试。

覆盖：
  - estimate_tokens 遵循 bytes/3.5 启发式
  - _encode_project_path 遵循 Claude project path 编码规则
  - project_data_dir / ensure_gitignore 写入报告目录规则
  - build_baseline 在空项目下不崩溃，字段齐全
  - build_baseline 正确统计 CLAUDE.md 与工具 schema 表
  - auto_memory 在 disabled 标志下归零
  - calibration 在无样本时返回中等置信度
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

import first_turn_breakdown as ftb  # noqa: E402


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------

class TestEstimateTokens:
    def test_empty_is_zero(self):
        assert ftb.estimate_tokens("") == 0

    def test_single_char_is_at_least_one(self):
        assert ftb.estimate_tokens("a") >= 1

    def test_bytes_over_3_5(self):
        # 35 ASCII bytes ≈ 10 tokens (35 / 3.5 = 10)
        assert ftb.estimate_tokens("a" * 35) == 10

    def test_utf8_byte_count_not_char_count(self):
        # '中' 是 3 字节；2 个中文字符 = 6 bytes，round(6/3.5) = 2
        assert ftb.estimate_tokens("中文") == pytest.approx(2, abs=1)


# ---------------------------------------------------------------------------
# _encode_project_path — Claude project path 编码规则
# ---------------------------------------------------------------------------

class TestEncodeProjectPath:
    def test_matches_claude_project_convention(self):
        assert ftb._encode_project_path("/Users/foo/bar") == "-Users-foo-bar"
        assert ftb._encode_project_path("/a.b_c/d") == "-a-b-c-d"


# ---------------------------------------------------------------------------
# project_data_dir & ensure_gitignore
# ---------------------------------------------------------------------------

class TestProjectDataDir:
    def test_points_to_dotfolder(self, tmp_path):
        assert ftb.project_data_dir(tmp_path) == tmp_path / ".project-token-insights"


class TestEnsureGitignore:
    def test_creates_when_missing(self, tmp_path):
        ftb.ensure_gitignore(tmp_path)
        gi = tmp_path / ".gitignore"
        assert gi.exists()
        assert ".project-token-insights/" in gi.read_text()

    def test_appends_when_rule_absent(self, tmp_path):
        (tmp_path / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
        ftb.ensure_gitignore(tmp_path)
        text = (tmp_path / ".gitignore").read_text()
        assert "node_modules/" in text
        assert ".project-token-insights/" in text

    def test_idempotent(self, tmp_path):
        ftb.ensure_gitignore(tmp_path)
        first = (tmp_path / ".gitignore").read_text()
        ftb.ensure_gitignore(tmp_path)
        assert (tmp_path / ".gitignore").read_text() == first

    def test_noop_if_rule_exists_without_slash(self, tmp_path):
        (tmp_path / ".gitignore").write_text(".project-token-insights\n", encoding="utf-8")
        ftb.ensure_gitignore(tmp_path)
        assert (tmp_path / ".gitignore").read_text() == ".project-token-insights\n"


# ---------------------------------------------------------------------------
# build_baseline — 基础健壮性
# ---------------------------------------------------------------------------

class TestBuildBaseline:
    def test_empty_project_has_all_fields(self, tmp_path, monkeypatch):
        # 隔离 PROJECTS_DIR/全局 CLAUDE.md/settings 避免读取真实 ~/.claude
        isolated = tmp_path / "home" / ".claude"
        isolated.mkdir(parents=True)
        monkeypatch.setattr(ftb, "PROJECTS_DIR", isolated / "projects")
        monkeypatch.setattr(ftb, "GLOBAL_CLAUDE_MD", isolated / "CLAUDE.md")
        monkeypatch.setattr(ftb, "GLOBAL_SETTINGS", isolated / "settings.json")
        monkeypatch.setattr(ftb, "PLUGINS_MANIFEST", isolated / "plugins" / "installed_plugins.json")
        monkeypatch.setattr(ftb, "PLUGINS_CACHE_DIR", isolated / "plugins" / "cache")

        baseline = ftb.build_baseline(str(tmp_path))

        expected_keys = {
            "generated_at", "project_root", "project_name",
            "global_claude_md", "project_claude_md",
            "plugin_descriptions", "plugin_descriptions_detail",
            "skill_descriptions", "skill_descriptions_detail",
            "agent_definitions", "agent_definitions_detail",
            "tool_schemas", "tool_schema_detail",
            "auto_memory", "auto_memory_disabled",
            "user_first_message", "first_turn_total",
            "calibration", "confidence",
        }
        assert expected_keys.issubset(baseline.keys())
        assert baseline["project_name"] == tmp_path.name
        assert baseline["global_claude_md"] == 0
        assert baseline["project_claude_md"] == 0
        assert baseline["tool_schemas"] > 0  # 硬编码查表始终非零
        assert baseline["confidence"] in {"高", "中", "低"}

    def test_project_claude_md_counted(self, tmp_path, monkeypatch):
        isolated = tmp_path / "home" / ".claude"
        isolated.mkdir(parents=True)
        monkeypatch.setattr(ftb, "PROJECTS_DIR", isolated / "projects")
        monkeypatch.setattr(ftb, "GLOBAL_CLAUDE_MD", isolated / "CLAUDE.md")
        monkeypatch.setattr(ftb, "GLOBAL_SETTINGS", isolated / "settings.json")
        monkeypatch.setattr(ftb, "PLUGINS_MANIFEST", isolated / "plugins" / "installed_plugins.json")
        monkeypatch.setattr(ftb, "PLUGINS_CACHE_DIR", isolated / "plugins" / "cache")

        (tmp_path / "CLAUDE.md").write_text("x" * 350, encoding="utf-8")
        baseline = ftb.build_baseline(str(tmp_path))
        # 350 bytes / 3.5 = 100 tok
        assert baseline["project_claude_md"] == 100

    def test_auto_memory_disabled_zeroes_out(self, tmp_path, monkeypatch):
        isolated = tmp_path / "home" / ".claude"
        isolated.mkdir(parents=True)
        (isolated / "settings.json").write_text(json.dumps({
            "env": {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
        }))
        (Path(tmp_path.parent) / "fake-memory-dir").mkdir(exist_ok=True)

        monkeypatch.setattr(ftb, "PROJECTS_DIR", isolated / "projects")
        monkeypatch.setattr(ftb, "GLOBAL_CLAUDE_MD", isolated / "CLAUDE.md")
        monkeypatch.setattr(ftb, "GLOBAL_SETTINGS", isolated / "settings.json")
        monkeypatch.setattr(ftb, "PLUGINS_MANIFEST", isolated / "plugins" / "installed_plugins.json")
        monkeypatch.setattr(ftb, "PLUGINS_CACHE_DIR", isolated / "plugins" / "cache")

        baseline = ftb.build_baseline(str(tmp_path))
        assert baseline["auto_memory"] == 0
        assert baseline["auto_memory_disabled"] is True

    def test_auto_memory_reads_project_memory_entrypoint(self, tmp_path, monkeypatch):
        isolated = tmp_path / "home" / ".claude"
        projects_dir = isolated / "projects"
        memory_dir = projects_dir / ftb._encode_project_path(str(tmp_path)) / "memory"
        memory_dir.mkdir(parents=True)
        (memory_dir / "MEMORY.md").write_text("x" * 350, encoding="utf-8")

        monkeypatch.delenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", raising=False)
        monkeypatch.setattr(ftb, "PROJECTS_DIR", projects_dir)
        monkeypatch.setattr(ftb, "GLOBAL_CLAUDE_MD", isolated / "CLAUDE.md")
        monkeypatch.setattr(ftb, "GLOBAL_SETTINGS", isolated / "settings.json")
        monkeypatch.setattr(ftb, "PLUGINS_MANIFEST", isolated / "plugins" / "installed_plugins.json")
        monkeypatch.setattr(ftb, "PLUGINS_CACHE_DIR", isolated / "plugins" / "cache")

        baseline = ftb.build_baseline(str(tmp_path))

        assert baseline["auto_memory"] == 100
        assert baseline["auto_memory_disabled"] is False

    def test_auto_memory_uses_project_local_auto_memory_enabled_false(self, tmp_path, monkeypatch):
        isolated = tmp_path / "home" / ".claude"
        projects_dir = isolated / "projects"
        memory_dir = projects_dir / ftb._encode_project_path(str(tmp_path)) / "memory"
        memory_dir.mkdir(parents=True)
        (memory_dir / "MEMORY.md").write_text("x" * 350, encoding="utf-8")
        project_settings = tmp_path / ".claude"
        project_settings.mkdir()
        (project_settings / "settings.local.json").write_text(
            json.dumps({"autoMemoryEnabled": False}),
            encoding="utf-8",
        )

        monkeypatch.delenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", raising=False)
        monkeypatch.setattr(ftb, "PROJECTS_DIR", projects_dir)
        monkeypatch.setattr(ftb, "GLOBAL_CLAUDE_MD", isolated / "CLAUDE.md")
        monkeypatch.setattr(ftb, "GLOBAL_SETTINGS", isolated / "settings.json")
        monkeypatch.setattr(ftb, "PLUGINS_MANIFEST", isolated / "plugins" / "installed_plugins.json")
        monkeypatch.setattr(ftb, "PLUGINS_CACHE_DIR", isolated / "plugins" / "cache")

        baseline = ftb.build_baseline(str(tmp_path))

        assert baseline["auto_memory"] == 0
        assert baseline["auto_memory_disabled"] is True

    def test_no_sample_confidence_is_medium(self, tmp_path, monkeypatch):
        isolated = tmp_path / "home" / ".claude"
        isolated.mkdir(parents=True)
        monkeypatch.setattr(ftb, "PROJECTS_DIR", isolated / "projects")
        monkeypatch.setattr(ftb, "GLOBAL_CLAUDE_MD", isolated / "CLAUDE.md")
        monkeypatch.setattr(ftb, "GLOBAL_SETTINGS", isolated / "settings.json")
        monkeypatch.setattr(ftb, "PLUGINS_MANIFEST", isolated / "plugins" / "installed_plugins.json")
        monkeypatch.setattr(ftb, "PLUGINS_CACHE_DIR", isolated / "plugins" / "cache")

        baseline = ftb.build_baseline(str(tmp_path))
        assert baseline["calibration"]["actual_cache_creation_input_tokens"] == 0
        assert baseline["confidence"] == "中"

    def test_tool_schemas_table_non_empty(self):
        total, detail = ftb.estimate_tool_schemas()
        assert total == sum(detail.values())
        assert "Bash" in detail
        assert "Read" in detail
        assert "Glob" not in detail
        assert "Grep" not in detail
        assert total > 3000  # 所有内置工具之和保底

    def test_cold_start_samples_cover_each_session(self, tmp_path, monkeypatch):
        isolated = tmp_path / "home" / ".claude"
        projects_dir = isolated / "projects"
        encoded = ftb._encode_project_path(str(tmp_path))
        proj_dir = projects_dir / encoded
        proj_dir.mkdir(parents=True)
        for idx, cache_creation in enumerate([10_000, 20_000, 30_000], start=1):
            lines = [
                json.dumps({
                    "type": "user",
                    "sessionId": f"s{idx}",
                    "message": {"content": f"hello {idx}"},
                }),
                json.dumps({
                    "type": "assistant",
                    "sessionId": f"s{idx}",
                    "timestamp": f"2026-01-0{idx}T00:00:00Z",
                    "isSidechain": False,
                    "message": {
                        "model": "claude-sonnet",
                        "usage": {
                            "input_tokens": cache_creation,
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": cache_creation,
                        },
                    },
                }),
            ]
            (proj_dir / f"s{idx}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

        monkeypatch.setattr(ftb, "PROJECTS_DIR", projects_dir)
        monkeypatch.setattr(ftb, "GLOBAL_CLAUDE_MD", isolated / "CLAUDE.md")
        monkeypatch.setattr(ftb, "GLOBAL_SETTINGS", isolated / "settings.json")
        monkeypatch.setattr(ftb, "PLUGINS_MANIFEST", isolated / "plugins" / "installed_plugins.json")
        monkeypatch.setattr(ftb, "PLUGINS_CACHE_DIR", isolated / "plugins" / "cache")

        baseline = ftb.build_baseline(str(tmp_path))

        assert baseline["calibration"]["sample_count"] == 3
        assert baseline["cold_start_cache_creation_stats"]["p50"] == 20_000
        assert len(baseline["cold_start_samples"]) == 3

    def test_disabled_skill_descriptions_are_not_counted(self, tmp_path, monkeypatch):
        isolated = tmp_path / "home" / ".claude"
        isolated.mkdir(parents=True)
        plugin_root = tmp_path / "plugins" / "demo"
        (plugin_root / ".claude-plugin").mkdir(parents=True)
        (plugin_root / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "demo", "description": "demo plugin"}),
            encoding="utf-8",
        )
        enabled_skill = plugin_root / "skills" / "enabled" / "SKILL.md"
        disabled_skill = plugin_root / "skills" / "disabled" / "SKILL.md"
        enabled_skill.parent.mkdir(parents=True)
        disabled_skill.parent.mkdir(parents=True)
        enabled_skill.write_text(
            "---\nname: enabled\ndescription: loaded skill\n---\n",
            encoding="utf-8",
        )
        disabled_skill.write_text(
            "---\nname: disabled\ndisable-model-invocation: true\ndescription: explicit only\n---\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        monkeypatch.setattr(ftb, "PROJECTS_DIR", isolated / "projects")
        monkeypatch.setattr(ftb, "GLOBAL_CLAUDE_MD", isolated / "CLAUDE.md")
        monkeypatch.setattr(ftb, "GLOBAL_SETTINGS", isolated / "settings.json")
        monkeypatch.setattr(ftb, "PLUGINS_MANIFEST", isolated / "plugins" / "installed_plugins.json")
        monkeypatch.setattr(ftb, "PLUGINS_CACHE_DIR", isolated / "plugins" / "cache")

        baseline = ftb.build_baseline(str(tmp_path))
        skill_names = {s["name"] for s in baseline["skill_descriptions_detail"]}
        assert "enabled" in skill_names
        assert "disabled" not in skill_names

    def test_skill_descriptions_include_project_user_plugin_metadata(self, tmp_path, monkeypatch):
        isolated = tmp_path / "home" / ".claude"
        isolated.mkdir(parents=True)
        user_skill = isolated / "skills" / "user-skill" / "SKILL.md"
        user_skill.parent.mkdir(parents=True)
        user_skill.write_text(
            "---\n"
            "name: user-skill\n"
            "description:\n"
            "  user skill folded description\n"
            "  continues here\n"
            "when_to_use: use for user-only workflows\n"
            "---\n"
            "# User Skill\n\n"
            "This full body should only load when invoked.\n",
            encoding="utf-8",
        )

        project_skill = tmp_path / ".claude" / "skills" / "project-skill" / "SKILL.md"
        project_skill.parent.mkdir(parents=True)
        project_skill.write_text(
            "---\nname: project-skill\nwhen_to_use: use for project workflows\n---\n"
            "# Project Skill\n\n"
            "Fallback body paragraph used as description.\n\n"
            "Longer body that should not be part of metadata.\n",
            encoding="utf-8",
        )

        plugin_root = tmp_path / "plugins" / "demo"
        (plugin_root / ".claude-plugin").mkdir(parents=True)
        (plugin_root / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "demo", "description": "demo plugin"}),
            encoding="utf-8",
        )
        plugin_skill = plugin_root / "skills" / "plugin-skill" / "SKILL.md"
        plugin_skill.parent.mkdir(parents=True)
        plugin_skill.write_text(
            "---\n"
            "name: plugin-skill\n"
            f"description: {'x' * (ftb.SKILL_METADATA_CHAR_LIMIT + 100)}\n"
            "---\n",
            encoding="utf-8",
        )
        plugin_command = plugin_root / "commands" / "ship.md"
        plugin_command.parent.mkdir()
        plugin_command.write_text(
            "---\ndescription: Ship the current change\n---\n# Ship\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        monkeypatch.setattr(ftb, "PROJECTS_DIR", isolated / "projects")
        monkeypatch.setattr(ftb, "GLOBAL_CLAUDE_MD", isolated / "CLAUDE.md")
        monkeypatch.setattr(ftb, "GLOBAL_SETTINGS", isolated / "settings.json")
        monkeypatch.setattr(ftb, "PLUGINS_MANIFEST", isolated / "plugins" / "installed_plugins.json")
        monkeypatch.setattr(ftb, "PLUGINS_CACHE_DIR", isolated / "plugins" / "cache")

        baseline = ftb.build_baseline(str(tmp_path))

        skills = {s["name"]: s for s in baseline["skill_descriptions_detail"]}
        expected_total = sum(s["tokens"] for s in skills.values())
        assert baseline["skill_descriptions"] == expected_total
        assert skills["user-skill"]["source"] == "user"
        assert skills["user-skill"]["description_source"] == "frontmatter"
        assert skills["project-skill"]["source"] == "project"
        assert skills["project-skill"]["description_source"] == "body"
        assert skills["plugin-skill"]["source"] == "plugin"
        assert skills["plugin-skill"]["plugin"] == "demo"
        assert skills["plugin-skill"]["metadata_chars"] == ftb.SKILL_METADATA_CHAR_LIMIT
        assert skills["demo:ship"]["kind"] == "plugin_command"
        assert skills["demo:ship"]["source"] == "plugin"
        assert skills["init"]["source"] == "builtin"

    def test_nested_skill_directories_are_not_model_visible(self, tmp_path, monkeypatch):
        isolated = tmp_path / "home" / ".claude"
        nested_skill = isolated / "skills" / "outer" / "inner" / "SKILL.md"
        nested_skill.parent.mkdir(parents=True)
        nested_skill.write_text(
            "---\nname: nested\ndescription: should not be discovered\n---\n",
            encoding="utf-8",
        )

        top_skill = isolated / "skills" / "top" / "SKILL.md"
        top_skill.parent.mkdir(parents=True)
        top_skill.write_text(
            "---\nname: top\ndescription: top level skill\n---\n",
            encoding="utf-8",
        )

        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        monkeypatch.setattr(ftb, "PROJECTS_DIR", isolated / "projects")
        monkeypatch.setattr(ftb, "GLOBAL_CLAUDE_MD", isolated / "CLAUDE.md")
        monkeypatch.setattr(ftb, "GLOBAL_SETTINGS", isolated / "settings.json")
        monkeypatch.setattr(ftb, "PLUGINS_MANIFEST", isolated / "plugins" / "installed_plugins.json")
        monkeypatch.setattr(ftb, "PLUGINS_CACHE_DIR", isolated / "plugins" / "cache")

        baseline = ftb.build_baseline(str(tmp_path))

        skill_names = {s["name"] for s in baseline["skill_descriptions_detail"]}
        assert "top" in skill_names
        assert "nested" not in skill_names

    def test_agent_definitions_include_project_user_and_plugin_agents(self, tmp_path, monkeypatch):
        isolated = tmp_path / "home" / ".claude"
        isolated.mkdir(parents=True)
        user_agents = isolated / "agents"
        user_agents.mkdir()
        (user_agents / "user-reviewer.md").write_text(
            "---\nname: user-reviewer\ndescription: review diff changes\n---\n" + "u" * 350,
            encoding="utf-8",
        )

        project_agents = tmp_path / ".claude" / "agents"
        project_agents.mkdir(parents=True)
        (project_agents / "project-reviewer.md").write_text(
            "---\nname: project-reviewer\ndescription: inspect project policies\n---\n" + "p" * 350,
            encoding="utf-8",
        )

        plugin_root = tmp_path / "plugins" / "demo"
        (plugin_root / ".claude-plugin").mkdir(parents=True)
        (plugin_root / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "demo", "description": "demo plugin"}),
            encoding="utf-8",
        )
        plugin_agents = plugin_root / "agents"
        plugin_agents.mkdir()
        (plugin_agents / "plugin-reviewer.md").write_text(
            "---\nname: plugin-reviewer\ndescription: plugin provided review\n---\n" + "g" * 350,
            encoding="utf-8",
        )

        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        monkeypatch.setattr(ftb, "PROJECTS_DIR", isolated / "projects")
        monkeypatch.setattr(ftb, "GLOBAL_CLAUDE_MD", isolated / "CLAUDE.md")
        monkeypatch.setattr(ftb, "GLOBAL_SETTINGS", isolated / "settings.json")
        monkeypatch.setattr(ftb, "PLUGINS_MANIFEST", isolated / "plugins" / "installed_plugins.json")
        monkeypatch.setattr(ftb, "PLUGINS_CACHE_DIR", isolated / "plugins" / "cache")

        baseline = ftb.build_baseline(str(tmp_path))

        agents = {a["name"]: a for a in baseline["agent_definitions_detail"]}
        expected_total = sum(a["tokens"] for a in agents.values())
        assert baseline["agent_definitions"] == expected_total
        custom_agents = {
            name: agent for name, agent in agents.items()
            if name in {"project-reviewer", "user-reviewer", "plugin-reviewer"}
        }
        assert all(a["tokens"] < a["full_tokens"] for a in custom_agents.values())
        assert agents["project-reviewer"]["source"] == "project"
        assert agents["user-reviewer"]["source"] == "user"
        assert agents["plugin-reviewer"]["source"] == "plugin"
        assert agents["plugin-reviewer"]["plugin"] == "demo"
        assert agents["general-purpose"]["source"] == "builtin"

    def test_agent_definitions_recurse_into_subdirectories(self, tmp_path, monkeypatch):
        isolated = tmp_path / "home" / ".claude"
        isolated.mkdir(parents=True)

        project_agents = tmp_path / ".claude" / "agents"
        (project_agents / "academic").mkdir(parents=True)
        (project_agents / "academic" / "historian.md").write_text(
            "---\nname: historian\ndescription: academic history expert\n---\n" + "h" * 350,
            encoding="utf-8",
        )
        (project_agents / "design").mkdir()
        (project_agents / "design" / "layout.md").write_text(
            "body without frontmatter " + "l" * 350,
            encoding="utf-8",
        )

        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        monkeypatch.setattr(ftb, "PROJECTS_DIR", isolated / "projects")
        monkeypatch.setattr(ftb, "GLOBAL_CLAUDE_MD", isolated / "CLAUDE.md")
        monkeypatch.setattr(ftb, "GLOBAL_SETTINGS", isolated / "settings.json")
        monkeypatch.setattr(ftb, "PLUGINS_MANIFEST", isolated / "plugins" / "installed_plugins.json")
        monkeypatch.setattr(ftb, "PLUGINS_CACHE_DIR", isolated / "plugins" / "cache")

        baseline = ftb.build_baseline(str(tmp_path))

        detail = {a["rel_path"]: a for a in baseline["agent_definitions_detail"]}
        project_detail = {
            path: agent for path, agent in detail.items()
            if agent["source"] == "project"
        }
        assert set(project_detail.keys()) == {"academic/historian"}
        assert detail["academic/historian"]["name"] == "historian"
        assert "design/layout" not in detail
        assert all(a["full_tokens"] >= a["tokens"] for a in project_detail.values())

    def test_project_local_settings_are_read_for_enabled_plugins(self, tmp_path, monkeypatch):
        isolated = tmp_path / "home" / ".claude"
        cache_root = isolated / "plugins" / "cache"
        plugin_root = cache_root / "market" / "local-plugin" / "v1"
        (plugin_root / ".claude-plugin").mkdir(parents=True)
        (plugin_root / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "local-plugin", "description": "from local project settings"}),
            encoding="utf-8",
        )
        (isolated / "plugins").mkdir(parents=True, exist_ok=True)
        (isolated / "plugins" / "installed_plugins.json").write_text(
            json.dumps({
                "plugins": {
                    "local-plugin@market": [{
                        "installPath": str(plugin_root),
                        "version": "v1",
                    }]
                }
            }),
            encoding="utf-8",
        )
        project_settings = tmp_path / ".claude"
        project_settings.mkdir()
        (project_settings / "settings.local.json").write_text(
            json.dumps({"enabledPlugins": {"local-plugin@market": True}}),
            encoding="utf-8",
        )

        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        monkeypatch.setattr(ftb, "PROJECTS_DIR", isolated / "projects")
        monkeypatch.setattr(ftb, "GLOBAL_CLAUDE_MD", isolated / "CLAUDE.md")
        monkeypatch.setattr(ftb, "GLOBAL_SETTINGS", isolated / "settings.json")
        monkeypatch.setattr(ftb, "PLUGINS_MANIFEST", isolated / "plugins" / "installed_plugins.json")
        monkeypatch.setattr(ftb, "PLUGINS_CACHE_DIR", cache_root)

        baseline = ftb.build_baseline(str(tmp_path))

        plugin_names = {p["name"] for p in baseline["plugin_descriptions_detail"]}
        assert "local-plugin" in plugin_names

    def test_non_official_project_settings_json_local_is_ignored(self, tmp_path, monkeypatch):
        isolated = tmp_path / "home" / ".claude"
        cache_root = isolated / "plugins" / "cache"
        plugin_root = cache_root / "market" / "ghost-plugin" / "v1"
        (plugin_root / ".claude-plugin").mkdir(parents=True)
        (plugin_root / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "ghost-plugin", "description": "non official local filename"}),
            encoding="utf-8",
        )
        (isolated / "plugins").mkdir(parents=True, exist_ok=True)
        (isolated / "plugins" / "installed_plugins.json").write_text(
            json.dumps({
                "plugins": {
                    "ghost-plugin@market": [{
                        "installPath": str(plugin_root),
                        "version": "v1",
                    }]
                }
            }),
            encoding="utf-8",
        )
        (isolated / "settings.json").write_text(
            json.dumps({"enabledPlugins": {}}),
            encoding="utf-8",
        )
        project_settings = tmp_path / ".claude"
        project_settings.mkdir()
        (project_settings / "settings.json.local").write_text(
            json.dumps({"enabledPlugins": {"ghost-plugin@market": True}}),
            encoding="utf-8",
        )

        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        monkeypatch.setattr(ftb, "PROJECTS_DIR", isolated / "projects")
        monkeypatch.setattr(ftb, "GLOBAL_CLAUDE_MD", isolated / "CLAUDE.md")
        monkeypatch.setattr(ftb, "GLOBAL_SETTINGS", isolated / "settings.json")
        monkeypatch.setattr(ftb, "PLUGINS_MANIFEST", isolated / "plugins" / "installed_plugins.json")
        monkeypatch.setattr(ftb, "PLUGINS_CACHE_DIR", cache_root)

        baseline = ftb.build_baseline(str(tmp_path))

        plugin_names = {p["name"] for p in baseline["plugin_descriptions_detail"]}
        assert "ghost-plugin" not in plugin_names

    def test_main_writes_baseline_to_project_dir(self, tmp_path, monkeypatch):
        import subprocess
        import os as _os
        isolated_home = tmp_path / "home"
        (isolated_home / ".claude").mkdir(parents=True)
        env = {
            **_os.environ,
            "CLAUDE_PROJECT_DIR": str(tmp_path / "proj"),
        }
        (tmp_path / "proj").mkdir()
        script = str(_SCRIPTS_DIR / "first_turn_breakdown.py")
        result = subprocess.run(
            [sys.executable, script, "--no-write"],
            capture_output=True, text=True, env=env,
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["project_name"] == "proj"
