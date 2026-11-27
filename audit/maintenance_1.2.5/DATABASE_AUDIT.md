# AMIG 1.2.5 数据库审计报告（DATABASE_AUDIT）

审计对象：commit `e0a2b27`（AMIG 1.2.5）
环境：Python 3.14.6 / SQLite 3.50.4 / MySQL 8.0.46（独立测试库 `amig_test_*`，未触碰生产数据）

## 1. 双后端一致性（18 项核查）

| # | 检查项 | 结论 | 说明 |
|---|---|---|---|
| 1 | SQL 方言转换 | 通过 | `upsert_suffix`/`random_unit_expr`/`least_expr`/`greatest_expr` 集中于 database.py，业务层不写死后端语法 |
| 2 | `?` 与 `%s` 占位符 | 通过 | 统一 qmark，MySQL 执行前翻译；LIKE 模式经参数传入避免 `%` 冲突 |
| 3 | UPSERT | 通过 | settings/daily_stats/登录限流等均走 `upsert_suffix`，双后端测试覆盖 |
| 4 | 随机数表达式 | 通过 | `random_unit_expr()`（MySQL RAND / SQLite random） |
| 5 | 日期时间处理 | 通过 | 统一 ISO 字符串存储，无原生 DATETIME 依赖 |
| 6 | 字段类型兼容 | 通过 | schema DDL 双版本定义（_SCHEMA_DDL/_SQLITE_SCHEMA_DDL） |
| 7 | 自增主键 | 通过 | lastrowid 包装兼容双后端 |
| 8 | 唯一索引 | 通过 | view_sessions 唯一索引、likes 防重，双后端 IntegrityError 统一 |
| 9 | 外键与级联 | 通过 | 应用层级联（删除图片时显式清理点赞/浏览记录） |
| 10 | 事务提交/回滚 | 通过 | transaction() 上下文管理器，SQLite BEGIN IMMEDIATE，MySQL 显式 BEGIN |
| 11 | SAVEPOINT | 通过 | 嵌套事务测试覆盖（test_sqlite_backend / test_models_db） |
| 12 | 死锁重试 | 通过 | 1213/1205 自动重试上限 3 次 |
| 13 | MySQL 连接池 | 通过 | 池化 + 空闲 ping + 断线重连（2006/2013） |
| 14 | SQLite WAL | 通过 | WAL 模式 + busy_timeout |
| 15 | 命名锁 | 通过 | MySQL GET_LOCK 跨进程 / SQLite 进程内 threading.Lock（文档明确 WORKERS=1 约束） |
| 16 | 多线程/多 worker 并发 | 通过 | 并发点赞去重、并发上传额度测试覆盖 |
| 17 | 结构自动升级 | 通过 | SCHEMA_VERSION=6 幂等补齐，schema_migrations 版本去重，老库升级测试（test_upgrade_sqlite）覆盖 |
| 18 | SQLite 老版本兼容 | 通过 | 1.1.x → 1.2.5 启动幂等迁移测试通过 |

## 2. P0 方言缺陷（已修复）

- [P0-1] `delete_likes_by_ids` / `decrement_like_count` 写死 `GREATEST()`；[P0-2] `get_recent_upload_ips` 写死 `ANY_VALUE()`。SQLite 下均抛 `OperationalError`。根因：SQL 直接使用 MySQL 专属函数，且仅被 MySQL 专属测试覆盖。修复：新增 `greatest_expr` 助手 + SQLite 方言分支；回归测试 `TestLikeCountDialect` 双后端通过。
- 未发现其他绕过适配层的业务模块：全部写入路径复查均经由 `get_db()`/`transaction()` 与方言助手。

## 3. 迁移审计（SQLite ↔ MySQL）

`app/db_migration.py` 复核结论：

- 目标库必须为空，绝不覆盖已有数据；
- 复制前强制源库备份，备份失败即中止且不写 `.env`；
- 复制在源库只读快照事务内进行（SQLite `BEGIN` / MySQL `REPEATABLE READ + CONSISTENT SNAPSHOT`），复制期间业务写入不会造成半新半旧；
- 逐表复制后做行数 + 首尾抽样校验，失败视为迁移失败、不改 `.env`；
- `.env` 更新走原子写（临时文件 + 替换）；
- 标识符白名单校验（库名 `_IDENTIFIER_RE`，表名来自固定 TABLE_ORDER）；
- `.env` 更新后需重启生效（设计中明示）。

测试：test_migration / test_db_switch 双后端通过；备份恢复独立演练通过（见 BACKUP_RESTORE_AUDIT）。

## 4. P1-1：切换期间写入拦截不完整（记录，未实施）

**核查结论（对应用户重点要求「是否真的停止了所有一致性写入」）：没有。**

- 唯一拦截点：`switch_in_progress()`（db_migration.py:337，锁文件 `BACKUP_DIR/.db_switch.lock`，30 分钟过期自动清除）；
- 唯一消费方：`models.reserve_upload_quota`（上传额度预约）；
- **未拦截的写入路径（逐项核查）**：点赞（insert_like/decrement_like_count）、浏览量（view_sessions/images.views）、daily_stats 统计、审核/驳回/回滚待审（try_approve_image 等）、封禁（ban_list）、设置修改（settings）、管理操作日志（admin_logs）、冷却（cooling）、登录限流（login_rate_limits）。

**数据丢失窗口**：快照备份完成后至进程重启前，上述写入落在旧库；`.env` 已指向新库但旧进程仍运行，重启后旧库数据不再可见（旧库文件保留，手工可救回）。

**建议方案**（见 CODE_AUDIT 第 5 节）：连接层写入守卫 + 路由层 503 映射 + 并发回归测试。属结构性变更，按规则记录不实施。

## 5. 连接池与稳定性

- MySQL：池大小/溢出/超时可配，失效连接自动重建；死锁重试有界。
- SQLite：线程本地连接 + WAL + busy_timeout；文档明确 SQLite 模式 `WORKERS=1`（命名锁为进程内实现，多 worker 下去重/额度约束会失效——README 已警示）。
- 长事务风险低：业务事务均短小；迁移快照事务隔离在独立连接。
