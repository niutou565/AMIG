# 并发与切换专项测试报告（CONCURRENCY_TEST_REPORT）

- 测试文件：`tests/test_db_switch_guard.py`（新增，12 个用例）
- 运行命令：
  - `DB_TYPE=sqlite python -m unittest tests.test_db_switch_guard -v`
  - `DB_TYPE=mysql python -m unittest tests.test_db_switch_guard -v`
- 结果：SQLite 12/12 通过；MySQL 12/12 通过（真实 MySQL 8.0.46 独立实例）
- 日志：`audit/maintenance_1.2.5/final_verification/test_sqlite_full.log`、`test_mysql_full.log`（全量含本文件）

## 1. 测试隔离方式

| 后端 | 测试库 | 临时文件 |
|---|---|---|
| SQLite | `tempfile.mkdtemp(prefix="amig_test_sqlite_")` 下的 `app.db`，测试结束删除 | 独立的 `amig_switch_guard_*` 临时目录存放备份/.env/迁移目标 |
| MySQL | `amig_test_<随机 12 位 hex>` 独立库，`tearDownClass` 中 `DROP DATABASE` | 同上 |

- 通过 `config.BACKUP_DIR` 重定向到临时目录，切换状态文件（`.db_switch.lock` / `.db_switch.pending_restart`）不会落在真实 `backups/`；
- 切换流程的「来源库」通过 `migration.current_source` 注入测试库配置，**不会读取 `config` 中的真实数据库**；
- 全程未连接生产库、未操作真实上传目录或真实备份。

## 2. 用例与任务书场景映射

| # | 用例 | 覆盖的任务书场景 | 断言要点（数据最终状态） |
|---|---|---|---|
| 1 | `test_switch_running_blocks_every_business_write` | 切换期间点赞、浏览量、审核、封禁、设置、操作日志（另含日统计、拒绝冷却） | 持锁时 8 类写调用全部抛 `DatabaseSwitchInProgressError`，且 `likes/views/like_count/status/ban_list/admin_logs/cooling/settings` 快照**逐项相等**；释放后同一批调用全部成功，计数按预期 +1 |
| 2 | `test_reads_allowed_while_switch_running` | 读不受影响 | `SELECT 1`、`get_setting`、`get_approved_image_by_id` 正常 |
| 3 | `test_pending_restart_blocks_writes_until_cleared` | 切换成功后旧库写入 | 仅建 pending（无锁）即拒绝写入且状态不变；清除后写入立即恢复 |
| 4 | `test_stale_state_files_self_heal` | 锁超时 | 两个状态文件 mtime 改为 31 分钟前 → 判定为未切换、文件被清除、写入可用 |
| 5 | `test_guard_cache_short_circuits_only_when_free` | 缓存不导致长期误拦 | 1 秒内重复判定只探测一次文件系统；建锁后立即判定为拦截 |
| 6 | `test_read_paths_tolerate_write_guard` | 浏览页/仪表盘不被 503 | 持锁时 `GET /image/{id}` = 200 且浏览计数未变化；`stats.get_dashboard_data()` 正常返回 |
| 7 | `test_http_like_returns_503_while_switch_running` | 切换期间点赞请求 | 持锁 POST `/api/like/{id}` = 503 且 `likes` 行数仍为 0；GET 图片页 200；释放后点赞 200 且 `likes`=1 |
| 8 | `test_concurrent_writes_rejected_without_partial_change` | 多写入与切换同时发生 | 8 线程并发点赞全部抛守卫异常；`likes`=0、`like_count`=0；释放后 8 个点赞全部成功、计数=8 |
| 9 | `test_switch_lock_is_exclusive_and_released` | 锁互斥/释放 | 二次获取返回 False；持锁写被拒；释放后写入成功 |
| 10 | `test_switch_failure_keeps_old_db_writable` | 切换失败后的恢复 | 目标库非空 → `MigrationError`；无 pending、无锁残留、`.env` 逐字节不变、旧库写入成功 |
| 11 | `test_switch_exception_in_env_write_clears_pending` | 切换过程中发生异常 | 写 `.env` 抛 `OSError` → 异常向上传播、pending 已清除、`.env` 不变、旧库写入成功 |
| 12 | `test_switch_success_blocks_old_db_and_logs_to_target` | 切换成功后新库写入验证 | 迁移 `verified=True`；pending 存在；旧库写入被拒且快照不变；目标库 `images`≥1、`settings` 已迁移、`admin_logs` 含 `database_switch`；清除 pending 后恢复写入 |

## 3. 关键实现细节验证

### 3.1 「已确认写入不丢失」

用例 12 断言目标库中存在切换前的业务数据（`images` 行、`settings` 键值），且切换后的审计日志写入目标库；用例 1/8 断言被拒绝的写入不会留下任何行或计数变化。

### 3.2 「被拒绝的写入不产生部分修改」

- 守卫在 `cursor.execute` 之前抛出，单条语句不会执行；
- 多语句原子操作（如 `add_like_atomic` 的「插点赞 + 图片计数 + 日统计」）在 `transaction()` 内首条语句即被拒，`transaction()` 的 `except` 分支回滚；
- 用例 1 与 8 对 `likes`、`like_count`、`views`、`daily_stats` 相关计数做了逐项快照比对。

### 3.3 「切换成功后写入进入正确的库」

- 切换成功后 `.env` 被原子更新（用例 12 由 `migrate`/`switch_database` 流程与既有 `test_db_switch.py` 双重覆盖）；
- 本进程在重启前保持「等待重启」，不再写旧库；
- 新进程启动时清除 pending（`app/main.py` startup），此后写入按新配置进入新库；
- 目标库的内容正确性由用例 12 直接读取目标 SQLite 文件断言（跨后端：源为 MySQL 时目标仍用 SQLite 文件，两条路径都验证）。

## 4. 并发压力与竞态检查

| 项目 | 结论 |
|---|---|
| 8 线程同时写 + 切换 | 全部被拒，无部分写入（用例 8） |
| 守卫缓存竞态 | 独立 `threading.Lock`，仅保护字典读写；多线程同时探测只产生重复只读 IO，无副作用 |
| 状态文件并发删除 | `os.unlink` / `os.utime` 的 `OSError` 已收敛为「未切换」 |
| 切换期间事务回滚 | 由既有 `transaction()` 承担，未被本次修改削弱 |
| 死锁 | 守卫不持有数据库锁，也不在持有 `_write_guard_cache_lock` 时做 IO 或 SQL，不存在新增锁顺序 |

## 5. 双后端结果明细

| 后端 | 用例数 | 通过 | 失败 | 跳过 | 耗时 |
|---|---|---|---|---|---|
| SQLite 3.50.4 | 12 | 12 | 0 | 0 | 约 3.1s |
| MySQL 8.0.46（独立 `amig_test_*` 库） | 12 | 12 | 0 | 0 | 约 11.8s |

## 6. 与既有切换测试的关系

未修改或削弱原有测试：`tests/test_db_switch.py`（迁移往返、非空目标拒绝、切换锁、`.env` 原子写入、后台三次确认）在 SQLite 模式下 5 项全部通过；`tests/test_migration.py` 等双后端迁移用例在 MySQL 模式下全部通过。

## 7. 结论

专项测试覆盖任务书要求的全部 11 个场景，并额外覆盖读路径容错、缓存语义、pending 语义。所有断言均检查数据库最终状态。双后端 0 失败，**P1-1 修复的有效性与并发安全性得到实测验证**。
