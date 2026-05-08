# skills

个人 skills 集合仓库，用于集中管理、测试和发布可复用的 Claude/Codex skill。

## 当前 skills

| Skill | 说明 |
| --- | --- |
| [`project-token-insights`](./project-token-insights/SKILL.md) | 检查当前项目首轮冷启动 token 组成，生成中文 Markdown 优化报告，并可选安装项目级提醒 hook。 |

## 本地路径

本仓库本地工作副本位于：

```bash
/Users/xuyangxy/Documents/ClaudeCode/skills
```

远程仓库：

```bash
https://github.com/xymelon/skills
```

## 使用方式

把需要启用的 skill 目录链接到你的 skills 目录，例如：

```bash
mkdir -p ~/.claude/skills
ln -sfn /Users/xuyangxy/Documents/ClaudeCode/skills/project-token-insights ~/.claude/skills/project-token-insights
```

启用后，在支持 skills 的环境里显式调用：

```text
/project-token-insights
```

## 开发与测试

在仓库根目录运行：

```bash
pytest project-token-insights/tests
```

新增 skill 时建议保持以下结构：

```text
skill-name/
├── SKILL.md
├── scripts/
├── references/
├── assets/
└── tests/
```

其中只有 `SKILL.md` 是必需文件，其他目录按需添加。
