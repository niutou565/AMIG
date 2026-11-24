# 分支管理规范（BRANCHING）

本文是 AMIG 的分支与发布操作规范，开发、产品、测试均以本文为准。版本差异见 [VERSIONING.md](VERSIONING.md)，维护范围见 [MAINTENANCE.md](MAINTENANCE.md)。

## 1. 分支模型

| 分支 | 类型 | 来源 | 合并去向 | 说明 |
|---|---|---|---|---|
| `main` | 长期 | — | — | 1.2.x 主维护线（MySQL），唯一发布新功能的基线 |
| `1.1.x` | 长期 | `v1.1.1` 标签 | — | 老数据库（SQLite）兼容线，仅接受修复，保持冻结 |
| `feature/<描述>` | 短期 | `main` | `main` | 新功能/重构 |
| `fix/<描述>` | 短期 | 目标版本线 | 对应长期分支 | 缺陷修复；按问题所属版本线选择起点 |
| `docs/<描述>` | 短期 | `main` | `main` | 纯文档变更 |

规则：
- 禁止直接向 `main`、`1.1.x` push 提交；必须经 PR（维护者可用 squash 合并）。
- `main` 与 `1.1.x` 之间**不做整体合并**，也不做互相 cherry-pick；两条线的数据库实现不同，修复需分别实现、分别验证。
- 不再为老数据库创建新的长期版本分支；老库相关改动统一进入 `1.1.x`。
- 分支命名使用小写英文与连字符，禁止中文与空格。

## 2. 提交规范

采用 Conventional Commits：`<type>(<scope>): <subject>`，type 取值：

- `feat`：新功能（仅 `main`）
- `fix`：缺陷修复
- `perf` / `refactor` / `style` / `docs` / `test` / `build` / `chore`
- `release`：发布提交（版本号、CHANGELOG、发布说明）

要求：
- 一次提交只做一件事；不混入无关格式化。
- 提交信息使用中文描述，说明"为什么"而不只是"改了什么"。
- 安全修复在提交正文注明影响面与验证方式。
- 不提交 `.env`、数据库文件、日志、备份、上传文件、`__pycache__` 等本地内容（见 `.gitignore`）。

## 3. PR 流程

1. 从目标长期分支切出 `feature/*` 或 `fix/*`。
2. 开发 + 自测（1.2.x 需全量 `python -m pytest tests -q`；1.1.x 跑 SQLite 回归）。
3. 提交 PR，填写：变更目的、影响范围、验证方式、是否需要双线修复。
4. 评审：至少 1 名维护者审核；涉及安全/数据库/发布流程的变更需 2 人。
5. 合并：默认 squash；合并后删除短期分支。
6. 需要双线修复时，由提交人**另开 PR 到 `1.1.x`**，不得直接 cherry-pick。

## 4. 发布流程

### 4.1 1.2.x 发布（当前主流程）

```bash
# 0) 确认工作区干净、在 main 上、与远程同步
git checkout main && git pull --ff-only
git status --short

# 1) 版本与文档
#    - app/config.py: APP_VERSION = "<新版本>"
#    - CHANGELOG.md: 新增 [<新版本>] 段落
#    - README/文档如涉及功能定位变化需同步

# 2) 发布提交与注释标签
git commit -am "release: AMIG <新版本>"
git tag -a <新版本> -m "AMIG <新版本>：<核心变更摘要>"

# 3) 发布包构建与扫描（必须无测试/临时/本地配置）
python tools/build_release.py --scan-only
python tools/build_release.py

# 4) 推送分支与标签
git push origin main
git push origin <新版本>

# 5) 创建 GitHub Release，附加扫描通过的发布包
gh release create <新版本> dist/amig-<新版本>.zip \
  --title "AMIG <新版本>" --notes-file <发布说明>
```

### 4.2 1.1.x 补丁发布

```bash
git checkout 1.1.x && git pull --ff-only
# 修复 + 回归（SQLite）
git commit -am "fix: <描述>"
git tag -a v1.1.<n> -m "AMIG v1.1.<n>：<修复摘要>"
git push origin 1.1.x
git push origin v1.1.<n>
gh release create v1.1.<n> --title "AMIG v1.1.<n>" --notes "<修复说明>"
```

说明：1.1.x 继续沿用带 `v` 前缀的历史标签风格；1.2.0 起新版本线不带前缀。历史标签与发布记录**不得删除或移动**。

## 5. 发布包内容规范

发布包（GitHub Release 附件）**必须不包含**：

- 测试代码与测试数据：`tests/`
- 本地配置与密钥：`.env`、任何含真实密码的文件
- 运行时数据：`data/*.db*`、`backups/`、`uploads/`、`private_uploads/`、日志
- 开发残留：`__pycache__/`、`*.pyc`、`.pytest_cache/`、`.screenshots/`、`dist/`、临时脚本

`tools/build_release.py` 会自动按白名单打包并扫描，命中以上任一条目时以非 0 退出码终止，禁止发布。

## 6. 标签与发布记录

- 标签必须为**注释标签**，备注包含核心变更说明。
- 标签指向发布提交（`release:` 提交），不得指向未合并的功能分支。
- 发布后如需修正说明，只编辑 Release 描述，不移动标签。
- 已发布版本若要下线，使用 Release 的"设为草稿/删除"并在 CHANGELOG 说明，不删除历史 tag。

## 7. 协作边界

| 角色 | 需要遵守 |
|---|---|
| 开发 | 按落点选分支、遵守提交规范、补测试、不直接 push 长期分支 |
| 测试 | 以 `main` 为 1.2.x 验收基线、以 `1.1.x` 为老库验收基线；发布包抽查不含测试/本地配置 |
| 产品/运营 | 新功能只承诺在 1.2.x 交付；1.1.x 仅承诺兼容与安全修复；发布说明与 EOL 通知 |
| 维护者 | 执行发布流程、配置分支保护、维护标签、跟踪双线修复闭环 |
