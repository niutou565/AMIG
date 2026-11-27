# AMIG 1.2.5 备份与恢复审计报告（BACKUP_RESTORE_AUDIT）

审计对象：commit `e0a2b27`（AMIG 1.2.5）
演练原则：全程使用独立测试环境（本地 MySQL 独立测试库 + 临时目录），未触碰任何生产数据。

## 1. 备份工具审查（tools/backup_db.py）

- MySQL：`mysqldump --single-transaction`（InnoDB 一致性快照，不锁表）；
- 行数清单：manifest 记录 created_at / database / 逐表 row_counts，用于恢复前核对；
- `--with-uploads`：uploads/ 与 private_uploads/ 一并归档（tar.gz）；
- `--keep N`：滚动保留份数。

**记录问题（未改动）**：
- [P3-C3] manifest 写入失败时不清理已生成的 final_path 转储文件；
- [P3-C3] manifest 未记录归档 SHA-256，完整性核对依赖解包结果而非哈希。

**时间差问题核查**：数据库转储与上传目录归档非原子（先库后文件）。上传/删除发生在转储之后、归档之前时，可能出现「库中有记录而文件缺失」或反之。工具已通过恢复时 `--check` 的完整性核对暴露该差异；缓解措施为「升级/切换前额外备份」+ 恢复前 check 必须人工确认。属可接受的设计权衡，已记录。

## 2. 恢复工具审查（tools/restore_db.py）

- `--check`：只读预检（库可达、归档可读、manifest 与目标库结构一致）；
- 恢复流程：staging 目录解包 → 数据库逐表核对 → 目录原子交换（`.pre_restore_<stamp>` 备份旧目录）→ 失败可回滚；
- 越界路径防御：拒绝绝对路径、`..` 段、Windows 盘符段、含 `:` 的相对路径（本次审计修复 P2-1 前仅拒绝前两类，Windows 盘符路径 `uploads/C:/x` 经 `os.path.join` 会逃出 staging 目录）。

## 3. 独立环境恢复演练（实际执行，结果 PASS）

演练脚本流程（MySQL 后端）：

1. 建独立测试库 A（`amig_test_` 前缀），插入 1 张已审核图片 + 点赞记录，写入 `site_a/uploads/drill_a.webp`、缩略图、`site_a/private_uploads/pending_b.webp`；
2. `python tools/backup_db.py --with-uploads --json`（环境变量隔离：MYSQL_DATABASE=A、BACKUP_DIR/UPLOADS_DIR/PRIVATE_UPLOADS_DIR 指向临时目录、MYSQLDUMP_PATH/MYSQL_CLIENT_PATH 指向 MySQL 8.0 bin）；
3. 建空测试库 B；
4. `python tools/restore_db.py --check` → 通过；
5. `python tools/restore_db.py --yes --restore-uploads --force-uploads` → 成功；
6. 核对：A/B 两库逐表行数一致；图片与缩略图、待审私有文件齐全；归档目录名（uploads/private_uploads）与配置一致。

**结果：DRILL_RESULT: PASS**（完整日志：backup_restore_drill.log）。

演练期间发现并修正的演练脚本问题（非产品缺陷）：`try_approve_image` 需要 admin_id int；恢复需 `--force-uploads`（目标目录预先存在）；归档根目录名必须为 uploads/private_uploads；样例必须包含缩略图否则完整性核对按设计报缺。

## 4. SQLite 备份恢复

- 在线备份：切换流程内置 `backup_sqlite_source`（SQLite Online Backup API，可在服务运行时安全备份）；
- 常规备份：README 7.4 建议「停止服务 + 复制 data/app.db（含 WAL checkpoint）」；
- [P3-C5] 无独立 CLI 恢复工具（tools/backup_db.py、restore_db.py 为 MySQL 专用）。属功能缺口而非缺陷，建议后续版本补齐，本次不实施（避免新增功能）。

## 5. 恢复后验证清单（工具内置 + 文档指引）

- 行数清单逐表比对；抽样校验；图片文件完整性核对（缺文件即中止并报告）；
- 恢复失败时旧目录以 `.pre_restore_<stamp>` 保留，可手工回滚；
- 恢复后启动：init_db 幂等补齐结构，测试套件中的升级/恢复路径用例覆盖。

## 6. 结论

备份与恢复链路在 MySQL 后端经实际演练验证可用（PASS）；发现并修复 1 个 P2（恢复解包 Windows 路径穿越）；SQLite 独立恢复工具缺口与 manifest 哈希增强作为 P3 记录。
