# AMIG · 匿名图片分享社区

AMIG 是一个「匿名前台 + 审核后台」的图片分享社区：访客无需注册即可上传、浏览、搜索与点赞；管理员在独立管理域名下完成审核、封禁、限流、站点配置与数据运维。全站暗色主题、响应式适配，采用双域名架构把前后台隔离。

**当前版本：1.2.6**。同一份代码通过 `DB_TYPE` 同时支持 **SQLite** 与 **MySQL 8.0+**：

- 小站 / 单机 / 宝塔简单部署：用 SQLite，零数据库依赖；
- 多 worker / 高并发：用 MySQL，连接池、行级锁与标准备份恢复；
- **1.1.x 老版本升级到 1.2.x 不需要安装脚本、不需要迁移数据**：直接启动即沿用原 SQLite 库并自动补齐结构；
- 需要换库时，登录后台「站点设置 → 切换数据库」完成（三次确认、自动备份、逐表校验）。

---

## 目录

- [1. 版本与兼容性](#1-版本与兼容性)
- [2. 功能特性](#2-功能特性)
- [3. 技术架构](#3-技术架构)
- [4. 快速开始](#4-快速开始)
- [5. 配置参考（.env）](#5-配置参考env)
- [6. 生产环境部署](#6-生产环境部署)
- [7. 数据库运维](#7-数据库运维)
- [8. 运维工具](#8-运维工具)
- [9. 测试](#9-测试)
- [10. 目录结构](#10-目录结构)
- [11. 常见问题（FAQ）](#11-常见问题faq)
- [12. 附录：数据表结构](#12-附录数据表结构)
- [13. 第三方组件与许可](#13-第三方组件与许可)

---

## 1. 版本与兼容性

### 1.1 版本定位

| 版本 | 分支 | 数据库 | 说明 |
|---|---|---|---|
| **1.2.6** | `main` | SQLite 或 MySQL 8.0+ | 当前主维护版本（1.2.5 维护审计修复版，无功能变更） |
| 1.2.5 | `main` 历史提交 | SQLite 或 MySQL 8.0+ | 双后端合并版，已被 1.2.6 取代 |
| 1.2.0 | `main` 历史提交 | MySQL 8.0+ | 已并入 1.2.5，不再单独维护 |
| 1.1.x | `1.1.x` | SQLite（WAL） | 老数据库兼容线，仅修复，不新增功能 |
| 1.0.x 及更早 | — | SQLite | 已停止维护（EOL） |

### 1.2 双后端差异

| 维度 | SQLite | MySQL |
|---|---|---|
| 部署成本 | 零依赖，单文件 `data/app.db` | 需安装 MySQL 8.0 / 8.4 LTS |
| 并发能力 | 单写者，建议 `WORKERS=1` | 连接池 + 行级锁，支持多 worker |
| 命名锁 | 进程内互斥锁 | `GET_LOCK`（跨进程/多 worker） |
| 备份 | Online Backup API / 复制库文件 | `mysqldump --single-transaction` + 行数清单 |
| 适用场景 | 个人站、小团队、宝塔单机 | 高并发、多实例、规范数据库运维 |

业务功能在两种后端下完全一致。

### 1.3 升级路径

- **1.2.5 → 1.2.6**：替换代码后直接启动，无数据库结构变化、无配置变化；1.2.6 仅修复缺陷，接口与数据表保持兼容。
- **1.1.x → 1.2.x**：替换代码后直接启动。`.env` 没有 `DB_TYPE` 时默认按 `sqlite` 处理；启动时幂等补齐缺失的表、列与索引，原有图片、管理员、配置全部保留。**不需要运行 `install.py`，也不需要迁移数据。**
- **1.2.0 → 1.2.x**：替换代码后直接启动，`.env` 保持 `DB_TYPE=mysql` 即可；表结构变化同样由启动流程自动补齐。
- **切换数据库**：登录后台「站点设置 → 切换数据库」，按页面提示完成三次确认；系统自动备份、复制并校验，成功后更新 `.env`，重启生效。旧库数据不会被删除。
- **发布标签**：1.2.x 起使用不带 `v` 前缀的注释标签（如 `1.2.6`）；1.1.x 沿用 `v1.1.x` 历史风格。

详细版本规则见 [docs/VERSIONING.md](docs/VERSIONING.md)、[docs/MAINTENANCE.md](docs/MAINTENANCE.md)、[docs/BRANCHING.md](docs/BRANCHING.md)。

---

## 2. 功能特性

### 2.1 前台（访客，无需登录）

| 功能 | 说明 |
|---|---|
| 首页 | 随机图片、最新通过、热度榜入口 |
| 图库 | 分页浏览、按相关度搜索标题/简介/署名 |
| 热度榜 | 窗口内点赞排行 |
| 上传 | 拖拽/移动端上传，实时进度；需管理员审核后公开 |
| 点赞 | 同 IP + UA 指纹对同一图片仅一次，防刷限流 |
| 自定义页面 / 友链 | 关于、规则、版权等页面与友情链接 |

### 2.2 后台（管理员，独立管理域名）

| 功能 | 说明 |
|---|---|
| 仪表盘 | 今日上传/通过/拒绝/点赞/浏览、通过率、近 7 天趋势、地区分布、热门图片 |
| 图片审核 | 待审列表、详情编辑、通过 / 拒绝（彻底删除）、批量操作 |
| 图片管理 | 全量搜索、排序、编辑简介、删除、点赞明细 |
| 用户/IP 管理 | IP、CIDR 网段、UA 哈希封禁；冷却状态查看与解除 |
| 站点设置 | 站名/简介/Logo/页脚/强调色、限流参数、验证码、去重、敏感词、管理域名 |
| 切换数据库 | 三次确认、自动备份、逐表校验、保留数据（需重启生效） |
| 操作日志 | 登录、审核、编辑、设置、封禁等全量留痕，支持多条件检索与高亮 |
| 密码管理 | 修改密码（校验原密码并轮换会话）、命令行重置 |

### 2.3 安全与防滥用

- 双域名隔离：非管理域名访问 `/admin` 一律 404；
- 匿名表单双提交 CSRF + 后台会话绑定 CSRF，所有修改请求校验 Origin/Referer；
- 上传三层限流（IP / UA / 全站）+ 冷却倍数递增；点赞按分钟限流；
- 蜜罐字段、可选 Cloudflare Turnstile 人机验证；
- 登录失败按来源 IP 限流（数据库共享，多 worker 一致），不做全局账户锁定；
- 密码策略：≥12 字符、≤72 字节（bcrypt）、拒绝弱口令/重复模式；
- 安全响应头：CSP（脚本 nonce）、HSTS（HTTPS）、X-Frame-Options、Referrer-Policy 等；
- 图片校验：扩展名 + Content-Type + 魔数 + 实际解码，像素上限防解压炸弹，剥离 EXIF，统一 WebP；
- 受控文件访问：`/uploads` 由应用鉴权，待审/已拒绝图片仅管理员可见；路径穿越与符号链接一律拒绝。

---

## 3. 技术架构

### 3.1 技术栈

| 层面 | 选型 |
|---|---|
| 语言 | Python 3.10+ |
| Web 框架 | FastAPI + Uvicorn |
| 模板 | Jinja2 |
| 数据库 | SQLite（标准库 `sqlite3`）/ MySQL 8.0+（PyMySQL） |
| 图片处理 | Pillow |
| 密码哈希 | bcrypt |
| IP 归属地 | HTTPS 公网 API（主 `ip.zxinc.org`，回退 `ipapi.co`，带缓存与退避） |
| 前端 | 原生 HTML / CSS / JS，无框架 |

### 3.2 架构

```
浏览器
  │
  ├── 主域名 example.com ─────► Nginx ──► Uvicorn 127.0.0.1:8000
  │        （前台，/admin 返回 404）        │
  │                                        ├── app/routes/front.py   前台
  ├── 管理域名 admin.example.com ─► Nginx ──┼── app/routes/admin.py   后台
  │        （后台入口）                     ├── app/routes/api.py     点赞 API
  │                                        └── app/routes/media.py   受控图片访问
  │
  └── 数据层：SQLite（data/app.db）或 MySQL（amig 库）+ uploads/ + private_uploads/
```

参数化 SQL、无 ORM；请求级数据库连接作用域，请求结束自动归还。

### 3.3 核心模块

| 模块 | 职责 |
|---|---|
| `app/main.py` | 应用入口、中间件注册、静态挂载、异常处理 |
| `app/config.py` | 全局配置（`DB_TYPE` 双后端）、版本号 |
| `app/database.py` | SQLite/MySQL 连接与事务、行对象适配、方言助手、建表与结构版本 |
| `app/db_migration.py` | SQLite ↔ MySQL 双向迁移、备份、切换锁 |
| `app/models.py` | 数据访问（参数化 SQL） |
| `app/auth.py` | 登录、Session、CSRF、登录限流 |
| `app/middleware.py` | 管理域名、CSRF/Origin、安全响应头、敏感词 |
| `app/limiter.py` | 上传/点赞限流与冷却 |
| `app/image_utils.py` | 图片校验、WebP、缩略图、水印、EXIF 剥离、文件搬移 |
| `app/upload_consistency.py` | 上传目录与数据库一致性检查 |
| `installer/` | 图形化安装向导（仅全新安装） |

---

## 4. 快速开始

### 4.1 方式一：图形化安装向导（全新安装推荐）

```bash
pip install -r requirements.txt   # 首次使用先安装依赖（一键脚本会自动安装）
python install.py                 # 本机安装，自动打开浏览器
python install.py --remote        # 宝塔等无浏览器环境：用自己电脑访问服务器 IP
python install.py --port 9000     # 自定义安装端口（默认 8765）
```

- 步骤：选择数据库（SQLite / MySQL，可实时测试连接）→ 站点配置 → 管理员账号 → 确认 → 执行；
- 前置校验：Python 版本、依赖、磁盘 ≥ 500MB、目录可写、端口占用、私有目录隔离、MySQL 直连与建库权限；
- 实时进度与日志、步骤回退修改；完成后生成 `logs/install_report_*.json` 并支持一键启动自检；
- 安装地址带随机令牌（`/install/<token>`），无令牌一律 404；
- 检测到已有安装时会提示「老版本直接启动即可升级」，不提供升级向导；
- 安装完成后按 `Ctrl+C` 结束向导，再用 `run.py` 启动站点。

### 4.2 方式二：一键启动脚本

| 环境 | 脚本 | 说明 |
|---|---|---|
| Windows | `start.bat` | 双击运行，自动安装依赖并打开浏览器 |
| Linux | `start.sh` | 创建 venv 后启动 |
| 宝塔 / 系统 Python | `start_bt.sh` | 使用系统 `python3`（适配 PEP 668） |

脚本自动完成：检查 Python → 安装依赖 → 检查/生成 `.env` → 初始化数据库 → 检查端口 → 启动服务。

### 4.3 方式三：手动启动

```bash
pip install -r requirements.txt
cp .env.example .env          # 默认 DB_TYPE=sqlite，可直接启动
python init_db.py             # 建表 + 创建管理员（打印随机密码）+ 预置内容
python run.py                 # 默认 http://127.0.0.1:8000
```

使用 MySQL 时，先把 `.env` 改为 `DB_TYPE=mysql` 并填写 `MYSQL_*`，并按 [6.3 MySQL 初始化](#63-mysql-初始化) 建库建号。

### 4.4 环境要求

- Python 3.10+（Windows 推荐 3.10–3.13，便于使用预编译 Pillow 轮子）；
- SQLite：无需额外组件；
- MySQL：8.0 / 8.4 LTS，建议使用专用账号（不要使用 root）。

---

## 5. 配置参考（.env）

### 5.1 站点与运行

| 变量 | 说明 | 默认值 |
|---|---|---|
| `ADMIN_DOMAIN` | 管理域名初始值；仅首次初始化或旧库迁移时写入数据库，之后以数据库设置为准 | 空 |
| `HOST` / `PORT` / `WORKERS` | 监听地址、端口与 worker 数；生产建议 `HOST=127.0.0.1`；SQLite 建议 `WORKERS=1` | `127.0.0.1` / `8000` / `1` |
| `COOKIE_SECURE` | Cookie 仅 HTTPS 传输；生产必须为 `true` | `true` |
| `HSTS_MAX_AGE` / `HSTS_INCLUDE_SUBDOMAINS` | HSTS 有效期与是否包含子域名（仅 HTTPS 生效） | `31536000` / `true` |
| `SESSION_TTL_HOURS` / `SESSION_IDLE_MINUTES` | 管理员会话绝对有效期 / 空闲超时 | `24` / `120` |
| `LOGIN_IP_MAX_ATTEMPTS` / `LOGIN_IP_WINDOW_MINUTES` | 同一 IP 登录失败上限与窗口 | `5` / `15` |
| `TRUSTED_PROXIES` | 可信反代地址（仅 IP/CIDR），只有这些来源的 `X-Real-IP` / `X-Forwarded-Proto` 被信任 | `127.0.0.1,::1` |

### 5.2 数据库

| 变量 | 说明 | 默认值 |
|---|---|---|
| `DB_TYPE` | `sqlite` 或 `mysql`；**缺失时默认 sqlite**（兼容 1.1.x 老配置） | `sqlite` |
| `DATABASE_PATH` | SQLite 库文件路径，相对路径按项目根目录解析 | `data/app.db` |
| `DB_BUSY_TIMEOUT_MS` / `DB_SYNCHRONOUS` | SQLite 写锁等待（毫秒）/ 持久性级别 | `5000` / `1` |
| `MYSQL_HOST` / `MYSQL_PORT` | MySQL 地址与端口 | `127.0.0.1` / `3306` |
| `MYSQL_DATABASE` | 库名（启动时自动建库建表） | `amig` |
| `MYSQL_USER` / `MYSQL_PASSWORD` | 账号与密码（不要提交到 Git） | `amig` / 空 |
| `MYSQL_POOL_SIZE` / `MYSQL_POOL_MAX_OVERFLOW` | 常驻 / 可溢出连接数 | `10` / `20` |
| `MYSQL_POOL_TIMEOUT` | 取连接超时（秒） | `10` |
| `MYSQL_PING_IDLE_SECONDS` | 空闲超过该秒数后先 ping 并自动重连 | `60` |
| `MYSQL_QUOTA_LOCK_TIMEOUT` | 上传额度命名锁等待秒数 | `5` |
| `MYSQL_CHARSET` / `MYSQL_COLLATION` | 连接字符集与排序规则 | `utf8mb4` / `utf8mb4_unicode_ci` |
| `DB_SLOW_QUERY_MS` | 慢查询阈值（毫秒，0 关闭） | `200` |

### 5.3 存储与上传

| 变量 | 说明 | 默认值 |
|---|---|---|
| `UPLOADS_DIR` | 公开上传目录（已审核图片与 Logo） | `uploads` |
| `PRIVATE_UPLOADS_DIR` | 私有目录（待审/已拒绝），必须不可被 Web 直接访问 | `private_uploads` |
| `UPLOAD_MAX_CONCURRENT` | 单进程上传并发上限，超出返回 503 | `8` |
| `BACKUP_DIR` / `BACKUP_KEEP` | 备份目录与保留份数 | `backups` / `7` |
| `MYSQLDUMP_PATH` / `MYSQL_CLIENT_PATH` | MySQL 备份恢复客户端路径（留空自动探测） | 空 |
| `WATERMARK_FONT_PATH` | 水印字体路径（默认内置 Noto Sans SC） | `app/assets/fonts/NotoSansSC-Regular.otf` |
| `IP_API_TIMEOUT` / `IP_API_CACHE_TTL` | 归属地查询超时 / 缓存秒数 | `3` / `300` |

> 完整示例见 [`.env.example`](.env.example)。所有密钥只写入本机 `.env`，不要提交到 Git。

---

## 6. 生产环境部署

### 6.1 环境要求

| 项 | 要求 |
|---|---|
| 操作系统 | Linux（Debian / Ubuntu / CentOS 均可） |
| Python | 3.10+，推荐 3.12 |
| 数据库 | SQLite（内置）或 MySQL 8.0+ |
| 反向代理 | Nginx 1.18+ |
| 进程管理 | systemd |
| 网络 | 可访问 HTTPS 归属地服务（失败不影响主流程） |

资源参考：纯源码约 0.25 MB；虚拟环境+依赖约 80–120 MB；1 worker 运行内存约 30–40 MB；4 workers 约 125 MB。最低 1 核 512 MB，推荐 2 核 2 GB。

> 请勿使用宝塔「Python 项目管理器」直接运行本项目（其系统 Python 3.7 的 pip 存在已知问题），请使用命令行 + 独立虚拟环境。

### 6.2 获取代码与安装依赖

```bash
cd /www/wwwroot
git clone https://github.com/niutou565/AMIG.git amig
cd amig

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

更新已部署环境：

```bash
cd /www/wwwroot/amig
git fetch origin
git pull origin main
systemctl restart amig    # Python 进程不会热加载
```

> Debian/Ubuntu 无法创建 venv 时先安装 `python3-venv`、`python3-full`；受 PEP 668 限制时可使用 `--break-system-packages` 或系统 Python。

### 6.3 MySQL 初始化

```sql
CREATE DATABASE IF NOT EXISTS amig
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'amig'@'127.0.0.1' IDENTIFIED BY '请替换为强密码';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, DROP, REFERENCES
  ON amig.* TO 'amig'@'127.0.0.1';
FLUSH PRIVILEGES;
```

- 应用启动时自动建表，无需手工导入 SQL；
- 备份/恢复工具需要 `mysqldump`、`mysql` 客户端，可通过 `MYSQLDUMP_PATH` / `MYSQL_CLIENT_PATH` 指定；
- 使用 SQLite 时可跳过本节。

### 6.4 配置 `.env`

```bash
cp .env.example .env
vim .env
```

SQLite（最简单）：

```ini
DB_TYPE=sqlite
DATABASE_PATH=data/app.db
HOST=127.0.0.1
PORT=8000
WORKERS=1
COOKIE_SECURE=true
ADMIN_DOMAIN=admin.example.com
```

MySQL：

```ini
DB_TYPE=mysql
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_DATABASE=amig
MYSQL_USER=amig
MYSQL_PASSWORD=替换为实际密码
HOST=127.0.0.1
PORT=8000
WORKERS=1
COOKIE_SECURE=true
ADMIN_DOMAIN=admin.example.com
```

说明：

- `ADMIN_DOMAIN` 只在首次初始化或旧库迁移时写入数据库；之后**数据库中的「站点设置 → 管理域名」是唯一权威配置**，修改无需改文件或重启。
- 留空 `ADMIN_DOMAIN` 时，后台仅允许本机回环访问，并在日志中给出提示；之后补充并重启仍可完成初始化。
- `HOST` 必须保持 `127.0.0.1`，由 Nginx 对外服务，避免绕过反向代理。

### 6.5 初始化数据库

```bash
source venv/bin/activate
python init_db.py
```

脚本按 `DB_TYPE` 建表，创建管理员（默认 `admin`，可用 `AMIG_ADMIN_USERNAME` / `AMIG_ADMIN_PASSWORD` 指定），写入默认配置并预置页面与友链。**请立即保存输出的管理员密码**；遗忘时执行 `python reset_password.py`。

- 15 张业务表：`images`、`likes`、`ban_list`、`admins`、`sessions`、`login_rate_limits`、`settings`、`upload_reservations`、`pages`、`friend_links`、`daily_stats`、`admin_logs`、`view_sessions`、`cooling`、`schema_migrations`；
- SQLite 模式会自动为老库补齐缺失表/列/索引；MySQL 模式由 `CREATE TABLE IF NOT EXISTS` 保证幂等。

### 6.6 systemd

`/etc/systemd/system/amig.service`：

```ini
[Unit]
Description=AMIG anonymous image gallery
After=network.target

[Service]
Type=simple
User=www-data
Group=www-data
WorkingDirectory=/www/wwwroot/amig
# 仅首次初始化/旧库迁移时作为数据库默认值，之后以后台设置为准
Environment="ADMIN_DOMAIN=admin.example.com"
Environment="HOST=127.0.0.1"
Environment="PORT=8000"
Environment="WORKERS=1"
ExecStart=/www/wwwroot/amig/venv/bin/python /www/wwwroot/amig/run.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now amig.service
sudo systemctl status amig.service
```

### 6.7 Nginx 反向代理

主域名站点（`example.com`）：

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

    client_max_body_size 12m;
    client_body_timeout 30s;

    # 后台路由伪装 404（应用层也会校验，这里是纵深防御）
    location /admin { return 404; }

    # 敏感路径兜底拒绝：不要把项目根目录配置为 Nginx root
    location ~ ^/(?:data|backups|private_uploads|logs|tools|tests)(?:/|$) { return 404; }
    location ~ ^/(?:\.git|\.svn|\.env)(?:/|$) { return 404; }

    # /uploads 必须由应用鉴权，严禁使用 alias/root 直接暴露
    location /static/ {
        alias /www/wwwroot/amig/app/static/;
        expires 30d;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        # 标准 443 用 $host；非 443 端口请用 $http_host 保留端口
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
    }
}
```

管理域名站点（`admin.example.com`）：与上面相同，但**去掉** `location /admin { return 404; }`，并改用该域名的证书。

宝塔面板：两个站点分别反向代理到 `http://127.0.0.1:8000`，申请 SSL，并在 `server` 块加入相同的敏感路径拒绝规则与 `/static/` alias；不要为 `/uploads` 配置静态目录。

### 6.8 目录权限

```bash
cd /www/wwwroot/amig
mkdir -p data uploads private_uploads backups
chown -R www-data:www-data data uploads private_uploads backups
chmod -R 755 data uploads private_uploads backups
chmod 600 .env
```

### 6.9 部署后验证

```bash
nginx -t && systemctl reload nginx

curl -I http://example.com/                                  # 301 → HTTPS
curl -sI https://example.com/ | grep -i strict-transport-security
curl -I https://example.com/admin                            # 404
curl -I https://admin.example.com/admin/login                # 200
curl -I https://example.com/backups/                         # 404
curl -I https://example.com/private_uploads/                 # 404
curl -I https://example.com/.env                             # 404
curl -I https://example.com/tools/backup_db.py               # 404
```

应用层复核：

```bash
python tools/check_db.py --deep      # MySQL 只读健康检查
python tools/check_uploads.py        # 上传目录一致性检查
```

### 6.10 安全建议

1. 前后台域名均强制 HTTPS；防火墙仅开放 80/443；
2. Uvicorn 只监听 `127.0.0.1`，不暴露 8000；
3. 不要将项目根目录设为 Nginx `root`；`data/`、`backups/`、`.env`、`tools/` 必须拒绝访问；
4. `/uploads` 与 `private_uploads/` 禁止配置静态直发；
5. `proxy_set_header Host $host` 必须配置，否则后台域名隔离与 CSRF 同源校验会失效；
6. `TRUSTED_PROXIES` 只填写实际代理地址，禁止 `0.0.0.0/0` / `::/0`；
7. 定期备份，并在升级/切换数据库前额外备份。

---

## 7. 数据库运维

### 7.1 老版本升级（1.1.x → 1.2.x）

1. 停止旧服务，备份 `data/app.db`、`uploads/`、`private_uploads/`、`.env`；
2. 用 1.2.5 代码覆盖旧目录（保留 `data/`、`uploads/`、`private_uploads/`、`backups/`、`.env`）；
3. 直接启动（`run.py` / start 脚本）。启动时自动：
   - 按 sqlite 后端连接原 `data/app.db`（`.env` 无 `DB_TYPE` 时默认 sqlite）；
   - 补齐新版本新增的表、列与索引；
   - 保留全部数据、管理员与站点配置；
4. 登录后台确认数据与设置正常；如需换 MySQL，使用后台「切换数据库」。

### 7.2 后台切换数据库

入口：后台「站点设置 → 切换数据库」。

- 三次确认：知晓停机维护、确认自动备份、输入确认词「切换」；
- 流程：备份当前库 → 复制到空目标库（来源快照读 + 目标单事务写）→ 逐表行数 + 首尾抽样校验 → 原子更新 `.env`；
- 目标库必须为空，避免覆盖；
- 任一步失败都不会修改 `.env`，站点继续使用原库；
- 成功后必须重启应用；切换期间所有业务写入（上传、点赞、浏览计数、审核、封禁、设置、操作日志等）统一返回 503，读操作不受影响；
- 切换成功到重启完成之间处于「等待重启」状态（`BACKUP_DIR/.db_switch.pending_restart`），旧进程不再写入旧库；重启后自动清除，若忘记重启则该标记 30 分钟后自动过期；
- 跨进程文件锁防止并发切换；备份文件位于 `BACKUP_DIR`。

> 若切换过程异常中断（进程崩溃），`.db_switch.lock` 也会在 30 分钟后自动过期；确认没有正在进行的切换后可手工删除该文件立即恢复写入。

### 7.3 备份与恢复（MySQL）

```bash
# 数据库 + 图片归档
python tools/backup_db.py --with-uploads

python tools/restore_db.py --list
python tools/restore_db.py --backup backups/backup_YYYYMMDDTHHMMSSZ.sql --check
```

恢复需先停服务：

```bash
systemctl stop amig
python tools/restore_db.py --backup backups/backup_xxx.sql                    # 预演
python tools/restore_db.py --backup backups/backup_xxx.sql --yes \
    --restore-uploads --force-uploads
systemctl start amig
```

- 备份写 `.part` 临时文件，校验通过后原子改名；失败不覆盖已有备份；
- 恢复前自动导出当前库为 `pre_restore_*.sql`，失败时自动回滚；
- 上传归档必须与备份同名配对；恢复后核对数据库引用的文件是否齐全。

### 7.4 备份（SQLite）

- 后台「切换数据库」与安装向导会自动使用 SQLite Online Backup API 备份；
- 手工备份：停止服务后复制 `data/app.db`（含 `-wal` / `-shm` 时建议先停服），或：
  ```bash
  python -c "import sqlite3;s=sqlite3.connect('data/app.db');d=sqlite3.connect('backups/app.db.bak');s.backup(d);d.close();s.close()"
  ```

### 7.5 命令行迁移（可选）

后台不可用时，可用命令行把 SQLite 数据迁入 MySQL：

```bash
systemctl stop amig
python tools/migrate_sqlite_to_mysql.py --source data/app.db
python tools/check_db.py
systemctl start amig
```

工具以只读方式打开源库、保留原始 ID、逐表事务、迁移后逐字段校验；目标库非空时拒绝执行。

---

## 8. 运维工具

| 工具 | 用途 |
|---|---|
| `tools/build_release.py` | 按白名单构建发布包，并扫描测试/临时/本地配置 |
| `tools/check_db.py` | MySQL 只读健康检查（版本、字符集、表与索引、计数漂移、孤儿点赞） |
| `tools/check_uploads.py` | 公开/私有上传目录与数据库一致性检查（默认只读） |
| `tools/migrate_private_uploads.py` | 历史待审/已拒绝图片迁移到私有目录（默认预演） |
| `tools/backup_db.py` / `tools/restore_db.py` | MySQL 备份与恢复 |
| `tools/migrate_sqlite_to_mysql.py` | SQLite → MySQL 一次性迁移 |
| `tools/mysql_cli.py` | mysqldump / mysql 客户端定位（密码经 `MYSQL_PWD` 传递） |
| `tools/generate_watermark_samples.py` | 生成水印效果样例（开发辅助） |

---

## 9. 测试

```bash
# MySQL 模式（需要可用 MySQL；测试使用随机 amig_test_* 库）
DB_TYPE=mysql python -m unittest discover -s tests -t . -v

# SQLite 模式（MySQL 专用用例自动跳过）
DB_TYPE=sqlite python -m unittest discover -s tests -t . -v
```

- `tests/test_upgrade_sqlite.py`：模拟 1.1.x 老库直接启动升级；
- `tests/test_sqlite_backend.py`：SQLite 建表、UPSERT、事务、命名锁、并发去重；
- `tests/test_db_switch.py`：迁移往返、非空目标拒绝、备份与切换锁、`.env` 原子写入、后台三次确认；
- `tests/test_db_switch_guard.py`：切换期间全量业务写入守卫（点赞/浏览/审核/封禁/设置/日志/并发/失败恢复/切换后新库写入，双后端）。

---

## 10. 目录结构

```
amig/
├── install.py               # 图形化安装向导入口（仅全新安装）
├── installer/               # 安装向导（步骤校验、前置检查、执行器、验证、页面）
├── app/
│   ├── main.py              # 应用入口
│   ├── config.py            # 全局配置（DB_TYPE 双后端）
│   ├── database.py          # SQLite / MySQL 双后端连接、事务、建表
│   ├── db_migration.py      # SQLite ↔ MySQL 双向迁移
│   ├── models.py            # 数据访问（纯 SQL）
│   ├── middleware.py        # 域名校验 / CSRF / 敏感词 / 安全头
│   ├── auth.py              # 认证与会话
│   ├── limiter.py           # 限流与冷却
│   ├── image_utils.py       # 图片处理
│   ├── stats.py             # 统计与热度缓存
│   ├── routes/              # front / admin / api / media
│   ├── templates/           # Jinja2 模板
│   └── static/              # CSS / JS / 图标
├── uploads/                 # 已审核图片与 Logo（运行期生成）
├── private_uploads/         # 待审/已拒绝图片（不可公开）
├── data/                    # SQLite 数据库目录
├── backups/                 # 备份文件
├── tools/                   # 运维与发布工具
├── tests/                   # 单元测试（不进入发布包）
├── docs/                    # 版本/维护/分支文档
├── .env.example
├── requirements.txt
├── init_db.py
├── reset_password.py
├── run.py
├── start.bat / start.sh / start_bt.sh
└── README.md
```

---

## 11. 常见问题（FAQ）

**Q：从 1.1.x 升级需要运行安装脚本或迁移数据吗？**
A：不需要。直接替换代码并启动：`DB_TYPE` 缺失时默认 sqlite，启动时自动补齐表/列/索引，原数据、管理员与配置全部保留。

**Q：第一次部署应该选哪种数据库？**
A：单机、小流量、宝塔简单部署选 SQLite（`WORKERS=1`）；需要多 worker、高并发或规范数据库运维选 MySQL。

**Q：SQLite 能开多个 worker 吗？**
A：不建议。上传额度锁为进程内锁，多 worker 会破坏串行化；需要多 worker 请切换 MySQL。

**Q：如何从 SQLite 换成 MySQL（或反向）？**
A：后台「站点设置 → 切换数据库」，三次确认后自动备份、复制并校验；重启生效，旧库数据保留。

**Q：主域名访问 `/admin` 返回 404？**
A：正常。后台请使用管理域名访问；本机可用 `http://127.0.0.1:8000/admin/login`。

**Q：改了管理域名后进不去后台？**
A：在本机访问 `http://127.0.0.1:8000/admin/login` 修改白名单；无需改 `.env` 或重启。

**Q：上传提示「操作过于频繁」/「图片格式与文件内容不匹配」？**
A：前者触发限流或冷却（后台可调上传间隔）；后者说明扩展名与真实内容不一致，请上传真实 JPG/PNG/WebP。

**Q：忘记管理员密码？**
A：`python reset_password.py`（支持 `--stdin` 自动化），会撤销全部旧会话。

**Q：启动报连接失败或连接池耗尽？**
A：SQLite 检查 `DATABASE_PATH` 与目录权限；MySQL 检查 `MYSQL_*`、服务状态与账号授权；连接池耗尽时适度调大 `MYSQL_POOL_SIZE` / `MYSQL_POOL_MAX_OVERFLOW`，但总量应小于 `max_connections`。

**Q：代码更新后不生效？**
A：重启进程：`sudo systemctl restart amig.service`。

**Q：备份时找不到 mysqldump？**
A：安装 MySQL 客户端，或设置 `MYSQLDUMP_PATH` / `MYSQL_CLIENT_PATH`；SQLite 模式无需该工具。

---

## 12. 附录：数据表结构

| 表名 | 用途 |
|---|---|
| `images` | 图片信息（文件名、尺寸、大小、标题、简介、状态、IP、地区、点赞/浏览计数） |
| `likes` | 点赞记录（图片 ID、IP、UA 哈希、地区） |
| `ban_list` | 封禁列表（IP / CIDR / UA 哈希） |
| `admins` | 管理员账号（bcrypt 哈希、失败统计；`locked_until` 保留兼容） |
| `sessions` | 管理员会话（绝对/空闲超时、CSRF 绑定） |
| `login_rate_limits` | 登录失败限流（按来源 IP） |
| `settings` | 站点配置（键值对，含管理域名） |
| `pages` | 自定义页面 |
| `friend_links` | 友情链接 |
| `daily_stats` | 每日统计 |
| `admin_logs` | 管理员操作日志 |
| `view_sessions` | 浏览去重（`(image_id, ip)` 唯一） |
| `cooling` | 冷却机制（拒绝次数、冷却倍数） |
| `upload_reservations` | 上传额度预约 |
| `schema_migrations` | 结构版本记录 |

时间字段统一为 ISO 8601 字符串（`VARCHAR(40)` / SQLite `TEXT`），与历史数据格式保持一致；MySQL 下全部表为 InnoDB + utf8mb4，`likes` 对 `images` 有外键级联删除。

---

## 13. 第三方组件与许可

- 运行依赖见 [`requirements.txt`](requirements.txt)：FastAPI、Uvicorn、Starlette、python-multipart、Pillow、bcrypt、aiofiles、Jinja2、httpx、PyMySQL、cryptography。
- 内置水印字体为 Noto Sans SC，采用 SIL Open Font License 1.1，许可证见 `app/assets/fonts/OFL.txt`。

---

**文档索引**：[CHANGELOG](CHANGELOG.md) · [版本说明](docs/VERSIONING.md) · [维护策略](docs/MAINTENANCE.md) · [分支与发布规范](docs/BRANCHING.md)
