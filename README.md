# Skills 仓库

当前仓库是个人维护的 skills 集合仓库，用于存放、迭代和测试可复用的 Claude Code / Codex skill。

## 当前目录包含的 Skills

### project-token-insights

`project-token-insights` 是一个面向单个项目的 token 优化检查 skill。它通过 `/project-token-insights` 或 Skill 工具显式调用，用来分析当前项目首轮冷启动 token 的组成，并生成中文 Markdown 优化报告。

这个 skill 的目标是回答一个很具体的问题：当前项目第一轮会把 token 花在哪里，以及哪些常驻上下文可以被优化。

核心能力：

- 估算首轮冷启动 token 组成，包括项目 `CLAUDE.md`、启用插件清单、可见 skill / command / agent metadata、Claude Code 内置工具 schema、Auto memory 注入内容和首条用户消息。
- 读取当前项目对应的本地 JSONL transcript，用实际 `cache_creation_input_tokens` 样本校准估算置信度。
- 生成 `.project-token-insights/first-turn-baseline.json`，保存首轮组件估算基线。
- 生成 `.project-token-insights/optimization-report.md`，输出中文优化建议。
- 可选安装项目级 hook，用于提醒 cache-bust 风险和首轮 token 超阈情况。
- 所有产物只写入当前项目目录，不需要 API Key，不联网，也不跨项目扫描。

优化报告覆盖的方向：

- 清理或精简 `CLAUDE.md`、插件、skills 等常驻上下文。
- 核查未使用或体量较大的 agent。
- 检查是否开启实验性的 Agent Team。
- 诊断 Auto memory 的启动注入开销。
- 识别冷门但高成本的工具 schema。
- 检查全局 / 项目 `CLAUDE.md` 中可能重复的段落。

主要文件：

| 路径 | 说明 |
| --- | --- |
| [`project-token-insights/SKILL.md`](./project-token-insights/SKILL.md) | skill 的主说明文件，定义触发方式、执行步骤、约束和中文呈现格式。 |
| [`project-token-insights/scripts/first_turn_breakdown.py`](./project-token-insights/scripts/first_turn_breakdown.py) | 生成首轮 token 组成估算基线。 |
| [`project-token-insights/scripts/optimization_report.py`](./project-token-insights/scripts/optimization_report.py) | 基于基线生成中文 Markdown 优化报告。 |
| [`project-token-insights/scripts/install_hooks.py`](./project-token-insights/scripts/install_hooks.py) | 安装或卸载当前项目的提醒 hook。 |
| [`project-token-insights/assets/cache-hooks/`](./project-token-insights/assets/cache-hooks/) | cache-bust 风险提醒 hook 脚本。 |
| [`project-token-insights/assets/first-turn-hooks/`](./project-token-insights/assets/first-turn-hooks/) | 首轮 token 超阈提醒 hook 脚本。 |
| [`project-token-insights/config/first-turn-budget.json`](./project-token-insights/config/first-turn-budget.json) | 首轮 token 预算阈值配置模板。 |
| [`project-token-insights/tests/`](./project-token-insights/tests/) | skill 脚本和 hook 的测试用例。 |

本仓库当前只有这一个 skill。后续新增 skill 时，应在本 README 中继续补充对应说明。
