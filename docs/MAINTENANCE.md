# 维护策略（MAINTENANCE）

本文明确 AMIG 各版本线的维护范围、迭代节奏与问题处理流程。版本差异见 [VERSIONING.md](VERSIONING.md)，分支与发布规则见 [BRANCHING.md](BRANCHING.md)。

## 1. 维护范围

### 1.2.6（`main`，当前主维护分支）

1.2.6 是 1.2.5 的维护版本：只包含缺陷与安全修复（含数据库切换期间的写入保护），无新增功能、无数据库结构变化。

维护内容：

- 功能迭代与体验优化
- 缺陷修复、安全修复
- 双数据库后端（SQLite / MySQL）兼容性维护
- 数据库结构演进（`init_db()` 幂等建表 + `schema_migrations` 版本记录）
- 依赖升级、部署与运维工具改进
- 文档同步更新

支持范围：

- SQLite：Python 标准库 `sqlite3`（WAL 模式），建议 `WORKERS=1`
- MySQL：8.0 与 8.4 LTS（InnoDB + utf8mb4）
- 不支持：MySQL 5.7 及更低版本；SQLite 多 worker 部署

### 1.1.x（`1.1.x`，老数据库兼容分支）

维护内容（**仅限**）：

- SQLite 兼容性缺陷修复（WAL / 锁 / 事务行为）
- 安全漏洞修复（与 `main` 同步）
- 阻断性缺陷的最小化补丁（不引入新功能、不重构）

不维护内容：

- 新功能、UI 改版、性能优化（除非属于阻断性问题的最小修复）
- 数据库架构变更
- 依赖大版本升级

> 1.1.x 存量站点建议直接升级到 1.2.5 的 SQLite 模式：数据格式一致，无需迁移，且能获得后续功能与安全修复。

## 2. 迭代节奏

- **1.2.x**：按需发布。安全修复随时以 PATCH 版本发布；功能迭代随 MINOR 版本发布。不承诺固定日历周期。
- **1.1.x**：不定期。仅在出现兼容性/安全问题且无法通过配置规避时发布补丁，其余时间保持冻结。
- **1.0.x 及更早**：停止维护（EOL），不再接受修复。

## 3. 问题处理流程

1. **登记**：Issue 标注 `version/1.2.x` 或 `version/1.1.x`；影响 1.1.x 的兼容问题需注明“需回补 1.1.x”。
2. **定级**：
   - S0 安全/数据丢失：立即处理；
   - S1 功能阻断：进入当前迭代；
   - S2 一般缺陷：排期修复；
   - S3 建议：进入待办池（1.1.x 不接受功能类建议）。
3. **修复落点**：
   - 1.2.x 问题 → `fix/*` → PR → `main`；
   - 1.1.x 问题 → `fix/*` → PR → `1.1.x`；
   - 同时影响两线的问题：先在 `main` 修复并验证（含 SQLite 后端回归），再向 `1.1.x` 单独提交等价最小修复。
4. **验证**：`main` 的 PR 必须至少通过：
   - `DB_TYPE=sqlite python -m unittest discover -s tests -t . -q`
   - `DB_TYPE=mysql python -m unittest discover -s tests -t . -q`（有可用 MySQL 时）
   - 涉及数据库层/迁移/切换的改动必须两套后端都跑。
5. **发布**：见 [BRANCHING.md](BRANCHING.md)；发布必须创建注释标签并记录变更。
6. **回补闭环**：安全修复发布后，若需回补 1.1.x，必须在对应 Issue 跟踪到完成。

## 4. 发布准入

发布 1.2.x 前必须完成：

- 两套后端测试通过（SQLite 必跑；MySQL 在具备环境时必跑，不得伪造结果）
- 老版本升级专项用例通过（`tests/test_upgrade_sqlite.py`）
- 发布包扫描通过：`python tools/build_release.py --scan-only`
- `app/config.py: APP_VERSION`、`CHANGELOG.md`、README、`docs/VERSIONING.md` 已同步
- 发布包构建成功并抽查内容（不含 tests / .env / data / backups / 日志）
- `git diff` 已审查，工作区干净，标签指向发布提交

发布 1.1.x 前必须完成：

- 对应 SQLite 回归用例通过
- CHANGELOG 记录安全/兼容修复内容
- 不包含新功能

## 5. EOL 与归档

- 版本线停止维护后，在 README / CHANGELOG 标注 EOL，并保留其标签与发布记录（不删除历史标签）。
- 1.1.x 进入 EOL 前需提前在 `1.1.x` 分支与 README 中声明“最终版本号”。
- SQLite 运行模式由 1.2.5 继续支持，不随 1.1.x EOL 移除。

## 6. 责任分工

| 角色 | 职责 |
|---|---|
| 维护者（Maintainer） | 分支与标签操作、发布准入审核、回补闭环跟踪 |
| 开发 | 按落点分支提交、补测试（覆盖 SQLite 与 MySQL）、更新文档 |
| 测试 | 双后回归、老版本升级验证、发布包内容抽查 |
| 产品/运营 | 版本范围确认、发布说明与用户公告、EOL 通知 |
