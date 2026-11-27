# 全量回归测试报告（REGRESSION_TEST_REPORT）

- 日期：2026-11-28
- 代码状态：P1-1 修复 + 1.2.5 审计遗留修复 + 1.2.6 发布记录（工作区，未提交）
- 环境：Windows / Python 3.14.6 / SQLite 3.50.4 / MySQL 8.0.46（本机独立实例，测试库 `amig_test_*`）

## 1. 执行命令与结果

```bash
DB_TYPE=sqlite python -m unittest discover -s tests -t . -v
DB_TYPE=mysql  python -m unittest discover -s tests -t . -v
```

| 后端 | 总数 | 通过（实际执行） | 失败 | 错误 | 跳过 | 结论 |
|---|---|---|---|---|---|---|
| SQLite | 228 | 123 | 0 | 0 | 105 | `OK (skipped=105)` |
| MySQL | 360 | 351 | 0 | 0 | 9 | `OK (skipped=9)` |

完整日志：
- `audit/maintenance_1.2.5/final_verification/test_sqlite_full.log`
- `audit/maintenance_1.2.5/final_verification/test_mysql_full.log`

> 跳过项不计入通过数。

### 1.1 跳过原因

| 后端 | 跳过数 | 原因 |
|---|---|---|
| SQLite | 105 | 全部是 MySQL 专属用例：`require_mysql()` 在 SQLite 进程中抛 `SkipTest`（避免误用默认 `data/app.db` 污染真实数据）。涉及 `test_stage1_audit`、`test_stage2_security`、`test_stage4_*`、`test_stage5_deploy`、`test_stage6_perf`、`test_stage7_search`、`test_stage8_admin_ux`、`test_admin_domain`、`test_phase3_media`、`test_upload_hardening`、`test_watermark` 等 MySQL 模式用例 |
| MySQL | 9 | 全部是 SQLite 专属类：`test_sqlite_backend`（Schema/UPSERT/事务/命名锁/并发/方言）、`test_upgrade_sqlite`、`test_db_switch.MigrationTest`、`test_db_switch.AdminSwitchRoutesTest` |

两侧合计没有「因缺少依赖/环境失败」的跳过：MySQL 实跑于真实 MySQL 8.0.46 独立实例，未使用 BLOCKED 标记。

### 1.2 与上一轮基线的对比

| 后端 | 1.2.5 审计基线 | 本轮 | 差异 |
|---|---|---|---|
| SQLite | 216（skipped=105） | 228（skipped=105） | +12 = 新增 `test_db_switch_guard.py` |
| MySQL | 348（skipped=9） | 360（skipped=9） | +12 = 同上 |

原有测试**未删除、未跳过、未降低断言**；本轮仅新增 1 个测试文件与 1 个用例（`test_read_paths_tolerate_write_guard` 并入新文件）。

## 2. 核心业务回归核对

以下功能均被全量套件覆盖并通过（SQLite / MySQL 两侧各自实际执行）：

### 2.1 前台

| 功能 | 覆盖用例（示例） | 结果 |
|---|---|---|
| 图片上传（含格式/大小/魔数/解压炸弹/CSRF/协议勾选） | `test_feature_integration.test_upload_*`、`test_security_fixes.test_upload_*`、`test_upload_hardening.*` | 通过 |
| 图片浏览（首页/画廊/详情/分页/排序/搜索） | `test_stage1_audit.test_13_public_and_admin_pages_smoke`、`test_stage7_search.*`、`test_db_switch_guard.test_read_paths_tolerate_write_guard` | 通过 |
| 图片搜索 | `test_stage7_search.*` | 通过 |
| 点赞与取消点赞 | `test_stage1_audit.test_03_insert_like_reports_real_errors`、`test_sqlite_backend.TestLikeCountDialect`、`test_db_switch_guard.*` | 通过 |
| 浏览量统计 | `test_stage6_perf.*`、`test_db_switch_guard.test_read_paths_tolerate_write_guard` | 通过 |
| 图片删除及关联记录清理 | `test_security_fixes.test_admin_delete_keeps_files_when_database_delete_fails`、`test_stage4_ops.*` | 通过 |

