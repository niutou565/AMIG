# P1 修复安全复审（SECURITY_RECHECK）

- 日期：2026-11-28
- 复审范围：本轮 P1-1 修复涉及的全部改动（`app/database.py`、`app/db_migration.py`、`app/main.py`、`app/routes/admin.py`、`app/routes/front.py`、`app/stats.py`、`app/models.py`）及其对既有安全机制的影响
- 方法：逐行审查 + 调用链追踪 + 专项测试（含并发/异常路径）+ bandit 静态扫描

## 1. 结论摘要

| 复审项 | 结论 |
|---|---|
| 死锁 | 未引入 |
| 竞态条件 | 已识别 2 处并加固（负缓存、并发 unlink） |
| 锁释放遗漏 | 无（`finally` 覆盖全部路径） |
| 事务边界错误 | 无（守卫在语句执行前抛出，事务由既有 `transaction()` 回滚） |
| 连接状态不一致 | 无 |
| 异常处理缺陷 | 无阻断性问题；2 处惰性写入已容错 |
| 权限检查绕过 | 无（守卫不影响认证/CSRF/域隔离判定顺序） |
| 请求处理异常 | 切换期写请求统一 503 JSON + `Retry-After`；读请求不受影响 |
| 新的 SQL 注入风险 | 无（新增 SQL 全部常量列名 + 参数化） |
| 原有安全机制削弱 | 无（全部既有安全测试通过） |
| 新增 P0 / P1 / P2 | **无** |

## 2. 逐项分析

### 2.1 死锁

- 守卫只做两件事：正则匹配（无锁）与查询两个文件的 `os.path.exists`/`getmtime`（无数据库锁）；
- 新增的 `_write_guard_cache_lock` 仅在读写一个两键字典时持有（微秒级），**不在持锁期间做文件 IO、SQL 或调用其他模块**；不存在嵌套加锁，也就没有锁顺序反转；
- SQLite 的 `BEGIN IMMEDIATE` 与 MySQL 的行锁/死锁重试逻辑未被改动；守卫在 `_tx_active` 判定与重试循环之外触发，不会与死锁重试交叉；
- 结论：无新增死锁路径。

### 2.2 竞态条件

| 位置 | 竞态 | 处置 |
|---|---|---|
| `switch_blocking_writes` 负缓存（1s） | 多进程下锁建立后最多 1s 可能按旧结果放行 | `switch_database` 在快照前等待 1.1s（`app/db_migration.py:761`），窗口关闭；正结果不缓存，锁释放后写入立即恢复 |
| `_probe_state_file` 的 `os.unlink` | 多线程/多进程同时判定过期并删除 | `except OSError` 后按「未切换」返回（`app/db_migration.py:365-368`） |
| `mark_pending_restart` 与 `clear_pending_restart` | 切换线程与启动流程可能同时操作 | 文件写为覆盖写（幂等）；清除失败静默忽略；状态最终由 mtime 过期兜底 |
| 多个线程同时探测文件 | 仅重复只读 IO | 无副作用，可接受 |
| 并发切换 | 两个管理员/两个 worker 同时点切换 | `acquire_switch_lock` 用 `O_CREAT|O_EXCL` 独占创建；存在 pending 时直接拒绝 |

### 2.3 锁释放遗漏

- `_run_db_switch`（`app/routes/admin.py`）的 `finally: db_migration.release_switch_lock()` 覆盖成功、失败、未捕获异常；
- `release_switch_lock` 本身 `try/except OSError`，不会因删除失败而抛出；
- `switch_database` 三个失败分支（目标相同、备份/复制/校验失败、`mark_pending_restart` 失败、写 `.env` 失败）均不遗留 pending；
- 进程级异常（kill -9）无法执行 `finally`：由 30 分钟过期自愈兜底（P3，见 §4）。

### 2.4 事务边界

- 守卫在 `ConnectionWrapper.execute` / `SqliteConnection.execute` 的**连接可用性检查之后、真正执行之前**抛出，语句未进入数据库；
- 多语句原子操作（如 `add_like_atomic`：点赞 + 图片计数 + 日统计）在 `transaction()` 中执行，首条即可触发异常，`transaction()` 的 `except BaseException` 分支执行 `rollback()`（SQLite 为 `BEGIN IMMEDIATE` 起的真实事务，MySQL 为显式事务）；
- 实测证据：持锁期间 8 线程并发点赞后 `likes=0`、`like_count=0`、`viz`/`daily_stats` 未变化（`test_concurrent_writes_rejected_without_partial_change`）；
- 守卫不拦截 `BEGIN/COMMIT/ROLLBACK/SAVEPOINT`，事务控制语句语义完整。

### 2.5 连接状态一致性

- 异常在获取 cursor 之前抛出，连接对象状态（`_tx_active`、`_last_used`）不被破坏；
- `_tx_active` 由 `transaction()` 正常复位（`finally: _local.tx_depth = depth`），异常路径亦经过；
- 请求级连接由 `DBConnectionMiddleware` 统一归还：MySQL 归还前 `rollback()` + `ping`，SQLite 直接关闭；
- 全量套件（MySQL 351 个执行用例）连续运行未出现连接池耗尽或悬挂事务。

### 2.6 异常处理缺陷

