# 部署指南（DEPLOY）

本文档面向生产环境部署，以实际生产环境为例：项目目录 `/www/wwwroot/91bus`，服务名 `91bus.service`，Python 3.12.3。

> 与 `README.md` 中的「生产环境部署」互为补充；本文档更贴近宝塔面板 + systemd 的真实部署场景。

---

## 1. 环境要求

| 项 | 要求 |
|---|---|
| 操作系统 | Linux（CentOS / Ubuntu / Debian 均可） |
| Python | 3.10+，推荐 3.12.3（已验证） |
| 数据库 | SQLite（无需独立安装） |
| 反向代理 | Nginx（1.18+，含 `proxy_pass` 模块） |
| 进程管理 | systemd |
| 可选 | `data/ip2region.xdb`（IP 归属地离线库，缺失仅停用该功能） |

### 资源占用参考（实测）

| 项目 | 占用 | 说明 |
|---|---|---|
| 纯源码（约 60 个文件） | ~0.25 MB | 不含 `.git` / 虚拟环境 / 数据 |
| Python 虚拟环境 + 依赖 | ~80–120 MB | Pillow、FastAPI 及传递依赖 |
| `ip2region.xdb`（可选） | ~11 MB | IP 归属地离线库 |
| SQLite 数据库 | 数 KB ~ 数十 MB | 随图片记录数量增长 |
| 运行内存（4 workers 实测） | ~125 MB | 峰值约 125 MB；1 worker 约 30–40 MB |
| 图片处理瞬时内存 | +100–200 MB | 大图缩放/转码时短暂增加 |

### 最低配置要求

| 规格 | 最低 | 推荐 |
|---|---|---|
| CPU | 1 核 | 2 核 |
| 内存 | 512 MB（1 worker） | 1 GB（4 workers） |
| 硬盘 | 1 GB（不含用户图片） | 10 GB 起（含图片存储） |
| 系统 | Linux（Debian/Ubuntu/CentOS） | Ubuntu 22.04+ / Debian 12+ |

> 硬盘需求主要取决于用户上传的图片数量。按单张 WebP 约 100–500 KB 估算，预留足够空间即可。云服务器常见的「1 核 1 GB、20 GB 系统盘」起步即可流畅运行；若流量较大，建议 2 核 2 GB。

> ⚠️ 请勿使用宝塔面板的「Python 项目管理器」直接创建/运行本项目，其系统 Python 3.7 的 pip 元数据存在已知问题。请使用命令行 + 独立虚拟环境部署。

---

## 2. 依赖安装

```bash
cd /www/wwwroot/91bus

# 1) 创建独立虚拟环境
python3 -m venv venv

# 2) 激活虚拟环境
source venv/bin/activate

# 3) 安装依赖
pip install --upgrade pip
pip install -r requirements.txt
```

`requirements.txt` 核心依赖：`fastapi`、`uvicorn`、`starlette`、`python-multipart`、`Pillow`、`bcrypt`、`aiofiles`、`Jinja2`、`httpx`。

> 运行测试（可选，使用内置 unittest，无需额外安装）：
> ```bash
> python -B -m unittest discover -s tests -t . -v
> ```

---

## 3. 部署流程

### 3.1 获取代码

```bash
cd /www/wwwroot
git clone https://github.com/niutou565/AMIG.git 91bus
cd 91bus
```

已部署环境更新代码：

```bash
cd /www/wwwroot/91bus
git fetch origin
git pull origin main
```

### 3.2 配置环境变量（.env）

```bash
cd /www/wwwroot/91bus
cp .env.example .env
vim .env
```

必填项：

```ini
ADMIN_DOMAIN=admin.91bus.top
```

可选项（有默认值，通常无需修改）：