### 2.2 后台

| 功能 | 覆盖用例（示例） | 结果 |
|---|---|---|
| 管理员登录 / 登出 / 改密 / 会话轮换 | `test_security_fixes.test_change_password_*`、`test_logout_requires_post_and_csrf`、`test_stage1_audit.test_04/12` | 通过 |
| 图片审核（单张 / 批量） | `test_stage4_ops.*`、`test_stage8_admin_ux.*` | 通过 |
| IP 封禁与解封 | `test_stage2_security.*`、`test_stage4_ops.*` | 通过 |
| 访问限制（限流 / 冷却 / 黑名单） | `test_security_fixes.test_failure_threshold_*`、`test_stage2_security.*` | 通过 |
| 系统设置 | `test_stage1_audit.test_06_admin_settings_rejects_invalid_numbers`、`test_feature_integration.*` | 通过 |
| 操作日志 | `test_stage1_audit.*`、`test_db_switch_guard.*` | 通过 |
| 数据库备份 | `test_stage4_ops.*`、独立演练（§3） | 通过 |
| 数据库恢复 | 独立演练（§3） | 通过 |
| 数据库切换 | `test_db_switch.*`（迁移往返/锁/`.env`/三次确认）、`test_db_switch_guard.*`（新增 12 项） | 通过 |

### 2.3 上传文件与数据库记录一致性

- `test_security_fixes.test_upload_success_persists_pending_image_and_files`、`test_upload_cleans_files_when_database_insert_fails`、`test_admin_delete_keeps_files_when_database_delete_fails`：验证「入库失败清文件」「删库失败保留文件」的一致性策略；
- 备份/恢复演练中恢复上传归档后，恢复工具核对「数据库引用的文件是否齐全」，结果无缺失（§3）。

## 3. 备份 / 恢复独立演练（MySQL）

脚本在临时目录与 `amig_test_*` 独立库中执行（不外传、不触碰生产库与真实 `backups/`）：

1. 建库 A（`amig_test_<token>a`）并写入：1 张图片、1 条点赞、1 条设置、1 条操作日志、2 个上传文件；
2. `tools/backup_db.py --with-uploads --json` → 生成 `.sql` + `_uploads.tar.gz` + `.manifest.json`；
3. 建空库 B（`amig_test_<token>b`），`tools/restore_db.py --check` 校验通过；
4. `tools/restore_db.py --yes --restore-uploads` → 恢复数据库 + 上传文件；
5. 校验：`images=1, likes=1, settings=1, admin_logs=1` 与备份清单一致；`drill.webp`、`drill_thumb.webp` 均恢复。

**结果：DRILL PASS**（`counts_match=true`、`files_match=true`）。
日志：`audit/maintenance_1.2.5/final_verification/backup_restore_drill.log`。

演练还验证了恢复工具的安全行为：当归档顶层目录名与当前 `UPLOADS_DIR` 不一致时**中止**（不会解包到错误位置），符合预期。

## 4. 未覆盖 / 未验证项（如实标注）

| 项目 | 状态 | 说明 |
|---|---|---|
| 真实浏览器端到端（上传/点赞/审核交互） | 未验证 | 仅服务端 TestClient 与单元/集成测试 |
| 生产环境 Linux + Nginx + systemd | 未验证 | 代码路径与配置模板已审查，未在目标机实测 |
| MySQL 8.4 LTS | 未验证 | 实测版本为 8.0.46 |
| 多 worker（WORKERS>1）实测切换 | 未验证（逻辑已覆盖） | 守卫状态为磁盘文件，跨进程语义由代码与单测保证；未做 2+ worker 压测 |
| SQLite 多进程并发 | 不适用 | README 已要求 SQLite 模式 `WORKERS=1` |

## 5. 结论

双后端全量回归 **0 失败、0 错误**；核心前后台功能无回归；备份恢复演练 PASS；P1 修复新增 12 个用例在两种后端均通过。跳过项均为后端专属用例，已如实列出原因，未计入通过数。
