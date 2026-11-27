# AMIG 1.2.5 部署与生产环境审计报告（DEPLOYMENT_AUDIT）

审计范围：README 第 6 章部署指引（systemd / Nginx / 权限 / 验证清单）、`.env` 配置参考、启动脚本。按任务要求未直接修改任何服务器配置，以下为检查结论与建议。

## 1. 检查结论

| 检查项 | 结论 | 依据 |
|---|---|---|
| Uvicorn 监听地址 | 通过 | README 明确生产 `HOST=127.0.0.1`，systemd 模板 `Environment="HOST=127.0.0.1"`，仅 Nginx 反代可达 |
| Nginx 转发 Host | 通过 | `proxy_set_header Host $host`，README 6.10 第 5 条明确警示缺失会导致域名隔离与 CSRF 同源校验失效 |
| X-Forwarded-For 伪造 | 通过 | `TRUSTED_PROXIES` 默认 `127.0.0.1,::1`，仅可信来源的 X-Real-IP/X-Forwarded-Proto 被采信；README 明令禁止 `0.0.0.0/0` |
| 前后台域名隔离 | 通过 | 主域 `/admin` 返回 404 + 应用层二次校验（纵深防御） |
| HTTPS 强制 | 通过 | 80 → 443 301 跳转模板；`COOKIE_SECURE=true` 默认；HSTS 由应用注入（`HSTS_MAX_AGE=31536000`） |
| `/uploads` 静态直发 | 通过 | README 明令禁止 alias/root 暴露 `/uploads`，必须走应用鉴权；`/static/` 才使用 alias |
| `.env` Web 可达性 | 通过 | `location ~ ^/(?:\.git|\.svn|\.env)` 404；权限 `chmod 600 .env`；`.env` 不入 Git（check-ignore 验证） |
| data/backups/private_uploads 保护 | 通过 | `location ~ ^/(?:data|backups|private_uploads|logs|tools|tests)` 404；部署后验证清单含逐项 curl 检查 |
| 工具脚本暴露 | 通过 | `tools/` 路径 Nginx 404；验证清单含 `curl -I .../tools/backup_db.py` |
| 文件权限 | 通过 | 目录 755 归属 www-data，`.env` 600 |
| 日志泄露敏感数据 | 通过 | 审查日志调用：无口令/token 打印；异常日志含堆栈但仅服务端可见 |
| systemd 重启/权限 | 通过（有增强空间） | `User=www-data`、`Restart=always`、`RestartSec=5` |
| 多 worker 与后端匹配 | 通过 | SQLite 建议 WORKERS=1（进程内命名锁约束），MySQL 支持多 worker；README 对比表明确 |

## 2. 建议（P3，不改变行为）

1. **systemd 加固指令**（建议追加，非必须）：
   ```ini
   NoNewPrivileges=true
   ProtectSystem=strict
   ProtectHome=true
   PrivateTmp=true
   ReadWritePaths=/www/wwwroot/amig
   ```
2. Nginx 模板可考虑追加 `server_tokens off;` 与现代 TLS 配置（`tls1.2/1.3`、OCSP stapling），属可选优化。
3. `client_max_body_size 12m` 与应用 `MAX_UPLOAD_BODY_SIZE` 的匹配关系建议在文档中给出换算说明（当前 12m > 应用上限，行为正确：Nginx 兜底拦截，应用层精确拦截）。

## 3. 部署安全总评

README 部署章节质量高：敏感路径兜底、双域名隔离、可信代理、目录权限、部署后验证 curl 清单齐备。按其指引部署可满足本项目安全模型的前提（回环监听、Host 正确转发、uploads 不直发）。未发现 P0/P1 级部署配置缺陷。
