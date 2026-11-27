# P1-1 修复专项验收报告（P1_FIX_VERIFICATION）

- 项目：AMIG（`niutou565/AMIG`，分支 `main`）
- 验收对象：数据库切换期间写入保护（1.2.5 维护审计 P1-1）
- 基线提交：`e0a2b27448b3af2eb6cd621a2e250f2aa4efb9fd`（1.2.5）
- 验收日期：2026-11-28
- 性质：维护性修复 + 验收，未新增功能、未修改前台 UI、未改动产品定位

## 1. 结论

**P1-1 已真正解决。**

1. 全部业务写入统一经「数据库访问层写入守卫」拦截，不依赖逐个接口改造；
2. 未发现可绕过守卫的业务写入入口（唯一豁免为切换流程自身、启动期建表与离线 CLI 工具，均属合理豁免，见 §4）；
3. 切换成功后新增「等待重启」状态，旧进程不再写入旧库，堵住了 1.2.5 中「成功后至重启前」的真实丢数据窗口；
4. 切换失败/异常时不留残留状态，旧库立即恢复可写；切换操作审计日志直接写入目标库；
5. 双后端（SQLite / MySQL 8.0.46）行为一致，专项测试与全量回归均 0 失败。

## 2. 修复前状态（证据）

验收开始时工作区**不存在**任何 P1 修复实现：

| 检查项 | 结果 |
|---|---|
| `app/database.py` 中写入守卫（`_assert_writable` / `DatabaseSwitchInProgressError`） | 不存在 |
| `app/db_migration.py` 中 `switch_blocking_writes` / `pending_restart` | 不存在 |
| `app/main.py` 中 503 异常处理器 / 启动清理 | 不存在 |
| `tests/test_db_switch_guard.py` | 不存在 |
| `git stash` / 其他分支中的修复代码 | 无 |
| 切换状态检查点 | 仅 `models.reserve_upload_quota`（`app/models.py:1151`）与后台页面展示 |

即 1.2.5 的原始缺陷：切换期间只有上传被拦截，点赞、浏览量、日统计、审核、封禁、设置、操作日志等写入继续进入旧库；切换成功后到重启前，旧进程仍可写入旧库。

## 3. 修复内容（差异清单）

### 3.1 `app/database.py`（+39 行）

| 位置 | 内容 |
|---|---|
| L90 | 新增 `DatabaseSwitchInProgressError(DatabaseError)`，供路由层与异常处理器识别 |
| L95 | `_WRITE_STMT_RE`：匹配 `INSERT/UPDATE/DELETE/REPLACE/CREATE/ALTER/DROP/TRUNCATE/RENAME`，不匹配 `SELECT/PRAGMA/BEGIN/COMMIT/SAVEPOINT` |
| L101 | `_assert_writable(sql)`：命中写语句且 `db_migration.switch_blocking_writes()` 为真时抛异常（延迟导入避免循环依赖） |
| L222 | `ConnectionWrapper.execute`（MySQL 路径）入口调用守卫 |
| L392 | `SqliteConnection.execute`（SQLite 路径）入口调用守卫 |

守卫位于**语句执行前**：异常在真正 `cursor.execute` 之前抛出，因此不存在「语句已执行一半」。

### 3.2 `app/db_migration.py`（+130 行）

| 位置 | 内容 |
|---|---|
| L330-346 | 新增 `_SWITCH_PENDING_RESTART_NAME`、`_WRITE_GUARD_CACHE_TTL=1.0`、`_GUARD_PROPAGATION_SECONDS=1.1` |
| L352 | `switch_pending_restart_path()` |
| L359 | `_probe_state_file(path)`：存在且未超过 1800 秒返回 True；过期则删除并返回 False；`OSError`（并发 unlink）按未切换处理 |
| L371 | `switch_in_progress()`（保持原有语义，供后台页面展示与上传快速拒绝） |
| L376 | `switch_pending_restart()` |
| L381 | `switch_blocking_writes()`：锁 或 pending 任一存在即返回 True；仅对「未切换」负结果做 1 秒缓存 |
| L395 | `reset_write_guard_cache()` |
| L402 | `mark_pending_restart()`：写 pid + 时间戳 |
| L418 | `clear_pending_restart()` |
| L431 | `acquire_switch_lock()`：存在 pending 时拒绝新切换；获取成功后清空守卫缓存 |
| L449 | `release_switch_lock()`：释放后清空缓存，写入立即恢复 |
| L733 | `switch_database()`：成功后先 `mark_pending_restart()` 再写 `.env`；写 `.env` 抛异常则 `clear_pending_restart()` 后重抛 |
| L761 | 快照前等待 1.1 秒（覆盖各 worker 的 1 秒负缓存），消除「锁已建立但仍按缓存放行」窗口 |
| L784 | `insert_admin_log_into_target()`：把切换审计日志直接写入目标库 |

### 3.3 `app/main.py`（+16 行）

