# AMIG · Anonymous Moderated Image Gallery（匿名图片分享站）

一个轻量级的匿名图片分享社区：前台匿名上传/浏览/点赞，后台管理员审核与管理，全站暗色主题，采用双域名架构隔离前后台。

---

## 目录

- [项目概述](#项目概述)
- [核心功能](#核心功能)
- [技术架构](#技术架构)
- [快速开始（本地 Windows）](#快速开始本地-windows)
- [生产环境部署](#生产环境部署)
- [使用指南](#使用指南)
- [目录结构](#目录结构)
- [常见问题解答（FAQ）](#常见问题解答faq)
- [附录：数据表结构](#附录数据表结构)

---

## 项目概述

本项目实现了一个「无登录前台 + 管理员审核后台」的图片分享站点，核心设计目标是：

- **匿名易用**：访客无需注册即可上传、浏览、点赞图片
- **内容可控**：所有上传图片需经管理员审核后才公开展示，拒绝即彻底删除
- **安全防滥用**：内置限流、封禁、蜜罐、验证码、敏感词、图片去重等多重防护
- **轻量高效**：SQLite 单文件数据库 + 纯 SQL 查询，无 ORM、无重框架，低内存占用

适用场景：个人图床、小团队内部分享、兴趣社区、演示/教学项目等。

---

## 核心功能

### 前台功能（访客，无需登录）

| 功能 | 说明 |
|------|------|
| 首页 | 展示随机图片、最新通过图片、热度榜前三模块 |
| 图库 | 浏览所有已通过图片，支持关键词搜索与分页 |
| 热度榜 | 按时间窗口内点赞数排行，窗口天数后台可配 |
| 图片详情 | 查看大图、点赞、浏览计数（同 IP 10 分钟内去重）、上一张/下一张导航 |
| 匿名上传 | 支持 JPG/PNG/WebP，自动 WebP 重编码、剥离 EXIF、生成缩略图 |
| 自定义页面 | 「关于」「站规」「版权」等内容页，后台可编辑 |
| 友情链接 | 页脚展示，后台可管理 |

### 图片上传处理流程

1. 前端校验：文件类型（JPG/PNG/WebP）与大小（≤10MB）
2. 后端校验：扩展名 + 文件魔数（magic bytes）双重校验，防止伪造后缀
3. 安全处理：剥离 EXIF、限制最大像素（防解压炸弹）、最长边缩放至 1920px、转 WebP、生成缩略图
4. 内容去重：计算 MD5，同 IP 24 小时 / 全站 30 天内重复则标记「疑似重复」
5. 入库为 `pending` 状态，等待管理员审核

### 后台功能（管理员，独立管理域名）

| 功能 | 说明 |
|------|------|
| 仪表盘 | 今日上传/通过/拒绝/点赞/浏览、通过率、活跃 IP、封禁数；近 7 天趋势图（上传/点赞/浏览）；点赞地区分布；热门图片（窗口内/累计） |
| 图片审核 | 仅展示待审核图片，支持列表内快速通过/删除、批量通过/删除、进入详情编辑简介与署名后通过 |
| 图片管理 | 展示全部图片，支持搜索、排序（时间/点赞/浏览）、查看详情、**编辑简介**、删除 |
| 用户/IP 管理 | 封禁单个 IP、IP 段（CIDR）、UA 指纹；查看与解除冷却状态 |
| 站点设置 | 站名、简介、Logo、备案号、版权、强调色、限流参数、热度榜参数、验证码开关、去重开关、敏感词、管理域名 |
| 友情链接 | 增删改、排序、显隐控制 |
| 页面管理 | 自定义页面增删改，内容支持 HTML（换行自动转为 `<br>`） |
| 操作日志 | 记录所有管理员操作，支持关键词模糊搜索与高亮，可追溯 |
| 修改密码 | 无需原密码，输入新密码 + 确认即可修改 |

### 审核机制说明

- **拒绝 = 删除**：被拒绝的图片会彻底移除图片文件与数据库记录，不可恢复，无需填写拒绝原因
- 审核列表仅保留「待审核」，已通过/已拒绝不再单独展示（已通过图片见「图片管理」）

---

## 技术架构

### 技术栈

| 层面 | 选型 |
|------|------|
| 语言 | Python 3.10+ |
| Web 框架 | FastAPI |
| 模板引擎 | Jinja2 |
| 数据库 | SQLite（WAL 模式） |
| 图片处理 | Pillow |
| IP 归属地 | ip2region（离线 xdb） |
| 密码哈希 | bcrypt |
| 前端 | 原生 HTML/CSS/JS，无框架 |

### 架构设计

```
浏览器
  │
  ├── 主域名 example.com ──────► nginx ──► uvicorn (127.0.0.1:8000)
  │        （前台，/admin 返回 404）              │
  │                                              ├── app/routes/front.py  前台路由
  ├── 管理域名 admin.example.com ─► nginx ───────┼── app/routes/admin.py  后台路由
  │        （后台管理入口）                       └── app/routes/api.py    点赞 API
  │
  └── 数据层：SQLite (data/app.db) + 图片文件 (uploads/)
```

**双域名架构**：通过中间件校验 `Host` 头，非管理域名的 `/admin` 请求一律伪装为 404，实现前后台物理隔离。

### 模块划分

| 模块 | 职责 |
|------|------|
| `app/main.py` | 应用入口、中间件注册、静态文件挂载、异常处理 |
| `app/config.py` | 全局配置常量与默认值 |
| `app/database.py` | SQLite 连接、WAL、建表、自动迁移 |
| `app/models.py` | 数据操作（纯 SQL 参数化查询） |
| `app/middleware.py` | 管理域名校验、CSRF 中间件、敏感词过滤 |
| `app/auth.py` | 登录、Session（数据库持久化）、密码哈希、登录锁定 |
| `app/limiter.py` | 上传/点赞限流、冷却机制 |
| `app/ban.py` | 封禁检查 |
| `app/ip_geo.py` | ip2region 封装、客户端 IP 获取 |
| `app/image_utils.py` | 图片校验、WebP 转换、缩略图、EXIF 剥离 |
| `app/stats.py` | 统计数据、热度榜缓存 |

### 安全设计

- **CSRF 防护**：中间件自动下发 CSRF Cookie，表单 + Cookie 双重校验
- **蜜罐字段**：隐藏表单字段，机器人填写即拒绝
- **验证码**：上传页可选加减法验证码（后台开关）
- **限流**：上传按 IP/UA/全站三层限制 + 冷却倍数递增；点赞按分钟限流
- **封禁**：IP 精确、CIDR 段、UA 哈希三种维度
- **登录锁定**：连续失败 5 次锁定 15 分钟
- **图片安全**：魔数校验、像素上限防解压炸弹、剥离 EXIF 防隐私泄露
- **敏感词**：简介/署名敏感词过滤（后台配置）
- **密码哈希**：bcrypt 单向加密，不可逆

---

## 快速开始（本地 Windows）

### 1. 环境要求

- Windows 10/11
- Python 3.10 ~ 3.13（推荐，含完整预编译依赖包）

### 2. 获取代码

将项目代码放置到任意目录（以下以 `C:\amig` 为例）。

### 3. 一键启动（推荐）

直接双击项目根目录的 **`start.bat`**，脚本会自动完成：

1. 检查 Python 环境
2. 升级 pip 并安装依赖（`requirements.txt`）
3. 检查 `data/ip2region.xdb`（缺失仅警告，不影响启动）
4. 检查/生成 `.env` 配置文件
5. 初始化数据库（首次运行）
6. 检查并释放端口 8000
7. 启动 uvicorn 服务并自动打开浏览器

启动后访问：`http://127.0.0.1:8000`

> 说明：`start.bat` 已实现完全自动化，全程无需手动输入；若端口被旧实例占用会自动结束旧进程。

### 4. 手动启动（可选）

```bash
# 安装依赖
pip install -r requirements.txt

# ip2region.xdb 已随仓库内置到 data/ 目录（如需更新，从官网下载替换）

# 配置环境变量
cp .env.example .env   # Windows 手动复制重命名即可

# 初始化数据库（输出管理员密码，请妥善保管）
python init_db.py

# 启动
python run.py
# 或
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### 5. 环境变量说明（.env）

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `ADMIN_DOMAIN` | 管理域名，多个逗号分隔，上线必填 | 空 |
| `DATABASE_PATH` | 数据库路径 | `data/app.db` |
| `UPLOADS_DIR` | 上传目录 | `uploads` |
| `IP2REGION_PATH` | ip2region 库路径 | `data/ip2region.xdb` |
| `TRUSTED_PROXIES` | 可信反向代理地址（逗号分隔），仅这些来源的 `X-Real-IP` 会被信任 | `127.0.0.1,::1,localhost` |

---

## 生产环境部署

### 架构说明

- **主域名**（如 `example.com`）：前台访问，`/admin` 一律返回 404
- **管理域名**（如 `admin.example.com`）：后台管理入口

### 1. 环境要求

| 项 | 要求 |
|---|---|
| 操作系统 | Linux（CentOS / Ubuntu / Debian 均可） |
| Python | 3.10+，推荐 3.12.3（已验证） |
| 数据库 | SQLite（无需独立安装） |
| 反向代理 | Nginx（1.18+，含 `proxy_pass` 模块） |
| 进程管理 | systemd |
| 可选 | `data/ip2region.xdb`（IP 归属地离线库，缺失仅停用该功能） |

#### 资源占用参考（实测）

| 项目 | 占用 | 说明 |
|---|---|---|
| 纯源码（约 60 个文件） | ~0.25 MB | 不含 `.git` / 虚拟环境 / 数据 |
| Python 虚拟环境 + 依赖 | ~80–120 MB | Pillow、FastAPI 及传递依赖 |
| `ip2region.xdb`（已内置） | ~11 MB | IPv4 归属地离线库 |
| `ip2region_v6.xdb`（已内置） | ~36 MB | IPv6 归属地离线库 |
| SQLite 数据库 | 数 KB ~ 数十 MB | 随图片记录数量增长 |
| 运行内存（4 workers 实测） | ~125 MB | 峰值约 125 MB；1 worker 约 30–40 MB |
| 图片处理瞬时内存 | +100–200 MB | 大图缩放/转码时短暂增加 |

#### 最低配置要求

| 规格 | 最低 | 推荐 |
|---|---|---|
| CPU | 1 核 | 2 核 |
| 内存 | 512 MB（1 worker） | 1 GB（4 workers） |
| 硬盘 | 1 GB（不含用户图片） | 10 GB 起（含图片存储） |
| 系统 | Linux（Debian/Ubuntu/CentOS） | Ubuntu 22.04+ / Debian 12+ |

> 硬盘需求主要取决于用户上传的图片数量。按单张 WebP 约 100–500 KB 估算，预留足够空间即可。云服务器常见的「1 核 1 GB、20 GB 系统盘」起步即可流畅运行；若流量较大，建议 2 核 2 GB。

> ⚠️ 请勿使用宝塔面板的「Python 项目管理器」直接创建/运行本项目，其系统 Python 3.7 的 pip 元数据存在已知问题。请使用命令行 + 独立虚拟环境部署。

### 2. 获取代码

```bash
cd /www/wwwroot
git clone https://github.com/niutou565/AMIG.git amig
cd amig
```

已部署环境更新代码：

```bash
cd /www/wwwroot/amig
git fetch origin
git pull origin main
```

### 3. 依赖安装

```bash
cd /www/wwwroot/amig

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

> 若服务器为 Debian/Ubuntu 且无法创建虚拟环境，需先安装 `python3-venv`、`python3-full`；pip 安装若受 PEP 668 限制，可加 `--break-system-packages` 或直接使用系统 `python3`。

### 4. 配置环境变量（.env）

```bash
cd /www/wwwroot/amig
cp .env.example .env
vim .env
```

必填项：

```ini
ADMIN_DOMAIN=admin.example.com
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

### 5. 放置 IP 归属地库（可选）

```bash
mkdir -p data
# 下载 ip2region.xdb 放入 data/ 目录
# 下载地址：https://github.com/lionsoul2014/ip2region/releases
```

### 6. 初始化数据库

```bash
cd /www/wwwroot/amig
source venv/bin/activate
python init_db.py
```

脚本会：建表 → 创建管理员 `admin` 并打印随机密码 → 写入默认配置 → 预置页面与友情链接。

> **请立即保存输出的管理员密码**。若遗忘，用 `python reset_password.py` 重置。

> **数据库自动迁移**：服务启动时 `init_db()` 会自动检测并补齐以下结构，缺失则自动 `ALTER TABLE ... ADD COLUMN` 或 `CREATE TABLE IF NOT EXISTS`：
> - `images` 表补充字段：`title`、`region`、`ip`、`ua`、`ua_hash`、`province`、`city`、`content_md5`、`status`、`reject_reason`、`admin_edited`、`reviewed_at`、`views`、`like_count`、`is_duplicate`（历史库缺失的字段自动补空值）
> - `admin_logs` 表补充字段：`ip`、`location`（历史日志无 IP/位置信息）
> - `sessions` 表：新增，用于持久化管理员会话（支持多 worker 共享登录态）
>
> 无需手工执行任何 SQL；部署后重启服务即可自动完成迁移。

### 7. 配置 systemd 服务

创建 `/etc/systemd/system/amig.service`：

```ini
[Unit]
Description=AMIG anonymous image gallery
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/www/wwwroot/amig
Environment="ADMIN_DOMAIN=admin.example.com"
Environment="WORKERS=1"
ExecStart=/www/wwwroot/amig/venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启动并设为开机自启：

```bash
sudo systemctl daemon-reload
sudo systemctl enable amig.service
sudo systemctl start amig.service
sudo systemctl status amig.service
```

> `WORKERS` 默认保持 `1`（配合线程池已满足性能需求）。后台 Session 已持久化到数据库，如需多 worker 也能正常共享登录态；但图片处理为 CPU 密集操作，多进程/多线程并发仍需注意资源占用。若需更高并发，可设为 `4`（实测 4 workers 内存约 125 MB），并同步修改 `Environment="WORKERS=4"` 与 `--workers 4`。

### 8. 配置 Nginx 反向代理

#### 方案一：普通 nginx

主域名站配置 `/etc/nginx/sites-available/example.com`：

```nginx
server {
    listen 80;
    server_name example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name example.com;

    ssl_certificate     /etc/ssl/certs/example.com.pem;
    ssl_certificate_key /etc/ssl/private/example.com.key;

    root /www/wwwroot/amig;
    client_max_body_size 12m;

    # 后台路由伪装 404
    location /admin { return 404; }

    # 静态文件直发
    location /uploads/ {
        alias /www/wwwroot/amig/uploads/;
        expires 1y;
        add_header Cache-Control "public, immutable";
    }

    location /static/ {
        alias /www/wwwroot/amig/app/static/;
        expires 30d;
    }

    # 反代应用
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
    }
}
```

管理域名站配置 `/etc/nginx/sites-available/admin.example.com`（去除 `location /admin { return 404; }`，其余相同）：

```nginx
server {
    listen 80;
    server_name admin.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name admin.example.com;

    ssl_certificate     /etc/ssl/certs/admin.example.com.pem;
    ssl_certificate_key /etc/ssl/private/admin.example.com.key;

    client_max_body_size 12m;

    location /uploads/ {
        alias /www/wwwroot/amig/uploads/;
        expires 1y;
    }

    location /static/ {
        alias /www/wwwroot/amig/app/static/;
        expires 30d;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
    }
}
```

#### 方案二：宝塔面板

1. **主域名建站**：宝塔 → 网站 → 添加站点（`example.com`），设置反向代理到 `http://127.0.0.1:8000`
2. 在配置文件 `server` 块中添加：

```nginx
location /admin { return 404; }

location /uploads/ {
    alias /www/wwwroot/amig/uploads/;
    expires 1y;
    add_header Cache-Control "public, immutable";
}

location /static/ {
    alias /www/wwwroot/amig/app/static/;
    expires 30d;
}
```

3. 申请 SSL 证书并强制 HTTPS
4. **管理域名建站**：添加站点（`admin.example.com`），反向代理到 `http://127.0.0.1:8000`，申请 SSL
5. 配置 `.env` 中 `ADMIN_DOMAIN=admin.example.com`
6. 域名解析：两条 A 记录分别指向服务器 IP

> 双域名隔离由应用层中间件 `AdminDomainMiddleware` 保证——非白名单域名访问 `/admin` 直接返回 404。即使多个域名共用同一个 Nginx `server` 块（未单独加 `location /admin { return 404; }`），也能正确隔离；Nginx 层的 `location /admin` 只是可选的纵深防御。

### 9. 目录权限

```bash
cd /www/wwwroot/amig
mkdir -p data uploads
chown -R www-data:www-data data uploads
chmod -R 755 data uploads
```

### 安全建议

1. **HTTPS**：前后台域名均强制 HTTPS
2. **防火墙**：仅开放 80/443，应用端口 8000 仅监听 127.0.0.1
3. **端口隔离**：uvicorn 仅绑定 `127.0.0.1:8000`，不对外暴露 8000 端口
4. **静态目录禁执行脚本**：
   ```nginx
   location /uploads/ {
       location ~ \.php$ { return 403; }
   }
   ```
5. **定期备份**：
   ```bash
   tar -czf backup-$(date +%Y%m%d).tar.gz data/app.db uploads/
   ```
6. **回滚**：`git revert <commit>` 后重启服务；或恢复代码备份后重启。

---

## 使用指南

### 前台使用

- **浏览**：访问主域名，首页展示随机/最新/热门图片，进入「图库」浏览全部
- **上传**：进入「上传」页，选择或拖拽图片，填写简介（必填）与署名（可选），勾选同意站规后提交，等待审核
- **点赞**：图片详情页点击点赞按钮，同一 IP + 浏览器指纹仅可赞一次

### 后台使用

**登录**：访问管理域名（本地为 `http://127.0.0.1:8000/admin/login`），使用初始化时生成的管理员账号（用户名 `admin`）密码登录。

#### 图片审核

- 进入「图片审核」，仅展示待审核图片
- 列表内可直接「通过」或「删除」，也可勾选后「批量通过 / 批量删除」
- 点击「详情」进入大图页面，可编辑简介和署名后「通过」，或「拒绝并删除」（彻底移除，不可恢复）

#### 图片管理

- 展示全部图片，支持搜索、排序
- 点击「详情」可查看完整信息、点赞明细，并可**编辑简介**（保存后生效，操作留痕）

#### 用户/IP 管理

- 封禁单个 IP、IP 段（CIDR 格式，如 `192.168.1.0/24`）、UA 指纹
- 查看上传者的冷却状态，可手动解除

#### 站点设置

- 修改站名、简介、Logo、备案号、版权、强调色
- 调整限流参数、热度榜窗口天数与展示数量
- 开关「署名」「验证码」「去重」功能，配置敏感词（逗号分隔）
- 修改「管理域名」（多个逗号分隔，修改后立即生效）

#### 操作日志

- 记录所有管理员操作（登录、审核、编辑、设置、封禁等），支持按关键词、操作类型、IP、地理位置、日期范围模糊搜索
- 搜索关键词会在结果中高亮显示，方便快速定位（如按图片编号 `image#123` 检索）

#### 修改密码

- 进入「修改密码」，输入新密码并确认（至少 6 位），无需原密码

#### 密码重置（忘记密码时）

在服务器项目根目录（宝塔终端）执行：

```bash
python reset_password.py                # 交互式确认后重置为 abcd1234
python reset_password.py --yes          # 跳过确认直接重置
python reset_password.py --password 你的密码 --yes   # 自定义密码
```

- 默认将管理员密码重置为 `abcd1234`
- 同时清除登录失败计数与锁定状态
- 重置后请立即登录后台修改为强密码

---

## 目录结构

```
amig/
├── app/
│   ├── main.py              # 应用入口
│   ├── config.py            # 全局配置
│   ├── database.py          # 数据库连接、建表与自动迁移
│   ├── models.py            # 数据模型（纯 SQL）
│   ├── middleware.py        # 中间件（域名校验/CSRF/敏感词）
│   ├── auth.py              # 认证与 Session
│   ├── limiter.py           # 限流
│   ├── ban.py               # 封禁
│   ├── ip_geo.py            # IP 归属地
│   ├── image_utils.py       # 图片处理
│   ├── stats.py             # 统计与热度榜缓存
│   ├── routes/
│   │   ├── front.py         # 前台路由
│   │   ├── admin.py         # 后台路由
│   │   └── api.py           # 点赞 API
│   ├── templates/           # Jinja2 模板（前台 + 后台）
│   └── static/              # 静态资源（CSS/JS）
├── uploads/                 # 上传图片存储（按年/月分目录）
├── data/                    # 数据文件（app.db、ip2region.xdb）
├── tests/                   # 单元测试（unittest）
├── .env.example             # 环境变量示例
├── requirements.txt         # Python 依赖
├── init_db.py               # 数据库初始化脚本
├── reset_password.py        # 管理员密码重置脚本
├── run.py                   # 启动入口
├── start.bat                # Windows 一键启动脚本
└── README.md                # 本文档
```

---

## 常见问题解答（FAQ）

### 启动类

**Q：双击 `start.bat` 闪退或报错？**
A：确认 Python 已安装并加入 PATH；确认 `requirements.txt` 存在；若依赖安装失败，手动执行 `pip install -r requirements.txt`。

**Q：提示端口 8000 被占用？**
A：`start.bat` 会自动结束占用端口的旧进程。若仍失败，手动执行 `netstat -ano | findstr :8000` 找到 PID 后 `taskkill /F /PID <pid>`。

**Q：提示 `ip2region.xdb not found`？**
A：这是可选组件，缺失时 IP 归属地功能停用，不影响其他功能。下载后放入 `data/` 目录重启即可。

**Q：启动后访问返回 502/504？**
A：502 通常是 uvicorn 未启动，检查 `systemctl status amig.service` 与日志；504 是上游处理超时，确认已配置 `proxy_read_timeout 120s`，并确认代码已更新到含性能修复的版本。

### 访问类

**Q：主域名访问 `/admin` 返回 404？**
A：这是双域名架构的正常行为，后台请通过管理域名（如 `admin.example.com`）访问。

**Q：本地如何访问后台？**
A：本地通过 `http://127.0.0.1:8000/admin/login` 访问（回环地址始终放行）。

**Q：修改管理域名后进不去后台了？**
A：通过 SSH 编辑 `.env` 修正 `ADMIN_DOMAIN`，重启应用即可。

**Q：后台改了管理域名，旧域名为什么还能访问后台？**
A：管理域名是「并集」生效——`.env` / systemd 的 `ADMIN_DOMAIN` 与数据库 `admin_domain` 都会被放行，后台修改只覆盖数据库，不会删除 `.env` 里的旧域名。要彻底停用旧域名，需同时更新 `.env`、systemd 的 `Environment`、数据库三处后重启。

**Q：双域名隔离靠什么实现？需要 Nginx 单独配置吗？**
A：由应用层中间件 `AdminDomainMiddleware` 保证——非白名单域名访问 `/admin` 直接返回 404，无需在 Nginx 单独加 `location /admin { return 404; }`。多个域名共用同一个 Nginx `server` 块也能正确隔离；Nginx 层的 `location /admin` 只是可选的纵深防御。

### 功能类

**Q：上传提示「图片格式与文件内容不匹配」？**
A：文件扩展名与真实内容不一致。请使用真实的 JPG/PNG/WebP 文件，不要直接改后缀名。

**Q：上传提示「操作过于频繁」？**
A：触发了上传间隔限流或冷却机制，请稍后再试；多次被拒绝会递增冷却时间。后台「站点设置 → 上传间隔(秒)」可调整（默认 30 秒）。

**Q：点赞提示「您已经赞过这张图片了」？**
A：同一 IP + 浏览器指纹对同一图片仅可点赞一次，属正常防刷机制。

**Q：验证码数字不显示？**
A：确认后台已开启「上传验证码」；若数字消失，刷新页面即可（每次渲染会重新生成）。

**Q：被拒绝的图片能恢复吗？**
A：不能。「拒绝」等同于「删除」，图片文件与记录会彻底移除，操作前请谨慎确认。

**Q：IP 归属地不显示？**
A：确认 `data/ip2region.xdb` 存在（已随仓库内置），并重启服务即可。

### 密码与账号类

**Q：忘记管理员密码？**
A：执行 `python reset_password.py` 重置为默认密码 `abcd1234`（详见上文「密码重置」）。

**Q：修改密码需要原密码吗？**
A：不需要。登录状态下直接输入新密码 + 确认即可修改。

**Q：管理员登录被锁定？**
A：连续输错 5 次会锁定 15 分钟，等待后重试，或运行密码重置脚本解除锁定。

### 部署与迁移类

**Q：代码更新后不生效？**
A：Python 进程不会热加载，务必 `sudo systemctl restart amig.service`。

**Q：如何验证数据库迁移是否成功？**
A：服务启动时自动迁移，用下面命令核对关键结构（`PRAGMA table_info` 返回元组，字段名在第二列，用 `r[1]` 取值）：

```bash
cd /www/wwwroot/amig && python3 -c "import sqlite3; c=sqlite3.connect('data/app.db'); cols={r[1] for r in c.execute('PRAGMA table_info(images)').fetchall()}; print('字段:', all(x in cols for x in ['title','region','ip','like_count','views'])); print('sessions 表:', bool(c.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'\").fetchone())); c.close()"
```

预期输出均为 `True`，即迁移完成。

---

## 附录：数据表结构

| 表名 | 用途 |
|------|------|
| `images` | 图片信息（文件名、尺寸、大小、标题、简介、状态、IP、地区、点赞/浏览计数等） |
| `likes` | 点赞记录（图片 ID、IP、UA 哈希、地区） |
| `ban_list` | 封禁列表（IP/CIDR/UA 哈希） |
| `admins` | 管理员账号（bcrypt 密码哈希、登录锁定） |
| `settings` | 站点配置（键值对，含管理域名） |
| `pages` | 自定义页面 |
| `friend_links` | 友情链接 |
| `daily_stats` | 每日统计（上传/通过/拒绝/点赞/浏览） |
| `admin_logs` | 管理员操作日志（含 IP、地理位置） |
| `sessions` | 管理员会话（持久化，支持多 worker 共享登录态） |
| `view_sessions` | 浏览去重记录 |
| `cooling` | 冷却机制（拒绝次数、冷却倍数） |

> 数据库文件位于 `data/app.db`，使用 SQLite，可通过 `sqlite3 data/app.db` 或图形化工具（如 DB Browser for SQLite）查看。
