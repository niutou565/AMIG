# AMIG 1.2.5 测试报告（TEST_REPORT）

## 1. 测试环境

| 项 | 值 |
|---|---|
| 操作系统 | Windows（本地开发机） |
| Python | 3.14.6 |
| SQLite | 3.50.4（Python 内置 sqlite3） |
| MySQL | 8.0.46 Community Server（本地独立实例，测试库 `amig_test_*` 前缀） |
| 生产数据 | 全程未触碰；测试使用独立测试库与临时目录 |

## 2. 实际执行命令

```bash
# SQLite
DB_TYPE=sqlite python -m unittest discover -s tests -t . -v
# MySQL
DB_TYPE=mysql python -m unittest discover -s tests -t . -v
```

（Windows PowerShell 下以环境变量 `$env:DB_TYPE` 等价设置执行。）

## 3. 执行结果汇总

| 轮次 | 后端 | 阶段 | 执行数 | 通过 | 失败 | 跳过 | 日志 |
|---|---|---|---|---|---|---|---|
| run1 | MySQL | P0 修复前（基线） | 348 | 339 | 0 | 8 | test_mysql_run1.log |
| run2 | MySQL | P0 修复后 | 348 | 339 | 0 | 9 | test_mysql_run2.log（结果经任务通知确认） |
| run3 | MySQL | 全部修复后（最终） | 348 | 339 | 0 | 9 | test_mysql_run3.log |
| run1 | SQLite | P0 修复后 | 216 | 111 | 0 | 105 | test_sqlite_run1.log |
| run2 | SQLite | 全部修复后（最终） | 216 | 111 | 0 | 105 | test_sqlite_run2.log |

**最终状态：SQLite 与 MySQL 全部执行用例 0 失败；退出码均为 0。**

### 跳过说明（如实标注）

- SQLite 轮次跳过 105 个：主体为 MySQL 专属测试（如 tests/test_models_db.py 整文件），SQLite 运行时按设计跳过。**这些跳过不计入 SQLite 通过数。**
- MySQL 轮次跳过 8~9 个：SQLite 专属/条件跳过用例。
- run1 与 run2/run3 的 MySQL 跳过数相差 1（8 → 9）：为条件跳过用例（依赖环境/时序判定），三轮所有实际执行的用例均通过，无失败。

## 4. 本审计新增的回归测试

`tests/test_sqlite_backend.py` 新增 `TestLikeCountDialect`（4 用例，SQLite 运行）：

1. `test_delete_likes_by_ids_syncs_count`：批量删除点赞后 likes 行数为 0、images.like_count 同步为剩余数；
2. `test_delete_likes_by_ids_floors_count_at_zero`：计数下限不为负；
3. `test_decrement_like_count_floors_at_zero`：取消点赞计数下限不为负；
4. `test_get_recent_upload_ips`：最近上传 IP 统计（ip/upload_count/like_count）双后端语义一致。

未删除、未跳过、未削弱任何既有测试。

## 5. 覆盖核查（对照任务书要求）

| 要求项 | 覆盖情况 |
|---|---|
| 数据库连接/事务/回滚/SAVEPOINT | 已覆盖（test_sqlite_backend.TestTransactions、test_models_db） |
| 并发点赞去重 | 已覆盖（TestConcurrency.test_concurrent_likes_are_deduplicated） |
| 并发上传限流/额度 | 已覆盖（上传额度与并发测试） |
| 并发数据库写入 | 已覆盖 |
| 管理员认证 / CSRF / 域名隔离 | 已覆盖（test_stage2_security、test_admin_domain、test_admin_domains_unit） |
| 文件上传安全 / 图片访问权限 / 文件删除 | 已覆盖（test_upload_hardening、test_phase3_media） |
| 数据库迁移 / 切换 | 已覆盖（test_migration、test_db_switch，双后端） |
| 备份 / 恢复 | 迁移与切换用例覆盖；完整恢复流程经独立环境演练验证（backup_restore_drill.log） |
| 老版本 SQLite 升级 | 已覆盖（test_upgrade_sqlite） |

缺口：切换期间并发写入拦截行为（P1-1 未实施，故无对应测试；若按建议方案实施需新增并发回归测试）。

## 6. 未执行/受限项

- 无真实浏览器端到端测试与真实设备验证（本报告结论基于服务端测试套件 + 静态审查）；
- SQLite 轮次的 105 个 MySQL 专属用例未在 SQLite 轮执行（设计如此）；
- 其余全部按计划执行，无 BLOCKED 项（本机存在可用 MySQL 8.0 测试实例）。
