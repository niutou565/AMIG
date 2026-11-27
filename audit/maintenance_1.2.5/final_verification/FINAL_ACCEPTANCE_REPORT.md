# AMIG 1.2.5 P1 修复后最终验收报告（FINAL_ACCEPTANCE_REPORT）

- 项目：`niutou565/AMIG`，分支 `main`，基线提交 `e0a2b27`（AMIG 1.2.5）
- 验收日期：2026-11-28
- 目标版本：AMIG 1.2.6（维护性修复版本，无新增功能）
- 性质：P1-1 专项验收 + 全项目最终回归；未添加功能、未修改前台 UI、未改动产品定位与架构

## 1. 任务执行说明（重要）

开始验收时，**仓库中并不存在 P1-1 的修复代码**（`database.py` 无守卫、`db_migration.py` 无 `switch_blocking_writes`/`pending_restart`、`main.py` 无 503 处理器、无 `tests/test_db_switch_guard.py`，`git stash` 与其他分支亦无）。经与用户确认，由本轮按 `audit/maintenance_1.2.5/P1_1_FIX_PLAN.md` 实施修复后再验收。

因此本报告同时包含「修复实施」与「修复验收」，全部改动见 §3 差异清单。

## 2. 最终结论（任务书要求的 8 项）

| 问题 | 结论 |
|---|---|
| P1 是否真正解决 | **是**。全部业务写入经连接层守卫统一拦截；切换成功后「等待重启」状态阻止旧进程写旧库；失败/异常路径无残留状态 |
| 是否存在绕过写入保护的入口 | **业务写入不存在**。豁免仅有：切换流程自身的迁移/备份适配器、启动期 `init_db()` 建表、离线 CLI 工具（`tools/*.py`，要求停服） |
| 是否存在数据丢失风险 | 切换相关的两类丢失窗口（切换中写入、成功后未重启的写入）**均已消除**；残余为 P3 级运维风险（进程崩溃残留状态文件最长 30 分钟冻结写入，读不受影响），不会造成已确认数据丢失 |
| SQLite 测试结果 | 228 个用例，执行 123、通过 123、失败 0、错误 0、跳过 105（均为 MySQL 专属用例） |
| MySQL 测试结果 | 360 个用例，执行 351、通过 351、失败 0、错误 0、跳过 9（均为 SQLite 专属类）；真实 MySQL 8.0.46 独立测试库，**非 BLOCKED** |
| 备份恢复测试结果 | **PASS**（MySQL 独立库 + 临时目录演练：行数一致、上传文件完整恢复；日志 `backup_restore_drill.log`） |
| 是否发现新的 P0/P1/P2 问题 | **无**。新增记录 5 项 P3（见 §6） |
| 是否满足发布条件 | **满足**（前置：发布说明须写明切换期写入暂停与必须重启） |

## 3. 本轮代码差异清单

| 文件 | 变更 | 归属 |
|---|---|---|
| `app/database.py` | 新增 `DatabaseSwitchInProgressError`、`_WRITE_STMT_RE`、`_assert_writable()`；两套连接包装器 `execute` 入口注入守卫（+39 行） | P1-1 |
| `app/db_migration.py` | 新增 pending_restart 状态、`switch_blocking_writes()`（负缓存）、`mark/clear_pending_restart()`、传播等待、`insert_admin_log_into_target()`；`acquire_switch_lock` 拒绝叠加切换（+130 行） | P1-1 |
| `app/main.py` | 全局 503 处理器；`startup` 清除 pending（+16 行） | P1-1 |
| `app/routes/admin.py` | 切换审计日志回写目标库（避免「成功却标记失败」/审计丢失） | P1-1 |
| `app/routes/front.py` | 浏览计数在切换期跳过写库，页面仍 200 | P1-1（可用性） |
| `app/stats.py` | 仪表盘日统计初始化在切换期跳过 | P1-1（可用性） |
| `app/models.py` | 上传快速拒绝改用 `switch_blocking_writes()`；`greatest_expr()` 方言助手 | P1-1 / P0-1 |
| `tests/test_db_switch_guard.py` | 新增 12 个双后端用例 | P1-1 回归 |
| `tests/test_sqlite_backend.py` | 新增 `TestLikeCountDialect` 等回归用例 | P0 回归 |
| `init_db.py` | 环境变量初始化管理员时套用密码强度策略 | P2-2 |
| `tools/restore_db.py` | 归档解包拒绝绝对/盘符路径 | P2-1 |
| `app/config.py` / `README.md` / `CHANGELOG.md` / `docs/*.md` | 版本号 1.2.6、发布记录、切换行为说明 | 发布准备 |

未删除或削弱任何既有测试；未改动前台模板/CSS/JS。

## 4. P1 专项验收要点（详见 `P1_FIX_VERIFICATION.md`）

1. **覆盖方式**：守卫下沉到数据库访问层（`ConnectionWrapper.execute` / `SqliteConnection.execute`），所有经 `get_db()` 的写语句统一拦截，覆盖点赞、浏览量、日统计、审核、封禁、设置、友链、页面、操作日志、会话、登录限流、上传预约、拒绝冷却、浏览会话去重；
2. **调用链追踪**（非函数名匹配）：全仓库直接建连只出现在 `app/database.py`（连接包装器/连接池/启动建表）与 `app/db_migration.py`（迁移适配器）以及离线 `tools/*.py`；业务路由/中间件/后台线程均只经 `models`/`auth`/`stats` 访问数据库；
3. **切换中阻断**：锁文件 → 写语句抛 `DatabaseSwitchInProgressError` → 全局 503（`Retry-After: 60`）；读请求与页面正常；
4. **切换成功后**：`pending_restart` 持续阻断旧库写入，重启后自动清除，30 分钟过期兜底；
5. **失败恢复**：目标非空/校验失败/`mark` 失败/写 `.env` 异常均不修改 `.env`、不残留状态、旧库立即可写；
6. **异常释放**：锁在 `finally` 释放；守卫异常不破坏连接与事务状态；
7. **并发**：8 线程同时写入全部失败且无部分修改；释放后计数准确；
8. **双后端**：同一守卫注入 MySQL 与 SQLite 两条路径，专项测试在真实 MySQL 与 SQLite 上均通过。

