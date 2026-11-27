# AMIG 1.2.5 最终维护验收报告（FINAL_REPORT）

> **2026-11-28 更新**：P1-1 已完成修复并通过专项验收，验收结论由「部分通过」升级为「通过（满足 1.2.6 发布条件）」。最新结论、测试数据与残余风险见 `final_verification/FINAL_ACCEPTANCE_REPORT.md`；本报告以下内容保留审计当时的原始记录。

审计日期：2026-11-28
对象：`niutou565/AMIG`，分支 `main`，commit `e0a2b27448b3af2eb6cd621a2e250f2aa4efb9fd`（AMIG 1.2.5）
性质：维护性审计（非功能开发）。全程未操作生产数据，未删除用户数据，未改动 UI 与产品定位。

## 1. 执行摘要

按任务书十阶段完成审计。确认并修复 2 个 P0（SQLite 方言缺陷导致核心功能 500）、2 个 P2（恢复工具 Windows 路径逃逸、初始化密码策略绕过）；记录 1 个 P1 设计缺陷（数据库切换期间写入拦截不完整，未实施，附完整方案）；记录 7 项 P3。SQLite 与 MySQL 全量测试套件在修复后均 0 失败；备份/恢复经独立环境演练 PASS；依赖无已知漏洞。

## 2. 修复清单

| 编号 | 严重度 | 摘要 | 状态 |
|---|---|---|---|
| P0-1 | P0 | SQLite 下 GREATEST() 不存在：删除点赞/取消点赞 500 | 已修复+回归 |
| P0-2 | P0 | SQLite 下 ANY_VALUE() 不存在：最近上传 IP 统计 500 | 已修复+回归 |
| P2-1 | P2 | restore_db.py 归档解包 Windows 盘符路径逃逸 | 已修复+验证 |
| P2-2 | P2 | init_db.py AMIG_ADMIN_PASSWORD 绕过密码策略 | 已修复+验证 |
| P1-1 | P1 | 数据库切换期间仅上传被拦截，点赞/审核/统计/设置/封禁/日志等写入继续打旧库，快照后写入随切换丢失 | **记录未实施**（设计缺陷，方案见 CODE_AUDIT §5） |
| P3-C1~C7 | P3 | 冗余预约行、未用变量、manifest 无哈希、批量接口无上限、SQLite 无独立恢复 CLI、ruff 风格债、清理路径静默异常 | 仅记录 |

## 3. 测试结果（实际执行）

| 套件 | 命令 | 结果 |
|---|---|---|
| SQLite 全量 | `DB_TYPE=sqlite python -m unittest discover -s tests -t . -v` | **216 tests, OK (skipped=105)，0 失败** |
| MySQL 全量 | `DB_TYPE=mysql python -m unittest discover -s tests -t . -v` | **348 tests, OK (skipped=9)，0 失败**（基线、P0 修复后、全部修复后三轮均通过） |

- SQLite 轮的 105 个跳过为 MySQL 专属用例，**未计入通过数**；MySQL 轮跳过 8~9 个为 SQLite 专属/条件用例。
- 环境：Python 3.14.6 / SQLite 3.50.4 / MySQL 8.0.46（独立测试库，未触碰生产）。

## 4. 独立验证

- **迁移**：db_migration 双向流程审查通过（空库校验、快照隔离、逐表行数+抽样校验、失败不写 .env、原子写配置）；test_migration/test_db_switch 双后端通过。
- **备份恢复演练**：MySQL 全流程（备份 --with-uploads → --check → --yes --restore-uploads）在独立测试库实际执行，**PASS**（行数一致、文件齐全）。
- **静态分析与依赖**：ruff 341 条（无逻辑错误，P3 记录）；bandit 甄别后无真实漏洞；pip-audit 无已知漏洞；密钥扫描无泄露。

## 5. 交付物清单（本目录）

1. CODE_AUDIT.md — 代码审查（含 P0/P1/P3 明细与误报甄别）
2. SECURITY_AUDIT.md — 安全审计
3. DATABASE_AUDIT.md — 双后端一致性（18 项）+ 迁移 + P1 核查
4. BACKUP_RESTORE_AUDIT.md — 备份恢复审查 + 演练记录
5. TEST_REPORT.md — 测试结果（含跳过说明与覆盖核查）
6. DEPLOYMENT_AUDIT.md — 部署检查与建议
7. FIX_CHANGELOG.md — 修复明细与回归
8. FINAL_REPORT.md — 本报告

测试与扫描日志：test_sqlite_run1/run2.log、test_mysql_run1/run2/run3.log、backup_restore_drill.log、ruff_check.log、bandit_scan.log、pip_audit.log、secret_scan.log。

## 6. 验收结论：**部分通过**

按任务书验收标准逐项核对：

| 标准 | 结果 |
|---|---|
| 没有未解决的 P0 | ✅ 通过 |
| 没有未解决且可复现的 P1 | ❌ **P1-1 可复现且未修复**（切换期间写入不拦截；按审计规则作为设计缺陷记录而非擅自实施） |
| SQLite 全部测试通过 | ✅ 通过（216/216 执行用例） |
| MySQL 全部测试通过 | ✅ 通过（348/348 执行用例，独立测试库） |
| 数据库迁移经独立测试 | ✅ 通过 |
| 备份恢复经实际演练 | ✅ 通过（MySQL 全流程 PASS） |
| 核心功能无回归 | ✅ 通过（上传/审核/点赞/访问控制全量套件验证） |
| 没有未经授权的功能变更 | ✅ 通过（仅 5 文件 Bug/安全修复 + 4 个回归用例） |
| 部署安全问题有明确结论 | ✅ 通过（DEPLOYMENT_AUDIT.md） |
| 所有修复有回归测试 | ✅ 通过（TestLikeCountDialect + 守卫用例 + 双后端全量） |

**未通过项仅 P1-1 一项。** 该问题不是可被外部攻击者利用的漏洞，而是切换数据库这一管理操作的内部一致性风险：切换期间及重启前的业务写入会留在旧库（旧库文件保留，数据可手工救回），且切换流程已有备份兜底。建议在后续版本按 CODE_AUDIT §5 方案（连接层写入守卫 + 503 映射 + 并发回归）实施后再升级为完全通过。

## 7. 使用限制声明

- 本报告基于服务端测试套件、静态分析与独立环境演练；未做真实浏览器端到端与真实设备验证。
- MySQL 测试使用本地 MySQL 8.0.46 独立实例；生产环境（Linux/生产 MySQL）行为未实测，但代码路径与配置模板已逐项审查。
- 报告中不含任何密码、密钥、内网敏感信息。
