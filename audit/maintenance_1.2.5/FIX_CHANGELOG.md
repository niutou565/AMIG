# AMIG 1.2.5 修复变更记录（FIX_CHANGELOG）

基线 commit：`e0a2b27448b3af2eb6cd621a2e250f2aa4efb9fd`（审计开始时工作区干净）

> **2026-11-28 更新**：P1-1 已按 `P1_1_FIX_PLAN.md` 实施并通过专项验收，详见 `final_verification/P1_FIX_VERIFICATION.md` 与 `final_verification/FINAL_ACCEPTANCE_REPORT.md`。本节以下内容为审计当时的修复记录（共 5 个文件）；P1-1 修复涉及 `app/database.py`、`app/db_migration.py`、`app/main.py`、`app/routes/admin.py`、`app/routes/front.py`、`app/stats.py`、`app/models.py` 与新增 `tests/test_db_switch_guard.py`。

## FIX-1（P0-1）SQLite 下点赞删除/取消报错

- **文件**：`app/models.py`（delete_likes_by_ids、decrement_like_count）、`app/database.py`（新增 greatest_expr）
- **修复前**：SQL 写死 `GREATEST(0, like_count - 1)` / `GREATEST(0, like_count - N)`，SQLite 后端抛 `OperationalError: no such function: GREATEST`，管理员删除点赞与用户取消点赞直接 500。
- **修复后**：经方言助手生成表达式——MySQL 用 `GREATEST(a,b)`，SQLite 用标量 `MAX(a,b)`。行为语义（计数下限 0、同步 like_count）不变。
- **回归**：`tests/test_sqlite_backend.py::TestLikeCountDialect` 4 用例；SQLite 216 全过、MySQL 348 全过（3 轮）。

## FIX-2（P0-2）SQLite 下最近上传 IP 统计报错

- **文件**：`app/models.py`（get_recent_upload_ips）
- **修复前**：SQL 写死 `ANY_VALUE(province)`/`ANY_VALUE(city)`，SQLite 后端抛 `no such function: ANY_VALUE`，后台该页面 500。
- **修复后**：方言分支——SQLite 直接选取 `province as province, city as city`（SQLite 不启用 ONLY_FULL_GROUP_BY，语义与 MySQL ANY_VALUE 一致）；MySQL 保持 ANY_VALUE。
- **回归**：`TestLikeCountDialect::test_get_recent_upload_ips` + 双后端全量套件。

## FIX-3（P2-1）恢复工具上传归档解包的 Windows 路径逃逸

- **文件**：`tools/restore_db.py`（解包守卫，新增 `import re`）
- **修复前**：仅拒绝绝对路径（`/` 前缀）与 `..` 段；形如 `uploads/C:/x` 的成员名经 `os.path.join` 后 `C:` 被当作盘符组件，`dest` 变为盘符相对路径逃出 staging 目录；`file.jpg:ads` 可写 NTFS 备用数据流。
- **修复后**：额外拒绝首段匹配 `^[A-Za-z]:` 与任何含 `:` 的相对路径。
- **验证**：py_compile 通过；9 组守卫用例（正常文件、子目录、`..`、绝对路径、`C:/abs`、`C:rel`、ADS）全部符合预期；修复后 SQLite/MySQL 全量套件 0 失败。

## FIX-4（P2-2）init_db 管理员密码绕过策略

- **文件**：`init_db.py`（create_default_admin，新增导入 validate_password）
- **修复前**：`AMIG_ADMIN_PASSWORD` 环境变量提供的密码直接 bcrypt 入库，不校验 `password_policy`（安装向导路径已有校验，仅此路径漏网），可安装出弱口令管理员。
- **修复后**：显式提供的密码先走 `validate_password`，不满足策略即 `SystemExit` 并给出中文原因；随机生成路径（token_hex(16)）不受影响。
- **验证**：实测 `short` 被拒（「密码长度不能少于 12 个字符」）、强口令创建成功且管理员行存在；安装向导路径（installer/state.py 已有校验）行为不变；双后端全量套件 0 失败。

## 新增测试

- `tests/test_sqlite_backend.py`：`TestLikeCountDialect`（4 用例，见 FIX-1/FIX-2）。

## 明确未修改项（按规则记录）

- P1-1 数据库切换写入拦截：设计缺陷，建议方案见 CODE_AUDIT 第 5 节，未实施；
- P3-C1/C3/C4/C5/C6/C7 及部署加固建议：仅记录。

## 修复后验证汇总

| 套件 | 结果 | 日志 |
|---|---|---|
| SQLite 全量（最终轮） | 216 tests, OK (skipped=105), 0 失败 | test_sqlite_run2.log |
| MySQL 全量（最终轮） | 348 tests, OK (skipped=9), 0 失败 | test_mysql_run3.log |
| 备份恢复独立演练 | PASS | backup_restore_drill.log |