| 位置 | 内容 |
|---|---|
| L73 | `@app.exception_handler(DatabaseSwitchInProgressError)` → HTTP 503，`{"detail": ...}` + `Retry-After: 60` |
| L109 | `startup` 中先 `clear_pending_restart()` 再 `init_db()`（新进程已按新 `.env` 连新库） |

### 3.4 `app/routes/admin.py`（+16 行）

- L1343-1355：切换成功后写操作日志；若被守卫拒绝（本进程仍连旧库），改为 `db_migration.insert_admin_log_into_target()` 写入目标库，避免「切换成功却被标记失败」以及「重启后审计链断裂」。

### 3.5 `app/routes/front.py` / `app/stats.py`（读路径容错）

- `app/routes/front.py:216-222`：图片详情页的浏览计数（`should_count_view` / `increment_view` / `increment_daily_stat`）在切换期间跳过写库，页面本身仍正常渲染（200）。
- `app/stats.py:47-51`：后台仪表盘的 `ensure_daily_stats` 在切换期间跳过，仪表盘仍展示已有数据。

> 这两处是 GET 路径上的「惰性写入」，若不处理会使浏览页/仪表盘在切换期间返回 503，属于读取功能回归。

### 3.6 `app/models.py`

- `reserve_upload_quota` 的快速拒绝从 `switch_in_progress()` 改为 `switch_blocking_writes()`，使「切换成功等待重启」期间上传也得到友好提示（中央守卫仍为兜底）。

### 3.7 测试

- 新增 `tests/test_db_switch_guard.py`（12 个用例，双后端）。

## 4. 九项验收核对（含调用链证据）

### 4.1 所有业务写入入口是否受统一保护 — 是

实际写入调用链：

```
HTTP 路由 / 中间件 / 后台线程
   → app.models.* / app.auth.* / app.stats.*
     → app.database.get_db()            （请求作用域 or 线程本地）
       → ConnectionWrapper.execute / SqliteConnection.execute
         → _assert_writable(sql) → db_migration.switch_blocking_writes()
```

经逐模块追踪，以下写入全部落在该链路上，因此全部受保护：

| 业务写入 | 入口函数（示例） |
|---|---|
| 点赞 / 取消点赞 | `models.add_like_atomic`、`delete_like`、`decrement_like_count` |
| 浏览量 / 日统计 | `models.increment_view`、`increment_daily_stat`、`ensure_daily_stats` |
| 图片上传 / 审核 / 编辑 / 删除 | `models.insert_image`、`try_approve_image`、`update_image_status`、`delete_image` |
| 封禁 / 解封 | `models.add_ban`、`remove_ban` |
| 设置 / 友链 / 页面 | `models.set_setting`、`upsert_link`、`upsert_page` |
| 操作日志 / 会话 / 登录限流 | `models.insert_admin_log`、`auth.create_session`、`rotate_session`、`record_login_failure` |
| 上传额度预约 | `models.reserve_upload_quota`（DELETE/INSERT） |
| 拒绝冷却 | `models.increment_reject_cooldown` |
| 浏览会话去重 | `models.should_count_view`（INSERT view_sessions） |

「搜索函数名」之外的实际调用关系确认：`DBConnectionMiddleware` 为每个请求建立 `get_db()` 作用域，所有路由（`front/admin/api/media`）、中间件（管理域名、敏感词加载）与后台线程均只通过 `models`/`auth`/`stats` 访问数据库，未发现直接构造 SQL 的业务代码。

### 4.2 是否存在遗漏的数据库写入路径 — 无业务路径遗漏

全仓库 `sqlite3.connect` / `pymysql.connect` 仅出现于：

| 位置 | 性质 | 是否应受守卫 |
|---|---|---|
| `app/database.py:363/474/1299` | 连接包装器、连接池、`init_db()` 建表 | `init_db` 仅在启动/安装期执行，启动时已清除 pending → 合理豁免 |
| `app/db_migration.py:151/206/289/472/473` | 迁移/备份适配器 | 切换流程自身，必须豁免 |
| `tools/*.py` | 离线 CLI（备份/恢复/检查/迁移） | 要求停服执行，离线豁免（README 已注明） |

`executemany` 仅存在于迁移适配器；业务连接包装器不提供 `executemany`；`conn.raw()` 无业务调用。

### 4.3 切换开始后写入是否被阻断 — 是

- 路由在启动切换线程前 `acquire_switch_lock()` 成功后才开工；
- 锁文件存在 → 所有写语句抛 `DatabaseSwitchInProgressError` → 全局处理器返回 503（含 `Retry-After`）；
- 多 worker 场景：锁是磁盘文件，跨进程可见；`switch_database` 在快照前等待 1.1 秒，覆盖其他进程最长 1 秒的负缓存；
- 实测：持锁期间 8 个并发点赞全部失败，且 `likes`、`like_count` 均未变化。

### 4.4 切换成功后是否仍有旧连接写旧库 — 已消除

1.2.5 的窗口：`switch_database` 成功后释放锁，旧进程在重启前恢复放行写入 → 写入落在旧库并丢失。

