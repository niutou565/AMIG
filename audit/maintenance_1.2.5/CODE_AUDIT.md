# AMIG 1.2.5 代码审计报告（CODE_AUDIT）

审计对象：`niutou565/AMIG` 分支 `main`，commit `e0a2b27448b3af2eb6cd621a2e250f2aa4efb9fd`（AMIG 1.2.5）
审计日期：2026-11-28
审计范围：全部 Python 源码（app/、tools/、installer/、init_db.py、run.py）、全部测试（tests/）、模板与静态资源、README/文档、启动与部署脚本。

## 1. 审计方法

- 按 5 个模块组（路由 / models+迁移 / 图片与工具 / 模板前端 / 安装器）逐文件审查；
- 所有疑似问题均结合调用路径确认，未仅凭关键词扫描定论；
- 静态工具佐证：ruff 0.17.6、bandit 1.9.4（结果见 `ruff_check.log`、`bandit_scan.log`）。

## 2. 总体结论

代码整体质量良好：请求级连接作用域、方言助手集中管理差异、参数化 SQL 全覆盖、上传校验链完整、异常路径均有清理逻辑。本次审查确认 2 个 P0（数据库方言缺陷，见第 4 节）、1 个 P1 设计缺陷（见第 5 节）、2 个已修复 P2、若干 P3。

## 3. 逐模块审查结论

### 3.1 app/routes/（front / admin / api / media）
- 认证与权限：所有管理路由统一走 `require_admin` 依赖，未发现遗漏；媒体访问先校验图片状态再发文件，pending/rejected 对前台返回 404，无 IDOR。
- 输入校验：分页参数有上限钳制；排序字段白名单；搜索走参数化 LIKE。
- 错误处理：异常日志不泄露 SQL/路径给终端用户。
- 发现问题：
  - [P3-C1] `app/routes/front.py:302-314`：chunked 超限与 Content-Length 超限的检查发生在 `reserve_upload_slot` 之后，先创建预约再失败。因 L408-410 `finally: release_upload_reservation(...)` 会归还额度，实际影响仅为一次多余的预约行写入/删除，不造成额度损失。建议（可选）：将两处大小检查移到预约之前。

### 3.2 app/models.py + app/db_migration.py
- 事务边界清晰（transaction()/SAVEPOINT），命名锁用于上传额度串行化。
- 发现问题：
  - [P0-1][已修复] `delete_likes_by_ids`、`decrement_like_count` 直接使用 MySQL 函数 `GREATEST`，SQLite 下抛 `OperationalError: no such function: GREATEST`。详见 FIX_CHANGELOG。
  - [P0-2][已修复] `get_recent_upload_ips` 使用 MySQL 函数 `ANY_VALUE`，SQLite 下同样报错。详见 FIX_CHANGELOG。
  - [P1-1][记录，不实施] 数据库切换期间的写入拦截不完整，见第 5 节。

### 3.3 app/database.py
- 连接包装器正确处理占位符翻译、空闲 ping、断线重连（2006/2013）、死锁重试（1213/1205，上限 3 次）。
- 连接池：MySQL 线程本地 + 池化；SQLite 线程本地连接 + WAL。
- bandit B110（try/except-pass）共 11 处，全部位于游标/连接清理路径，静默吞掉的仅是 `close()` 自身的异常，属可接受模式；已在 P3 建议中注明可加 debug 级日志。
- [P3-C2] `app/auth.py:245`：局部变量 `cutoff` 赋值未使用（ruff F841），为无害冗余。

### 3.4 图片处理与工具（app/image_utils.py、tools/）
- 解压炸弹防护（像素上限）、EXIF 剥离、统一 WebP 重编码、随机文件名、路径穿越拒绝均在位。
- tools/backup_db.py / restore_db.py：
  - [P2-1][已修复] restore_db.py 上传归档解包对 Windows 盘符路径（如 `uploads/C:/x` 经 `os.path.join` 后逃出 staging 目录）与 NTFS 备用数据流（`:`）未设防。已修复。
  - [P3-C3] backup_db.py：manifest 写入失败时不清理已生成的 final_path 转储文件；manifest 未记录 SHA-256。建议后续增强，未改动（避免引入行为变化）。

### 3.5 模板与前端
- Jinja2 自动转义开启；自定义 filter `highlight` 先 escape 后注入固定 `<mark>` 标签（bandit B704 为误报）。
- CSP nonce 全覆盖，未发现内联事件处理或 `|safe` 滥用。
- 前后台双域名隔离 + 服务端 session + 每会话 CSRF token，匿名接口采用双提交 CSRF。

