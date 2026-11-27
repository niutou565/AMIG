# 1.2.6 发布检查清单（RELEASE_CHECKLIST）

- 检查日期：2026-11-28
- 目标版本：AMIG 1.2.6（1.2.5 维护审计修复版）
- 前置条件：P1-1 已通过专项验收；SQLite / MySQL 全量回归 0 失败；备份恢复演练 PASS

## 1. 版本一致性

| 位置 | 期望 | 实际 | 结果 |
|---|---|---|---|
| `app/config.py` `APP_VERSION` | 1.2.6 | `1.2.6` | ✅ |
| `README.md` 首屏「当前版本」 | 1.2.6 | 1.2.6 | ✅ |
| `README.md` 版本定位表 | 含 1.2.6 行，1.2.5 标记为历史 | 已更新 | ✅ |
| `CHANGELOG.md` | 顶部含 `[1.2.6] - 2026-11-28` | 已写入 | ✅ |
| `docs/VERSIONING.md` 版本线表 | 1.2.6 为活跃维护 | 已更新 | ✅ |
| `docs/MAINTENANCE.md` 维护范围 | 1.2.6 为主维护分支 | 已更新 | ✅ |
| 页面页脚（模板渲染 `APP_VERSION`） | 自动跟随 | 无硬编码版本 | ✅ |

## 2. 依赖与配置

| 项目 | 结论 |
|---|---|
| `requirements.txt` | **未变更**；无新增第三方依赖（守卫使用标准库 `os`/`time`/`threading`/`re`） |
| 新增 `.env` 配置项 | **无**（1.1 秒传播等待为代码常量 `_GUARD_PROPAGATION_SECONDS`，不暴露给运维） |
| 既有配置依赖 | 无新增要求；`BACKUP_DIR` 原本就需可写（备份/切换备份），状态文件复用同一目录 |
| 默认值变化 | 无 |
| 数据库结构 | **无变化**：`SCHEMA_VERSION` 仍为 `6`，`init_db()` 未新增表/列/索引 |

## 3. 数据库升级与迁移脚本

| 项目 | 结论 |
|---|---|
| SQLite 老库升级（1.1.x / 1.2.5 → 1.2.6） | 直接替换代码启动，幂等建表逻辑未变（`tests/test_upgrade_sqlite.py` 通过） |
| 1.2.5 → 1.2.6 | 无结构变更、无数据迁移，直接替换代码 |
| `tools/migrate_sqlite_to_mysql.py` | 未改动 |
| `tools/backup_db.py` / `tools/restore_db.py` | 备份工具未改；恢复工具仅做上一轮 P2 路径安全加固（已在 1.2.5 审计中验证） |
| 后台「切换数据库」 | 行为变化见 §5，需写入发布说明 |

## 4. 部署说明核对（README / DEPLOYMENT_AUDIT 结论沿用）

| 项目 | 结论 |
|---|---|
| Uvicorn 监听回环 | 文档保持「仅 127.0.0.1」，未改 |
| Nginx 反代 `/uploads` | 要求反代到应用（不得 alias），未改 |
| 503 透传 | 无需额外配置（标准 HTTP 状态码，JSON 响应） |
| systemd | 无需变更；`BACKUP_DIR` 需与运行用户可写（既有要求） |
| HTTPS / Cookie Secure / HSTS | 未改动 |
| 新增静态度量 | 无（状态文件在 `BACKUP_DIR`，不在 Web 目录） |

## 5. 发布说明必须包含的行为变化（重要）

1. **切换期间写入范围扩大**：由「仅上传被拒」变为「全部业务写入返回 503，读操作不受影响」；
2. **切换成功后必须重启**：重启前处于「等待重启」状态，旧进程不再写旧库；忘记重启则该状态 30 分钟后自动过期（此时仍需重启才能真正使用新库）；
3. **切换异常中断的处理**：`.db_switch.lock` / `.db_switch.pending_restart` 最长 30 分钟自愈；确认没有正在进行的切换后可手工删除（README §7.2 已补说明）；
4. 切换操作的审计日志现在写入**新库**（重启后可在后台操作日志中看到）。

## 6. 备份恢复说明核对

- README §7.2/§7.3 已补充上述状态文件与写入暂停说明；
- 备份/恢复工具行为未变；
- 本次发布前已完成一次独立演练：`backup_restore_drill.log`（DRILL PASS）。

## 7. 交付物与测试证据

| 项目 | 位置 |
|---|---|
| P1 验收 | `audit/maintenance_1.2.5/final_verification/P1_FIX_VERIFICATION.md` |
| 并发/切换专项 | `CONCURRENCY_TEST_REPORT.md` |
| 全量回归 | `REGRESSION_TEST_REPORT.md` + `test_sqlite_full.log` / `test_mysql_full.log` |
| 安全复审 | `SECURITY_RECHECK.md` |
| 备份恢复演练 | `backup_restore_drill.log` |
| 最终验收 | `FINAL_ACCEPTANCE_REPORT.md` |

## 8. 未验证项（发布时不得声称已覆盖）

| 项目 | 状态 | 说明 |
|---|---|---|
| 真实浏览器 E2E | 未验证 | 仅服务端 TestClient / 单元集成测试 |
| 生产 Linux + Nginx + systemd | 未验证 | 配置模板与代码路径已审查 |
| MySQL 8.4 LTS | 未验证 | 实测 8.0.46 |
| 多 worker 切换实测 | 未验证（设计已覆盖） | 跨进程语义依赖文件锁，未做 2+ worker 压测 |
| 依赖漏洞重扫 | 未执行 | 审计 venv 无 `pip_audit`；依赖未变更，沿用上轮结论 |

## 9. 发布判定

**满足发布条件**（无未解决 P0/P1；双后端全量 0 失败；备份恢复演练 PASS；版本号与文档一致）。

发布前置动作（建议）：
1. 按本清单复核提交内容，仅包含「P0/P2/P1 修复 + 新增测试 + 文档/版本更新」，无功能变更；
2. 以注释标签 `1.2.6` 发布，发布说明采用 §5 的四条行为变化；
3. 发版后补跑一次依赖漏洞扫描（`pip-audit`）并在生产同构环境做一次切换演练验证。