```ini
DATABASE_PATH=data/app.db
UPLOADS_DIR=uploads
IP2REGION_PATH=data/ip2region.xdb
# 可信反向代理地址（逗号分隔），仅这些来源的 X-Real-IP 会被信任
# 默认 127.0.0.1,::1,localhost；如使用 Cloudflare 需把其回源 IP 段加入
TRUSTED_PROXIES=127.0.0.1,::1,localhost
```

> **后台域名可动态修改**：登录后台「站点设置 → 管理域名」修改后立即生效（无需重启服务，中间件每次请求实时读取数据库）。但有两点必须注意：
> 1. **并集生效**：`.env` / systemd 的 `ADMIN_DOMAIN` 与数据库 `admin_domain` 是**并集**（都会被放行），不会互相覆盖。若想彻底停用旧域名，需同时更新 `.env`、systemd 的 `Environment` 与数据库三处。
> 2. **先解析 + 配置 Nginx**：改为新域名前，必须先完成 DNS 解析，并把新域名加入 Nginx 的 `server_name`，否则新域名无法访问到应用。

### 3.3 放置 IP 归属地库（可选）

```bash
mkdir -p data
# 下载 ip2region.xdb 放入 data/ 目录
# 下载地址：https://github.com/lionsoul2014/ip2region/releases
```

### 3.4 初始化数据库

```bash
cd /www/wwwroot/91bus
source venv/bin/activate
python init_db.py
```

脚本会：建表 → 创建管理员 `admin` 并打印随机密码 → 写入默认配置 → 预置页面与友情链接。

> **请立即保存输出的管理员密码**。若遗忘，用 `python reset_password.py` 重置。

> **本次更新（数据库自动迁移）**：服务启动时 `init_db()` 会自动检测并补齐以下结构，缺失则自动 `ALTER TABLE ... ADD COLUMN` 或 `CREATE TABLE IF NOT EXISTS`：
> - `images` 表补充字段：`title`、`region`、`ip`、`ua`、`ua_hash`、`province`、`city`、`content_md5`、`status`、`reject_reason`、`admin_edited`、`reviewed_at`、`views`、`like_count`、`is_duplicate`（历史库缺失的字段自动补空值）
> - `admin_logs` 表补充字段：`ip`、`location`（历史日志无 IP/位置信息）
> - `sessions` 表：新增，用于持久化管理员会话（支持多 worker 共享登录态）
>
> 无需手工执行任何 SQL；部署后重启服务即可自动完成迁移。

### 3.5 配置 systemd 服务

创建 `/etc/systemd/system/91bus.service`：

