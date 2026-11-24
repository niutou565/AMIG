# 版本说明（VERSIONING）

本文说明 AMIG 各版本分支的定位、差异与适用场景。分支管理流程见 [BRANCHING.md](BRANCHING.md)，维护与支持策略见 [MAINTENANCE.md](MAINTENANCE.md)。

## 1. 当前版本线

| 版本线 | 分支 | 数据库 | 定位 | 状态 |
|---|---|---|---|---|
| **1.2.x** | `main` | MySQL 8.0+ | 当前主维护版本，唯一推荐用于新部署 | 活跃维护 |
| **1.1.x** | `1.1.x` | SQLite（单文件，WAL） | 老数据库兼容线，仅修复，不新增功能 | 有限维护（仅兼容/安全修复） |
| 1.0.x | 无独立分支 | SQLite | 历史版本，已停止维护 | 停止维护（EOL） |

> 自 1.2.0 起，**不再为老数据库（SQLite）创建任何独立的版本分支**。所有与老数据库兼容性相关的修复、更新、功能调整，一律归并到 `1.1.x` 分支管理。

## 2. 1.1.x 与 1.2.0 的核心差异

| 维度 | 1.1.x（SQLite） | 1.2.0（MySQL） |
|---|---|---|
| 数据库 | SQLite 单文件 `data/app.db`，WAL 模式 | MySQL 8.0 / 8.4 LTS，InnoDB + utf8mb4 |
| 驱动 | 标准库 `sqlite3` | PyMySQL（`cryptography` 支持 caching_sha2_password） |
| 连接管理 | 线程本地连接，文件级写锁 | 连接池（常驻 + 溢出 + 取连接超时）、请求级连接作用域、空闲 ping 重连 |
| 事务 | `BEGIN IMMEDIATE` + commit | `transaction()`（`BEGIN` / SAVEPOINT 嵌套），归还连接前回滚残留事务 |
| 并发写入 | 单写者，写锁串行 | 行级锁；死锁/锁等待超时单语句自动重试，显式事务整体重试 |
| 上传额度原子化 | `BEGIN IMMEDIATE` 写事务 | `GET_LOCK` 命名锁 |
| 备份 | SQLite Online Backup API | `mysqldump --single-transaction` + 行数清单 |
| 恢复 | 替换数据库文件 + `-wal/-shm` 处理 | 导入 SQL + 恢复前回退副本 + 行数/关系校验 |
| 健康检查 | PRAGMA / integrity_check | 版本/字符集/表与索引/计数漂移/孤儿点赞 |
| 多进程部署 | SQLite 文件锁约束，工作进程数需谨慎 | 原生支持多 worker，Session/限流/额度由数据库共享 |
| 字符集 | 默认 UTF-8，emoji 依赖连接设置 | 强制 utf8mb4 / utf8mb4_unicode_ci |
| 数据迁移 | — | 提供 `tools/migrate_sqlite_to_mysql.py` 一次性迁移 |

功能面（前台、后台、审核、限流、封禁、日志、统计、页面/链接、敏感词、备份恢复等）在两个版本线**保持一致**；1.2.0 只是更换数据库底层并补齐 MySQL 下的并发与运维能力，不是功能重写。

## 3. 适用场景

### 选择 1.2.x（推荐）
- 新站点部署、已有站点扩容、需要多 worker / 更高并发
- 需要标准数据库运维能力（账号权限、备份、监控、主从/云数据库）
- 需要中文与 emoji 严格 utf8mb4 保证
- 可以从 1.1.x 升级：旧 SQLite 数据用迁移工具一次性迁入 MySQL

### 选择 1.1.x（仅限存量）
- 已有 SQLite 站点，短期不具备 MySQL 运维条件
- 仅在遇到缺陷/安全问题时需要最小化补丁，不希望变更数据库架构
- 新项目不建议使用；1.1.x 不新增功能

## 4. 版本号与标签规则

- 语义化版本 `MAJOR.MINOR.PATCH`：破坏性变更升 MAJOR，功能升 MINOR，修复升 PATCH。
- **1.2.0 起标签不带 `v` 前缀**（如 `1.2.0`、`1.2.1`），带注释说明核心变更。
- 历史标签保留原样（`v1.1.0`、`v1.1.1`、`v1.0.x`），不再重命名或移动，避免已发布内容漂移。
- 1.1.x 后续补丁继续使用历史风格标签（`v1.1.2` 起），与旧发布体系保持一致。

## 5. 升级路径

1.1.x → 1.2.x：

1. 备份 SQLite 库文件与 `uploads/`、`private_uploads/`（先停服务）
2. 按 README「MySQL 初始化」建库建号，配置 `.env` 的 `MYSQL_*`
3. 执行 `python tools/migrate_sqlite_to_mysql.py --source data/app.db`
4. `python tools/check_db.py --deep` 复核后启动新版本
5. 观察一个发布周期后再归档旧 SQLite 文件（迁移工具不修改源库）

1.2.x 内部小版本升级：拉取 `main` → 安装依赖 → 重启服务，启动时自动幂等建表。