修复后：成功后写入 `pending_restart`，旧进程**持续**拒绝写入，直到：
- 新进程启动（`startup` 清除标记），或
- 30 分钟过期自愈（防止忘记重启导致永久停写）。

实测：切换成功后对旧库调用 `set_setting` 抛异常且旧库快照完全不变。

### 4.5 切换失败时能否恢复原库 — 能

| 失败阶段 | 行为 |
|---|---|
| 目标库相同 / 目标非空 / 复制校验失败 | 抛 `MigrationError`，不写 `.env`，不创建 pending，写入立即可用 |
| `mark_pending_restart()` 失败 | 抛 `MigrationError`，此时 `.env` 尚未修改，旧库继续可用 |
| 写 `.env` 抛异常 | 捕获后 `clear_pending_restart()` 再抛出；`.env` 内容不变（测试逐字节比对） |
| 任何阶段异常 | `_switch_worker` 的 `finally` 释放锁并清空守卫缓存 |

### 4.6 锁 / 事务 / 连接 / 切换状态异常释放 — 已验证

- 锁：`_switch_worker` 的 `finally: release_switch_lock()`；`release_switch_lock` 吞掉 `OSError` 并清缓存；
- 事务：守卫在语句执行前抛出；`transaction()` 的 `except` 分支执行 `rollback()`（SQLite 为 `BEGIN IMMEDIATE` 起的真实事务），测试确认无部分修改；
- 连接：异常不改变连接对象状态，请求结束时 `DBConnectionMiddleware` 正常归还/丢弃；测试后全量套件无连接泄漏迹象；
- 状态文件：`mark` 失败即中止且不改 `.env`；`clear` 幂等；过期文件自愈。

### 4.7 多线程 / 多请求竞争 — 已加固

- 守卫缓存用独立 `threading.Lock` 保护，且只在读写字典的瞬间持锁（不持锁做文件 IO / SQL），无锁顺序问题；
- 仅缓存负结果：命中切换状态时每次实时探测，锁释放后立即恢复写；
- `os.unlink` 与 `os.utime` 的并发竞态用 `try/except OSError` 收敛；
- 实测 8 线程与切换同时发生：全部被拒绝、无部分写入；释放后 8 个点赞全部成功且计数一致。

### 4.8 SQLite / MySQL 是否都使用正确机制 — 是

- MySQL：`ConnectionWrapper.execute`（连接池 + 自动重连 + 死锁重试）注入守卫，位于 `_translate_placeholders` 之前、重试循环之外，不会被重试吞掉；
- SQLite：`SqliteConnection.execute`（线程本地连接 + `BEGIN IMMEDIATE`）注入同一守卫；
- 两后端使用同一个状态判定 `switch_blocking_writes()`（磁盘文件），语义一致；
- 专项测试在真实 MySQL 8.0.46 独立库与 SQLite 3.50.4 上均通过。

### 4.9 是否存在通过其他路由 / 后台任务 / 直接调用绕过 — 无

- 后台任务：切换线程自身使用迁移适配器（必要豁免）；其审计日志通过 `insert_admin_log_into_target` 写目标库；
- 安装向导（`installer/`）在全新安装阶段使用迁移适配器，此时不存在业务进程并发；
- 读路径惰性写入（浏览计数、仪表盘日统计）已显式跳过写入而非绕过守卫，页面可用性不受影响。

## 5. 残余风险（均已如实声明，非阻断）

| 编号 | 风险 | 影响 | 处置 |
|---|---|---|---|
| R-1 | 切换进程崩溃残留 `.db_switch.lock` | 全站写入暂停最长 30 分钟（读正常） | 已有 30 分钟自愈；README 补充「确认无切换后可手工删除锁文件」；后续版本可加心跳 |
| R-2 | 负缓存 1 秒窗口（仅跨进程） | 理论上锁建立后 1 秒内的写入可能放行 | `switch_database` 快照前等待 1.1 秒，窗口已关闭；单进程部署无此窗口 |
| R-3 | 切换成功但忘记重启 | 写入持续 503 最长 30 分钟，随后自动恢复（此时新库已生效但旧进程仍连旧库，需重启才真正切库） | 界面与启动日志提示；README 说明必须重启 |
| R-4 | 旧进程在重启前收到写请求 | 返回 JSON 503，后台表单无专用提示页 | 遵守「不改 UI」约束，仅记录为 P3 |
| R-5 | 离线 CLI 工具不受守卫 | 切换期间手工执行备份/恢复存在竞争 | README/工具文档已要求停服执行 |

## 6. 判定

| 验收项 | 结果 |
|---|---|
| P1 是否真正解决 | **是** |
| 是否存在绕过写入保护的业务入口 | **否** |
| 是否存在数据丢失风险（切换相关） | **已消除**（残留见 §5 R-1~R-5，均不导致已确认数据丢失） |
| 双后端专项测试 | SQLite 12/12、MySQL 12/12 通过 |
| 双后端全量回归 | SQLite 228（skipped=105）0 失败；MySQL 360（skipped=9）0 失败 |
