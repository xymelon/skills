---
name: deploy-github-pages
description: 将用户指定的单个 HTML 文件部署到 GitHub Pages 聚合站（<user>.github.io 下的子路径）。当用户说"部署到 github pages"、"deploy to github pages"、"发布 html 到 github"、"把这个 html 放到网上"、"用 github pages 托管"、"publish on github pages"、"host this html on github"等类似表述时，必须触发此 skill。用户通常会指定一个具体的 html 文件路径，且该文件可能位于任意目录（不一定是 git 仓库）。
disable-model-invocation: true
---

# Deploy to GitHub Pages（聚合站模式）

将**用户指定的单个 HTML 文件**部署到聚合仓库 `<user>.github.io` 下的子路径，最终 URL 形如 `https://<user>.github.io/<slug>/`。

## 核心约定（务必理解）

| 概念 | 说明 |
|---|---|
| HTML 源文件 | 用户指定的绝对路径，可能位于任何位置 |
| 部署仓库 | 固定为 `<user>.github.io`，**与源文件所在目录无关** |
| 本地工作副本 | `mktemp -d` 创建的临时目录，部署完毕清理 |
| slug | html 文件名（去扩展名 + 转小写 + 连字符化） |
| 站内路径 | `<repo>/<slug>/index.html` —— **必须重命名为 `index.html`** |
| 访问 URL | `https://<user>.github.io/<slug>/` |

**重要禁忌**：

- 绝对不要在 html 源文件所在目录执行 `git init` / `git remote add` / 任何写 git 操作。源目录是不是 git 仓库都与本流程无关。
- 绝对不要 `cp *.html` 把整目录拷过去。每次只部署用户明确指定的那一个 html。

---

## 第一步：检查 GitHub Token

```bash
echo ${GITHUB_TOKEN:+已设置}
```

### 如果未设置，引导用户配置 PAT：

1. GitHub → 右上角头像 → **Settings**
2. 左侧菜单底部 → **Developer settings**
3. **Personal access tokens** → **Tokens (classic)** → **Generate new token (classic)**
4. 备注随便写，过期时间按需，**勾选 `repo` 权限**
5. **Generate token**，立即复制（只显示一次）

写入 shell 配置（永久生效）：

```bash
# zsh（macOS 默认）
echo 'export GITHUB_TOKEN="ghp_你的token"' >> ~/.zshrc
source ~/.zshrc

# bash
echo 'export GITHUB_TOKEN="ghp_你的token"' >> ~/.bashrc
source ~/.bashrc
```

> 配置完后告诉我，我会继续后续步骤。

---

## 第二步：定位 HTML 源文件

用户通常在消息中给出 html 路径。如果不明确，**主动询问用户**：「请告诉我要部署的 html 文件的完整路径。」

```bash
HTML_INPUT="<用户给出的路径>"

# 解析为绝对路径
case "$HTML_INPUT" in
  /*) HTML_ABS="$HTML_INPUT" ;;
  *)  HTML_ABS="$(pwd)/$HTML_INPUT" ;;
esac

[ -f "$HTML_ABS" ] || { echo "错误：找不到 $HTML_INPUT"; exit 1; }

# 计算 slug
SLUG=$(basename "$HTML_ABS" .html \
  | tr '[:upper:]' '[:lower:]' \
  | tr ' _.' '-' \
  | tr -s '-' \
  | sed 's/^-//;s/-$//')

echo "源文件：$HTML_ABS"
echo "Slug：$SLUG"
```

---

## 第三步：确认部署仓库（聚合站 `<user>.github.io`）

```bash
GITHUB_USER=$(curl -s \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/user \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['login'])")

DEPLOY_REPO="${GITHUB_USER}.github.io"
echo "部署仓库：$GITHUB_USER/$DEPLOY_REPO"

# 检查仓库是否已存在
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  "https://api.github.com/repos/$GITHUB_USER/$DEPLOY_REPO")

if [ "$HTTP_CODE" = "404" ]; then
  # 创建聚合站仓库；auto_init=true 避免空仓库 clone 报错
  curl -s -X POST \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    https://api.github.com/user/repos \
    -d "{\"name\":\"$DEPLOY_REPO\",\"private\":false,\"description\":\"My GitHub Pages aggregation site\",\"auto_init\":true}" > /dev/null
  REPO_IS_NEW=yes
  sleep 2
else
  REPO_IS_NEW=no
fi
```

### 第三步附加：检测同名仓库遮蔽（重要预检）

如果 GitHub 上存在 `<user>/<slug>` 这样的项目仓库且启用了 Pages，它会**遮蔽**聚合仓库的 `<slug>/` 子目录路径，导致部署后访问 404。**部署前必须预检并处理。**

```bash
CONFLICT_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  "https://api.github.com/repos/$GITHUB_USER/$SLUG/pages")

if [ "$CONFLICT_CODE" = "200" ]; then
  echo "检测到同名仓库 $GITHUB_USER/$SLUG 已启用 Pages，会遮蔽聚合站子目录。"
  echo "请选择处理方式后再继续："
  echo "  A) 禁用该仓库 Pages（保留代码作归档）"
  echo "  B) 删除该仓库（需要 token 有 delete_repo scope，或在 GitHub 网页手动删）"
  echo "  C) 部署时换一个 slug"
  exit 1
fi
```

