# AMIG 1.2.5 安全审计报告（SECURITY_AUDIT）

审计对象：commit `e0a2b27`（AMIG 1.2.5），分支 `main`
方法：OWASP 风险逐项核查，所有结论均经调用路径确认；工具佐证 bandit 1.9.4 + pip-audit + 自研密钥扫描（误报甄别见 CODE_AUDIT 第 7 节）。

## 1. 认证与会话 — 通过

| 检查项 | 结论 | 依据 |
|---|---|---|
| 管理员登录认证 | 通过 | bcrypt 哈希存储，`app/auth.py` 验证逻辑完整 |
| 密码策略 | 通过（审计后补强） | `app/password_policy.py`：≥12 字符、≤72 字节、弱口令/重复模式拦截。发现 [P2-2]：`init_db.py` 的 `AMIG_ADMIN_PASSWORD` 环境变量绕过策略（安装向导路径已有校验），已修复——显式密码不满足策略即中止初始化 |
| Session 随机性/过期/轮换 | 通过 | 服务端 session（secrets 随机 token），登录成功后轮换 session_id |
| Cookie 属性 | 通过 | HttpOnly + SameSite=Lax + `COOKIE_SECURE`（默认 true） |
| 暴力破解防护 | 通过 | `login_rate_limits` 表按 IP+UA 计数，窗口递增，冷却乘数 |
| 退出登录 | 通过 | session 删除 + Cookie 清除 |

## 2. CSRF 与域名隔离 — 通过

- 前后台双域名隔离：应用层校验 Host 属于配置的管理域名才放行 `/admin`；Nginx 层主域 `location /admin { return 404; }` 纵深防御（README 6.7）。
- CSRF：管理操作全部要求每会话 CSRF token；匿名点赞/上传使用双提交 Cookie 模式；同源校验依赖 `proxy_set_header Host $host`（README 明确警示）。
- 反代头信任：`X-Real-IP`/`X-Forwarded-*` 仅信任 `TRUSTED_PROXIES`（默认 `127.0.0.1,::1`）内的来源，伪造 X-Forwarded-For 不影响限流与封禁判定。

## 3. 文件上传与图片处理 — 通过

- 校验链：扩展名白名单 → Content-Type → 魔数 → Pillow 真实解码 → 像素上限（防解压炸弹）→ 统一 WebP 重编码（顺带清 EXIF）→ 随机文件名。
- 路径穿越/符号链接：保存与读取路径均拒绝越界与软链接。
- 访问控制：`/uploads` 由应用鉴权（media.py 先查状态再发文件），pending/rejected 对前台 404；`private_uploads/` 无任何路由直发；README 明令禁止 Nginx 静态直发 `/uploads`。
- 大小限制：`MAX_UPLOAD_SIZE`（分块读取边读边限）+ `MAX_UPLOAD_BODY_SIZE`（预检 Content-Length）+ 中间件 chunked 超限标记 + Nginx `client_max_body_size 12m`。
- 失败清理：磁盘写入后的 DB 失败路径有孤儿文件清理（front.py L402-407）；额度预约失败归还（L408-410）。
- [P3-C1] 超限检查位于预约之后（额度归还，仅冗余写），见 CODE_AUDIT。

## 4. 数据库与 SQL — 通过

- 全部业务 SQL 参数化；动态标识符仅来自白名单（迁移表清单、排序字段白名单）。
- MySQL 数据库名经 `_IDENTIFIER_RE` 校验后才可拼入 DDL。
- bandit B608 52 条均甄别为方言助手/白名单误报。
- 数据库账号：README 6.3 建议专用最小权限账号；`.env` 权限 600 且不被 Nginx 暴露、不入 Git（check-ignore 验证）。

## 5. 其他 OWASP 项 — 通过

| 项 | 结论 |
|---|---|
| SSRF | 应用无用户可控的出站 URL；IP 归属地查询目标为固定 API，IP 经公网校验+URL 编码 |
| 开放重定向 | 重定向目标均为固定内部路径 |
| 命令注入 | 子进程仅 mysqldump/mysql 客户端，参数列表传参，无 shell=True |
| 信息泄露 | 404 伪装后台路由；错误页不含栈信息；日志不打印口令/token |
| 目录遍历 | 敏感目录 Nginx 兜底拒绝（data/backups/private_uploads/tools/tests/.git/.env） |
| 安全响应头 | CSP(nonce)/HSTS/X-Frame-Options/Referrer-Policy 由中间件统一注入 |
| 限流绕过 | 限流键为 IP+UA 哈希，IP 取值仅信任可信反代；封禁支持 IP/CIDR/UA，均服务端判定 |

## 6. 本次修复的安全问题

| 编号 | 严重度 | 位置 | 问题 | 状态 |
|---|---|---|---|---|
| P2-1 | P2（安全加固） | tools/restore_db.py:352 | 上传归档解包未拒绝 Windows 盘符段与 `:`（备用数据流），恶意归档可写逃出 staging 目录 | 已修复：拒绝 `^[A-Za-z]:` 段及含 `:` 的相对路径；9 组守卫用例验证 |
| P2-2 | P2 | init_db.py:28 | `AMIG_ADMIN_PASSWORD` 不校验密码策略，可安装出弱口令管理员 | 已修复：显式密码走 `validate_password`，不合规即 SystemExit；实测弱口令被拒、强口令创建成功 |

## 7. 依赖与密钥

- pip-audit：**No known vulnerabilities found**（见 pip_audit.log）。
- 密钥扫描：Git 跟踪文件无 .env/.pem/.key；疑似命中 8 条均为测试夹具口令（如 `Admin-Password-123`），无真实泄露。

## 8. 遗留风险

- [P1-1] 数据库切换期间写入拦截不完整（数据一致性，非直接可利用漏洞），见 CODE_AUDIT 第 5 节；已计入验收结论。
- P3-C4 管理批量接口无 ids 上限（需要已认证管理员会话 + CSRF token，实际风险低）。