### 3.6 安装器（installer/）
- state.py 装向导密码已走 `password_policy.validate_password`；[P2-2][已修复] 唯一漏网路径 `init_db.py` 的 `AMIG_ADMIN_PASSWORD` 环境变量不校验策略，已补齐。
- executor 失败路径有回滚与临时文件清理。

## 4. P0 明细（均已修复，回归通过）

| 编号 | 位置 | 触发条件 | 根因 | 影响 | 复现 | 修复 | 回归 |
|---|---|---|---|---|---|---|---|
| P0-1 | app/models.py `delete_likes_by_ids` / `decrement_like_count` | SQLite 后端删除点赞或取消点赞 | SQL 直接写死 `GREATEST()`（MySQL 专属） | SQLite 下管理员删除点赞、用户取消点赞直接 500 | 是（修复前手工复现） | 新增方言助手 `greatest_expr()` | tests/test_sqlite_backend.py `TestLikeCountDialect`（4 用例） |
| P0-2 | app/models.py `get_recent_upload_ips` | SQLite 后端后台查看最近上传 IP | SQL 直接写死 `ANY_VALUE()`（MySQL 专属） | SQLite 下后台该功能 500 | 是（修复前手工复现） | 方言分支：SQLite 直接选取 province/city（不启用 ONLY_FULL_GROUP_BY） | 同上 `test_get_recent_upload_ips` |

测试此前未拦截 P0 的原因：这两个函数仅被 MySQL 专属测试 `tests/test_models_db.py` 覆盖，该文件在 SQLite 下整体跳过（105 个跳过用例的主体）。

## 5. P1-1 设计缺陷记录（未实施修复）

**问题**：数据库切换期间仅上传接口被 `switch_in_progress()`（app/db_migration.py:337）拦截（消费方仅 `models.reserve_upload_quota`）。点赞、浏览量去重、daily_stats、审核/驳回、封禁、设置修改、admin_logs、cooling、login_rate_limits 等全部写入路径在切换期间继续写旧库。

**后果**：`switch_database()`（db_migration.py:651）先对源库做快照备份再复制，快照之后的写入不会带入新库；`.env` 原子更新后需重启生效，重启前旧进程的所有写入最终随旧库被抛弃而丢失。

**建议方案（未实施，避免大规模行为变更）**：
1. 在 `ConnectionWrapper.execute` 层对 INSERT/UPDATE/DELETE/REPLACE 语句统一检查 `switch_in_progress()`，命中即抛受控异常（如 `DatabaseSwitchInProgress`）；
2. 管理路由将该异常映射为 503 + 明确提示「数据库切换中，请稍后再试」；
3. 允许列表：切换流程自身通过独立 adapter 写目标库，不受影响；
4. 回归：新增并发测试——切换锁持有期间并发点赞/审核/设置写入必须全部失败且旧库行数不变。

**不实施理由**：该修改影响全部写入路径的错误语义，需要全面的路由层错误处理改造与双后端并发回归，属于结构性变更；按审计规则第 9 条「设计缺陷无法安全修复时先记录」处理。已如实计入验收结论（部分通过）。

## 6. P3 清单（仅记录，未改动）

| 编号 | 位置 | 内容 |
|---|---|---|
| P3-C1 | app/routes/front.py:302 | 超限检查在预约之后（额度有归还，仅冗余预约行） |
| P3-C2 | app/auth.py:245 | 未使用变量 `cutoff` |
| P3-C3 | tools/backup_db.py | manifest 失败不清理转储文件、无 SHA-256 |
| P3-C4 | app/routes/admin.py:513 | 批量操作 ids 无数量上限（管理员-only + CSRF，风险低） |
| P3-C5 | tools/backup_db.py、restore_db.py | 仅支持 MySQL；SQLite 部署依赖「停服复制文件 / 切换流程内置 Online Backup」（README 7.4 已记载），可考虑后续补齐 |
| P3-C6 | 全局 | ruff 341 条风格类告警（导入排序、datetime.UTC 别名、未使用导入、宽泛 except），无逻辑错误；建议后续引入 lint 配置统一治理 |
| P3-C7 | app/database.py 等 | 11 处 try/except-pass 清理模式，可加 debug 日志 |

## 7. ruff / bandit 误报甄别

- bandit B608（52 条）：全部为方言助手 f-string（`upsert_suffix` 常量分支、`TABLE_ORDER` 白名单表名、`_PK_COLUMNS` 白名单），参数值全部走占位符，无注入路径。
- bandit B324（MD5）：`image_utils.py:592` 用于上传内容去重指纹，非安全用途，误报。
- bandit B105（23 条）：测试夹具口令字面量，误报。
- bandit B310：ip_geo 的 IP 已做公网校验 + URL 编码，目标为固定 API 主机，接受。
- bandit B704：`Markup()` 输入已先 `escape()` 且替换内容为固定标签，误报。
