# P1-1 修复方案：数据库切换期间全面拦截一致性写入

对应审计问题：`audit/maintenance_1.2.5/CODE_AUDIT.md` §5（P1-1）与 `DATABASE_AUDIT.md` §4。
现状：切换期间仅上传额度预约（`models.reserve_upload_quota`）检查 `switch_in_progress()`，点赞、浏览量、统计、审核、封禁、设置、admin_logs、cooling、登录限流等写入继续打旧库；快照后的写入随切换丢失。

## 设计目标

1. 切换开始 → 进程重启前，**所有**业务写入被统一拒绝，读操作不受影响；
2. 业务模块零改动（守卫下沉到数据库访问层）；
3. 对现有行为零影响：无锁时守卫开销趋近于零；
4. 多 worker（MySQL 模式）下各进程同样生效（锁状态在磁盘文件，天然跨进程）。

## 核心机制：三部分

### 1. 数据库访问层中央写入守卫（app/database.py）

**新增异常**（导出给路由层捕获）：

```python
class DatabaseSwitchInProgressError(DatabaseError):
    """数据库切换进行中，业务写入被临时拒绝。"""
```

**写语句判定**（模块级编译一次）：

```python
_WRITE_STMT_RE = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP|TRUNCATE|RENAME)\b",
    re.IGNORECASE,
)
```

说明：`BEGIN/COMMIT/SAVEPOINT/RELEASE/ROLLBACK/SELECT/PRAGMA` 不命中；DDL 一并拦截（防止切换期间触发结构变更）。

**守卫注入点**（两套连接包装器各一处，复用同一助手）：

```python
def _assert_writable(sql: str) -> None:
    if _WRITE_STMT_RE.match(sql) and db_migration.switch_blocking_writes():
        raise DatabaseSwitchInProgressError("数据库切换进行中，写入已暂停")
```