## 5. 测试与演练结果汇总

| 项目 | 命令/方式 | 结果 |
|---|---|---|
| SQLite 全量 | `DB_TYPE=sqlite python -m unittest discover -s tests -t . -v` | 228 总数 / 123 通过 / 0 失败 / 105 跳过 → `OK (skipped=105)` |
| MySQL 全量 | `DB_TYPE=mysql python -m unittest discover -s tests -t . -v` | 360 总数 / 351 通过 / 0 失败 / 9 跳过 → `OK (skipped=9)` |
| P1 专项 | `python -m unittest tests.test_db_switch_guard`（双后端） | 12/12 + 12/12 通过 |
| 备份恢复演练 | 独立 MySQL 测试库 + 临时目录 | PASS（行数一致、2 个上传文件完整恢复） |
| 静态扫描 | bandit 1.9.4 | High 1（既有 MD5 去重误报）、Medium 35、Low 12；本轮新增代码无新告警 |
| 依赖扫描 | `pip-audit` | **未执行**（审计 venv 无该工具；依赖未变更，沿用上轮无已知漏洞结论） |

环境：Windows / Python 3.14.6 / SQLite 3.50.4 / MySQL 8.0.46。

跳过说明：SQLite 轮的 105 个跳过全部为 MySQL 专属用例（`require_mysql()` 主动跳过）；MySQL 轮的 9 个跳过全部为 SQLite 专属类。**跳过项未计入通过数。**

## 6. 新发现问题记录（均为 P3，不影响发布）

| 编号 | 级别 | 问题 | 影响 | 处置 |
|---|---|---|---|---|
| N-1 | P3 | 进程被强制杀死时 `.db_switch.lock` 残留，最长 30 分钟内全站写入返回 503（读正常） | 可用性 | 30 分钟自愈；README 给出人工删除指引；后续可加心跳/pid 校验 |
| N-2 | P3 | `pending_restart` 残留同理 | 可用性 | 同上 |
| N-3 | P3 | 多进程下守卫负缓存 1 秒窗口 | 理论一致性 | 切换前等待 1.1 秒已消除该窗口；单进程部署无此问题 |
| N-4 | P3 | 切换期后台表单操作为 JSON 503，无专用提示页 | 体验 | 遵守「不改 UI」约束，仅记录 |
| N-5 | P3 | 离线 CLI 工具不受守卫；本轮未重跑依赖漏洞扫描 | 运维/流程 | 文档要求停服执行；发版后补跑 `pip-audit` |

**未发现新的 P0 / P1 / P2 问题。**

## 7. 发布条件核对

| 验收标准 | 结果 |
|---|---|
| 没有未解决的 P0 | ✅ 通过（P0-1/P0-2 已修复并有回归） |
| 没有未解决且可复现的 P1 | ✅ 通过（P1-1 已修复，12 项专项测试双后端通过） |
| SQLite 全部测试通过 | ✅ 通过（123/123 执行用例） |
| MySQL 全部测试通过 | ✅ 通过（351/351 执行用例，独立测试库） |
| 数据库迁移经过独立测试 | ✅ 通过（`test_migration` / `test_db_switch` 双后端；切换全流程在专项测试中实跑） |
| 备份与恢复经过实际演练 | ✅ 通过（DRILL PASS） |
| 核心上传/审核/点赞/访问控制无回归 | ✅ 通过（全量套件 + 核心业务映射表见 `REGRESSION_TEST_REPORT.md`） |
| 没有未经授权的功能变更 | ✅ 通过（仅缺陷修复、测试与文档/版本更新） |
| 部署安全问题有明确结论 | ✅ 通过（沿用 `DEPLOYMENT_AUDIT.md`；本轮无新部署要求） |
| 所有修复都有回归测试 | ✅ 通过（P0：`TestLikeCountDialect` 等；P1-1：新增 12 项；P2：既有验证） |

## 8. 发布准备状态

- 1.2.6 版本号已统一（`app/config.py`、README、CHANGELOG、docs）；
- `CHANGELOG.md` 已写入 `[1.2.6] - 2026-11-28` 发布记录（含行为变化与修复条目）；
- `requirements.txt` 未变更，无新增依赖；
- 无数据库结构变更（`SCHEMA_VERSION=6` 未变），1.2.5 → 1.2.6 直接替换代码；
- README §7.2 已补充「切换期写入暂停 + 必须重启 + 状态文件自愈」说明。

**发布判定：满足发布条件。** 建议发布说明明确列出四项行为变化（见 `RELEASE_CHECKLIST.md` §5）。

## 9. 使用限制声明

- 本报告基于服务端测试套件、静态分析、代码调用链审查与独立环境演练；**未做真实浏览器端到端测试**。
- MySQL 实测为本地 8.0.46 独立实例；生产 Linux / Nginx / systemd 与 MySQL 8.4、多 worker 压测**未实测**（已逐项标注为未验证）。
- 未触碰生产数据库、生产上传目录与真实备份；全部演练在 `amig_test_*` 库与临时目录完成。
- 报告中不含任何口令、密钥或内网敏感信息。