如果用户选 A，调用：

```bash
curl -s -X DELETE \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/$GITHUB_USER/$SLUG/pages"
```

---

## 第四步：在临时目录中 clone 部署仓库

全程在 `WORK_DIR` 内操作，**绝不动 html 源目录**。

```bash
WORK_DIR=$(mktemp -d -t gh-pages-deploy-XXXXXX)
echo "工作目录：$WORK_DIR"

git clone "https://${GITHUB_TOKEN}@github.com/${GITHUB_USER}/${DEPLOY_REPO}.git" "$WORK_DIR" 2>&1 \
  | sed "s/${GITHUB_TOKEN}/***/g"
```

---

## 第五步：写入 html，**重命名为 `index.html`**，提交推送

**关键修复**：之前部署 404 的根因就是仓库里只有 `xxx.html`，没有 `index.html`，访问 `<repo>/<slug>/` 默认找不到入口。本步骤强制写为 `index.html`。

```bash
TARGET_DIR="$WORK_DIR/$SLUG"
mkdir -p "$TARGET_DIR"

# 强制重命名为 index.html
cp "$HTML_ABS" "$TARGET_DIR/index.html"

cd "$WORK_DIR"

# 兜底 git identity（用户未设全局时不至于 commit 失败）
git config user.email >/dev/null 2>&1 || git config user.email "deploy@local"
git config user.name  >/dev/null 2>&1 || git config user.name  "GitHub Pages Deploy"

git add "$SLUG"
# 使用 conventional commits 格式，避免被项目的 commit-msg hook 拦截
# 如果 slug 内容没变化，commit 会报 nothing to commit；用 --allow-empty 兜底
git commit -m "chore: deploy $SLUG" --allow-empty
git push origin HEAD
```

---

## 第六步：启用 Pages（仅新建仓库需要）

```bash
if [ "$REPO_IS_NEW" = "yes" ]; then
  curl -s -X POST \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com/repos/$GITHUB_USER/$DEPLOY_REPO/pages" \
    -d '{"source":{"branch":"main","path":"/"}}' > /dev/null
fi
```

---

## 第七步：清理 & 输出

```bash
cd /tmp
rm -rf "$WORK_DIR"

echo ""
echo "部署完成"
echo "URL：https://${GITHUB_USER}.github.io/${SLUG}/"
echo ""
echo "提示：首次启用 Pages 需要 1-3 分钟生效；后续部署通常 30 秒内可见。"
```

---

## 常见问题

**Q：访问 URL 显示 404，但仓库里 `<slug>/index.html` 明明存在**

最常见的隐藏原因：**存在同名的项目仓库 `<user>/<slug>` 且也启用了 Pages**。GitHub 路由规则下，`https://<user>.github.io/<slug>/` 会优先解析到 `<user>/<slug>` 这个项目站点，**遮蔽**聚合仓库的同名子目录。

排查与处理：

```bash
# 检查是否存在同名仓库的 Pages
curl -s -o /dev/null -w "%{http_code}\n" \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  "https://api.github.com/repos/<user>/<slug>/pages"
# 200 = 同名仓库 Pages 还活着，正在遮蔽聚合站
# 404 = 没有遮蔽，是别的问题
```

解决方式（任选一种，按破坏性递增）：

1. 禁用同名仓库的 Pages：`DELETE /repos/<user>/<slug>/pages`（只需 `repo` scope）
2. 删除同名仓库本身（需要 `delete_repo` scope，或在 GitHub 网页 Settings 里手动删除）
3. 给 slug 改名（部署时换一个 slug）

处理完后建议往聚合仓库 push 一个空 commit 触发 rebuild：`git commit --allow-empty -m "chore: trigger pages rebuild" && git push`。

**Q：访问 URL 显示 404（无同名仓库遮蔽时）**

1. 最常见原因：html 在仓库里没有重命名为 `index.html`（本版 skill 已强制修复）
2. 进仓库 Settings → Pages 查看部署状态
3. 首次启用需等 1-3 分钟

**Q：旧的部署 `https://<user>.github.io/<repo>/` 一直 404 怎么办**

旧版 skill 是「一 html 一仓库 + 原文件名」模式，仓库根没有 `index.html`，所以根 URL 必然 404。

- 临时访问：在 URL 后追加完整文件名，例如 `https://<user>.github.io/claude-code-token-optimization/claude-code-token-optimization.html`
- 永久方案：用本 skill 重新部署到聚合站；旧仓库可以删除，或在其根目录手动加一个 `index.html`

**Q：想覆盖之前部署过的同名 slug**

直接重跑本 skill，会覆盖 `<slug>/index.html`。

**Q：想下线某个作品**

进入 `<user>.github.io` 仓库，删除对应 `<slug>/` 目录并提交即可。

**Q：聚合站仓库已被我用作博客/首页，部署会覆盖吗**

不会。本 skill 只往 `<slug>/` 子目录写，不动根目录已有内容。

**Q：html 引用了相对路径的 css/js/图片怎么办**

本 skill 是「单文件部署」，仅适合**自包含的 html**（内联样式/脚本/数据 URI）。如果 html 依赖外部资源，需要先把 html 改成自包含，或换用整目录部署的 skill。

**Q：私有仓库支持吗**

免费账号的 `<user>.github.io` 必须是 public。
