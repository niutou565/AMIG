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
```

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

> `WORKERS` 默认保持 `1`。后台 Session 为进程内存存储，多 worker 会导致管理员登录态在进程间跳失；性能优化采用「单 worker + 线程池」方案，无需多进程。

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