```ini
[Unit]
Description=91bus anonymous image gallery
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/www/wwwroot/91bus
Environment="ADMIN_DOMAIN=admin.91bus.top"
Environment="WORKERS=1"
ExecStart=/www/wwwroot/91bus/venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启动并设为开机自启：

```bash
sudo systemctl daemon-reload
sudo systemctl enable 91bus.service
sudo systemctl start 91bus.service
sudo systemctl status 91bus.service
```

> `WORKERS` 默认保持 `1`（配合线程池已满足性能需求）。后台 Session 已持久化到数据库，如需多 worker 也能正常共享登录态；但图片处理为 CPU 密集操作，多进程/多线程并发仍需注意资源占用。

### 3.6 配置 Nginx 反向代理

主域名站点（`91bus.top`）关键配置：

```nginx
server {
    listen 443 ssl http2;
    server_name 91bus.top;

    # 静态文件由 Nginx 直出，提升性能
    location /static/ {
        alias /www/wwwroot/91bus/app/static/;
        expires 30d;
    }
    location /uploads/ {
        alias /www/wwwroot/91bus/uploads/;
        expires 1y;
        add_header Cache-Control "public, immutable";
    }

    # 后台路由在主域名伪装 404（双域名架构）
    location /admin { return 404; }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;

        # 上传大图时兜底，避免 504
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
    }
}
```

管理域名站点（`admin.91bus.top`）去除 `location /admin { return 404; }`，其余相同。

> 宝塔面板：在「网站」中分别建主站与后台站，配置反向代理到 `127.0.0.1:8000`，并在配置文件里手动添加上述 `location` 静态别名与超时配置。

### 3.7 目录权限

```bash
cd /www/wwwroot/91bus
mkdir -p data uploads
chown -R www-data:www-data data uploads
chmod -R 755 data uploads
```

---

## 4. 常见问题（FAQ）

**Q：启动后访问返回 502/504？**
A：502 通常是 uvicorn 未启动，检查 `systemctl status 91bus.service` 与日志；504 是上游处理超时，确认已配置 `proxy_read_timeout 120s`，并确认代码已更新到含性能修复的版本。

**Q：主域名访问 `/admin` 返回 404？**
A：属双域名架构的正常隔离，请通过 `admin.91bus.top` 访问后台。

**Q：修改 `ADMIN_DOMAIN` 后进不去后台？**
A：SSH 编辑 `.env` 与 systemd 的 `Environment` 后重启服务。

**Q：忘记管理员密码？**
A：`cd /www/wwwroot/91bus && venv/bin/python reset_password.py`，重置后立即登录修改为强密码。

**Q：上传提示「操作过于频繁」？**
A：上传间隔限流触发。后台「站点设置 → 上传间隔(秒)」可调整（默认 30 秒）；多次被拒会递增冷却。

**Q：IP 归属地不显示？**
A：未放置 `data/ip2region.xdb`，或未重启服务。放置后重启即可。

**Q：代码更新后不生效？**
A：Python 进程不会热加载，务必 `sudo systemctl restart 91bus.service`。

**Q：后台改了管理域名，旧域名为什么还能访问后台？**
A：管理域名是「并集」生效——`.env` / systemd 的 `ADMIN_DOMAIN` 与数据库 `admin_domain` 都会被放行，后台修改只覆盖数据库，不会删除 `.env` 里的旧域名。要彻底停用旧域名，需同时更新 `.env`、systemd 的 `Environment`、数据库三处后重启。

**Q：如何验证数据库迁移是否成功？**
A：服务启动时自动迁移，用下面命令核对关键结构（`PRAGMA table_info` 返回元组，字段名在第二列，用 `r[1]` 取值）：

```bash
cd /www/wwwroot/91bus && python3 -c "import sqlite3; c=sqlite3.connect('data/app.db'); cols={r[1] for r in c.execute('PRAGMA table_info(images)').fetchall()}; print('字段:', all(x in cols for x in ['title','region','ip','like_count','views'])); print('sessions 表:', bool(c.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'\").fetchone())); c.close()"
```

预期输出均为 `True`，即迁移完成。

**Q：双域名隔离靠什么实现？需要 Nginx 单独配置吗？**
A：由应用层中间件 `AdminDomainMiddleware` 保证——非白名单域名访问 `/admin` 直接返回 404，无需在 Nginx 单独加 `location /admin { return 404; }`。多个域名共用同一个 Nginx `server` 块也能正确隔离；Nginx 层的 `location /admin` 只是可选的纵深防御。

---

## 5. 注意事项

1. **配置安全**：`.env` 已被 `.gitignore` 排除，`git pull` 不会覆盖服务器上的真实 `.env`。
2. **端口隔离**：uvicorn 仅绑定 `127.0.0.1:8000`，不对外暴露 8000 端口。
3. **防火墙**：仅开放 80/443。
4. **上传目录安全**：`/uploads/` 建议禁止执行脚本（PHP 等）。
5. **数据库索引**：本次新增的性能索引会在服务启动时通过 `CREATE INDEX IF NOT EXISTS` 自动创建，无需手工迁移。
6. **备份**：
   ```bash
   tar -czf backup-$(date +%Y%m%d).tar.gz data/app.db uploads/
   ```
7. **回滚**：`git revert <commit>` 后重启服务；或恢复代码备份后重启。