- MySQL 路径：`ConnectionWrapper.execute`（[app/database.py:356](file:///c:/Users/niutou/Desktop/new91bus/AMIG/app/database.py)）入口处调用；
- SQLite 路径：SQLite 专用连接包装器的 `execute` 入口处同样调用；
- `transaction()` 内层语句同样经过 `conn.execute`，天然覆盖；`commit()` 不拦截（无数据写入）。

**性能**：`switch_blocking_writes()` 带 1 秒 TTL 进程内缓存（见下），无锁时每秒仅 1 次 `os.path.exists`，摊薄后可忽略；命中写锁时每条写语句实时抛异常（读缓存即可，无额外 IO）。

### 2. 切换状态判定升级（app/db_migration.py)

把现有 `switch_in_progress()`（L337，仅检查 `.db_switch.lock`）升级为 `switch_blocking_writes()`，同时检查两个文件：

| 文件 | 存在含义 | 创建者 | 清除者 |
|---|---|---|---|
| `.db_switch.lock` | 切换任务执行中（备份/复制/写 .env） | `acquire_switch_lock` | `switch_database` finally（成败都释放） |
| `.db_switch.pending_restart` | 切换成功，等待旧进程重启 | `switch_database` 成功路径（写完 .env 后） | 应用 `startup`（新进程已加载新库配置） |

**关键补充——堵住现有方案的真实数据丢失窗口**：`switch_database()` 成功后当前只释放锁，旧进程在重启前恢复放行写入，这些写入仍落在将被抛弃的旧库上。新增 `pending_restart` 后：

- 成功路径：释放 `.lock` → 创建 `.pending_restart`（内容仅时间戳，无敏感信息）→ 旧进程写入持续被拒，直至重启；
- 失败路径：仅释放 `.lock`，不创建 pending 文件，写入立即恢复到未变更的旧库（安全）；
- `startup` 清除 pending 文件的理由：重启后的进程已按新 `.env` 连接新库，恢复写入是安全的；若重启失败旧进程继续运行，pending 文件 30 分钟过期（复用 `_SWITCH_LOCK_STALE_SECONDS`）自动放行，运维可人工介入；
- 过期自动清除逻辑沿用现有 stale 判定，仅在非缓存路径执行 `os.unlink`，并包 `try/except OSError`（Windows 下多线程同时 unlink 的竞态）。

### 3. 路由层统一 503 响应（app/main.py）

利用现有异常处理器机制（L73-93），新增：

```python
@app.exception_handler(DatabaseSwitchInProgressError)
async def db_switch_in_progress_handler(request: Request, exc):
    return JSONResponse(status_code=503, content={"detail": "数据库切换进行中，请稍后重试"})
```

- 覆盖前台（点赞/浏览统计）与后台（审核/封禁/设置/日志）全部写入口，无需逐路由修改；
- 上传接口保留 `reserve_upload_quota` 现有的前置检查与友好提示（快路径），中央守卫作为兜底（防其他写路径遗漏）；
- 读请求与静态资源完全不受影响，管理员仍可浏览后台、查看切换进度。

**豁免路径确认**：迁移器 `_SqliteAdapter`/`_MysqlAdapter` 不经 `get_db()`，切换流程自身写目标库不受守卫影响；`startup` 中 `init_db()` 运行时 pending 文件已清除，不受影响。

## 涉及文件与改动量

| 文件 | 改动 |
|---|---|
| app/database.py | 新增异常类、`_WRITE_STMT_RE`、`_assert_writable()`；两处 execute 入口注入（约 +25 行） |
| app/db_migration.py | `switch_blocking_writes()`（含 TTL 缓存、双文件判定、stale 清理）；`switch_database()` 成功路径创建 pending 文件（约 +40 行） |
| app/main.py | 1 个异常处理器 + startup 清除 pending 文件（约 +10 行） |
| tests/test_db_switch_guard.py | 新增回归测试（见下） |
| README 维护章节 | 补充「切换期间写入暂停 + 切换后须重启 + pending 文件 30 分钟自愈」说明 |

## 回归测试计划（tests/test_db_switch_guard.py，双后端运行）

1. **拦截覆盖**：持锁状态下调用 `models.insert_like` / `set_setting` / `try_approve_image` / `increment_reject_cooldown` / `record_login_failure` → 全部抛 `DatabaseSwitchInProgressError`；释放锁后同样调用全部成功；
2. **pending_restart 语义**：仅创建 pending 文件（无 lock）→ 写入被拒；删除后恢复；
3. **stale 自愈**：将文件 mtime 改为 31 分钟前 → `switch_blocking_writes()` 返回 False 且文件被清除；
4. **TTL 缓存**：连续调用缓存命中（可用 monkeypatch 计数 `os.path.exists` 验证 1 秒窗口内只探测一次）；锁创建后最迟 1 秒内守卫生效；
5. **503 响应**：TestClient 持锁后 POST 点赞接口 → HTTP 503；GET 图片列表 → 200；
6. **并发回归**：持锁期间 8 线程并发点赞 → 全部失败、likes 表行数不变；释放后并发点赞去重行为与现有测试一致；
7. **切换流程自身不受阻**：`switch_database`（测试双后端 mini 流程）在守卫启用时仍能正常完成迁移与 .env 写入；
8. 最终全量：SQLite 套件 + MySQL 套件 0 失败（现有 216+348 用例不得回归）。

测试基建：`switch_lock_path()`/pending 路径均可通过 monkeypatch `db_migration` 模块级常量指向测试临时目录，不触碰真实 `BACKUP_DIR`。

## 实施顺序与验收

1. database.py 守卫 + 异常 → 单测 1/6；
2. db_migration.py 状态判定 + pending 文件 → 单测 2/3/4/7；
3. main.py 处理器 + startup 清理 → 单测 5；
4. README 补充说明；
5. 全量双后端回归（验收第 8 条）+ 更新 FIX_CHANGELOG 与 FINAL_REPORT，将 P1-1 从「记录未实施」改为「已修复」，验收结论由「部分通过」升级为「通过」。

## 残余风险（如实声明）

- 锁创建后 ≤1 秒内到达的写入可能漏拦（TTL 窗口）：该写入若发生在快照开始前则正常入快照，若在快照后 1 秒内则属极小残余窗口；如需彻底消除，可让 `acquire_switch_lock` 成功后 `time.sleep(1.1)` 再开始快照（一行代码，代价是切换多等 1 秒）——建议实施时一并加上；
- 进程崩溃残留 pending 文件：30 分钟自愈 + 手工删除兜底；
- 前端对 503 的展示为通用错误提示，不新增专用 UI（遵守不改 UI 约束）。