- `mark_pending_restart` 失败 → `MigrationError`，且此时 `.env` 尚未写入 → 站点继续用旧库；
- 写 `.env` 失败 → 先 `clear_pending_restart()` 再重抛，`.env` 逐字节不变（测试比对）；
- 切换成功后审计日志写入被守卫拒绝 → 回退为直接写目标库（`app/routes/admin.py:1349`），不再出现「切换成功但被标记失败」；
- 读路径惰性写入（图片浏览计数、仪表盘日统计）捕获守卫异常后跳过写入，页面仍 200（`app/routes/front.py:216-222`、`app/stats.py:47-51`）——避免修复 P1 引入可用性回归；
- 未发现被静默吞掉的非预期异常：除上述两处显式容错与既有的 `OSError` 收敛外，守卫异常一律向上传播并由全局处理器映射为 503。

### 2.7 权限检查绕过

- 守卫位于数据库访问层，**在路由鉴权之后**执行：`admin_required` / CSRF / 管理域名校验仍按原顺序先判定；
- 503 处理器不读取、不修改任何身份信息，也不跳过任何校验；
- 未新增路由、未修改任何路由的认证装饰器；
- 未放宽 `media.py` 对 `/uploads` 的受控访问；
- 结论：无法通过 503 路径获得未授权数据或操作。

### 2.8 请求处理异常

| 场景 | 行为 |
|---|---|
| 切换期 POST 点赞/审核/设置/封禁/日志 | HTTP 503，body `{"detail": "数据库切换进行中，写入已暂停，请稍后重试"}`，头 `Retry-After: 60` |
| 切换期 GET 页面/接口 | 正常 200（图片详情、列表、搜索、后台切换进度页） |
| 切换期上传 | 前置 `reserve_upload_quota` 返回友好提示；中央守卫兜底 503 |
| 响应体信息量 | 不含路径、SQL、异常堆栈、配置或数据库信息 |

### 2.9 SQL 注入

本轮新增/修改的 SQL 仅两处：

1. `insert_admin_log_into_target`（`app/db_migration.py:784`）：
   ```sql
   INSERT INTO admin_logs (admin_id, admin_name, action, detail, target, ip, created_at)
   VALUES (?,?,?,?,?,?,?)      -- MySQL 下为 %s
   ```
   列名与表名为代码常量；占位符由 `adapter.ph` 按后端生成；全部值参数化；目标库标识符经 `normalize_target` 的 `_IDENTIFIER_RE` 白名单校验（仅字母数字 `_` `$`）。
2. `switch_database` 中新增的 `mark_pending_restart` 为文件写入，不涉及 SQL。

既有 SQL 未改动；`increment_daily_stat` 等动态字段仍走既有白名单。

### 2.10 其他安全面

| 项目 | 结论 |
|---|---|
| 敏感信息写入磁盘 | 状态文件仅含 `pid` 与 UTC 时间戳，无口令/连接串；位于 `BACKUP_DIR`（非 Web 可访问目录） |
| 文件权限/路径 | pending 文件复用 `BACKUP_DIR`，未引入新的可写路径；`os.makedirs(exist_ok=True)` |
| 信息泄露 | 503 文案通用；日志仅记录异常类别 |
| 拒绝服务 | 切换期写入被拒是设计行为；读路径不受影响；负缓存限制文件探测频率 |
| SSRF/重定向/命令注入 | 本轮未新增外部请求或子进程调用（`mysqldump` 路径逻辑未改） |
| 依赖 | `requirements.txt` 未变更；上轮 `pip-audit` 无已知漏洞；本轮审计 venv 未安装 `pip_audit`，**未重新执行依赖漏洞扫描**（如实标注） |

## 3. 静态扫描（本轮重跑）

- 工具：bandit 1.9.4（审计 venv）
- 命令：`bandit -q -r app tests/test_db_switch_guard.py -ll`
- 结果：High 1 / Medium 35 / Low 12（共 7208 行）
  - 唯一 High：`app/image_utils.py:592` B324（`hashlib.md5` 用于图片内容去重，非安全用途）——**上一轮已甄别为误报，本轮未改动该文件**
  - Medium/Low 均为既有模式（Jinja `Markup` 高亮、`subprocess` 调用 mysqldump/mysql 等），与上轮结论一致
- 本轮新增代码未产生新的 High/Medium 告警项。

## 4. 遗留风险（记录，不扩大修改范围）

| 编号 | 级别 | 描述 | 建议 |
|---|---|---|---|
| S-1 | P3 | 切换进程被强制杀死时 `.db_switch.lock` 残留，全站写入暂停最多 30 分钟（读不受影响） | README 已给出人工删除指引；后续可在锁文件加心跳或启动时校验 pid |
| S-2 | P3 | `pending_restart` 残留同理 30 分钟自愈 | 同上 |
| S-3 | P3 | 切换期后台表单操作为 JSON 503，无专用错误页 | 遵守「不改 UI」约束，暂不处理 |
| S-4 | P3 | 离线 CLI（`tools/*.py`）不受守卫（要求停服执行） | 文档已注明；可考虑后续加运行期锁检测 |
| S-5 | P3 | 本轮未重跑依赖漏洞扫描（工具缺失） | 依赖未变更，沿用上轮结论；下次发版前补跑 |

## 5. 判定

本次 P1 修复**未引入**新的死锁、竞态（已加固）、锁释放遗漏、事务边界错误、连接状态不一致、权限绕过或 SQL 注入风险；未削弱任何既有安全机制（CSRF、双域名隔离、上传魔数/解压炸弹防护、受控文件访问、登录限流等相关测试全部通过）。未发现新的 P0/P1/P2 安全问题。
