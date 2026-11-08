# AMIG · Anonymous Moderated Image Gallery（匿名图片分享站）

一个轻量级的匿名图片分享社区：前台匿名上传/浏览/点赞，后台管理员审核与管理，全站暗色主题，采用双域名架构隔离前后台。当前发布版本为 1.0.10；1.0.5 已被 1.0.7 替代，属于不可用版本。

---

## 目录

- [项目概述](#项目概述)
- [核心功能](#核心功能)
- [技术架构](#技术架构)
- [快速开始](#快速开始)
- [生产环境部署](#生产环境部署)
- [使用指南](#使用指南)
- [目录结构](#目录结构)
- [常见问题解答（FAQ）](#常见问题解答faq)
- [附录：数据表结构](#附录数据表结构)
- [第三方软件](#第三方软件)

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
| 匿名上传 | 支持 JPG/PNG/WebP，自动 WebP 重编码、剥离 EXIF、添加可配置水印、生成缩略图，并显示实时上传进度 |
| 自定义页面 | 「关于」「站规」「版权」等内容页，后台可编辑（内容按纯文本安全渲染） |
| 友情链接 | 页脚展示，后台可管理 |

### 图片上传处理流程

1. 前端校验：文件类型（JPG/PNG/WebP）与大小（≤10MB），并读取文件头魔数确认真实格式；显示上传进度，支持取消、超时提示与失败重试，提交时防止重复点击
2. 后端校验：扩展名 + 文件魔数（magic bytes）双重校验，防止伪造后缀
3. 安全处理：剥离 EXIF、严格校验总像素数（超限直接拒绝，防解压炸弹）、最长边缩放至 1920px、转 WebP、生成缩略图
4. 水印处理：使用项目内置的 Noto Sans SC 字体（SIL OFL 1.1），左下角添加后台自定义文字，右下角添加上传者和上传时间；根据横竖图和图片尺寸自动调整字号、边距、换行与位置，水印半透明合成，不遮挡图片主体
5. 内容去重：计算 MD5，同 IP 24 小时 / 全站 30 天内重复则标记「疑似重复」
6. 入库为 `pending` 状态，等待管理员审核

### 后台功能（管理员，独立管理域名）

| 功能 | 说明 |
|------|------|
| 仪表盘 | 今日上传/通过/拒绝/点赞/浏览、通过率、活跃 IP、封禁数；近 7 天趋势图（上传/点赞/浏览）；点赞地区分布；热门图片（窗口内/累计） |
| 图片审核 | 仅展示待审核图片，支持列表内快速通过/删除、批量通过/删除、进入详情编辑简介与署名后通过 |
| 图片管理 | 展示全部图片，支持搜索、排序（时间/点赞/浏览）、查看详情、**编辑简介**、删除 |
| 用户/IP 管理 | 封禁单个 IP、IP 段（CIDR）、UA 指纹；查看与解除冷却状态 |
| 站点设置 | 站名、简介、Logo、备案号、版权、强调色、限流参数、热度榜参数、验证码开关、去重开关、敏感词、管理域名、水印文字 |
| 友情链接 | 增删改、排序、显隐控制 |
| 页面管理 | 自定义页面增删改，内容按纯文本渲染并自动进行 HTML 转义（保留换行），不支持直接嵌入 HTML |
| 操作日志 | 记录所有管理员操作，支持关键词模糊搜索与高亮，可追溯 |
| 修改密码 | 无需原密码，输入新密码 + 确认即可修改 |

### 审核机制说明

- **拒绝 = 删除**：被拒绝的图片会彻底移除图片文件与数据库记录，不可恢复，无需填写拒绝原因
- 审核列表仅保留「待审核」，已通过/已拒绝不再单独展示（已通过图片见「图片管理」）

### 界面与移动端适配

- **统一设计规范**：颜色、字号、间距、圆角、边框、阴影以及按钮/表单共用一套 CSS 变量，前台与后台保持一致视觉语言
- **克制的暗色主题**：近黑背景 + 1px 边框分层 + 单一强调色，避免大面积渐变、发光、胶囊按钮和卡片堆叠；后台配置的主题强调色、Logo 与自定义页面继续生效
- **移动端按真实视口适配**：覆盖 360 / 375 / 390 / 430px 手机、横屏手机（844×390）与平板（768×1024）；导航折叠、图库双列网格、详情图不超出视口、触摸目标不小于 40px
- **后台窄屏**：侧栏改为抽屉式菜单，宽表格横向滚动而不压缩列，主要操作在手机上仍可完成

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
| IP 归属地 | ip-api.com（公网 API，带缓存与失败降级） |
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
| `app/ip_geo.py` | ip-api.com 归属地查询、客户端 IP 获取 |
| `app/image_utils.py` | 图片校验、WebP 转换、缩略图、EXIF 剥离 |
| `app/stats.py` | 统计数据、热度榜缓存 |

### 安全设计

- **CSRF 防护**：中间件自动下发 CSRF Cookie，表单 + Cookie 双重校验
- **蜜罐字段**：隐藏表单字段，机器人填写即拒绝
- **人机验证**：上传页可选 Cloudflare Turnstile（后台配置开关、Site Key 和 Secret Key）
- **限流**：上传按 IP/UA/全站三层限制 + 冷却倍数递增；点赞按分钟限流
- **封禁**：IP 精确、CIDR 段、UA 哈希三种维度
- **登录锁定**：连续失败 5 次锁定 15 分钟；IP 级限流记录按窗口期定期清理
- **管理员会话**：Session 持久化到数据库；修改密码后撤销其他设备会话并为当前设备重新签发
- **后台域名隔离**：数据库中的管理域名白名单为唯一权威配置，支持多个域名；保存后旧域名立即失效；仅信任 `Host` 头，回环 `Host` 只对本机客户端放行
- **退出登录**：使用 POST + CSRF 校验，避免被跨站请求触发
- **图片安全**：魔数校验、像素上限防解压炸弹（超限升级为异常并拒绝）、剥离 EXIF 防隐私泄露
- **文件一致性**：上传入库失败时清理已写入文件；删除图片时先删数据库记录再清理文件
- **敏感词**：简介/署名敏感词过滤（后台配置）
- **密码哈希**：bcrypt 单向加密，不可逆

---

## 快速开始

项目为三个环境提供了功能一致的一键启动脚本：

| 环境 | 脚本 | 说明 |
|------|------|------|
| Windows | `start.bat` | 双击运行，自动创建并打开浏览器 |
| Linux（通用） | `start.sh` | 创建 venv 虚拟环境后启动 |
| 宝塔面板 / 系统 Python | `start_bt.sh` | 使用系统 python3 直接启动（无需 venv） |

三个脚本均会自动完成：检查 Python 环境 → 安装依赖 → 检查/生成 `.env` → 初始化数据库 → 检查端口 → 启动服务。

### 1. 环境要求（通用）

- Python 3.10+（Windows 推荐 3.10–3.13 以使用预编译 Pillow 轮子）

### 2. 获取代码

```bash
git clone https://github.com/niutou565/AMIG.git
cd AMIG
```

或直接下载源码解压到任意目录。

### 3. Windows 一键启动

直接双击项目根目录的 **`start.bat`**，脚本会自动检查环境、安装依赖、初始化数据库并启动服务，随后自动打开浏览器。

启动后访问：`http://127.0.0.1:8000`

### 4. Linux 一键启动

```bash
cd AMIG
chmod +x start.sh
./start.sh
```

脚本会创建 `venv` 虚拟环境、安装依赖、初始化数据库并启动服务。

### 5. 宝塔面板 / 系统 Python 一键启动

```bash
cd AMIG
chmod +x start_bt.sh
./start_bt.sh
```

脚本使用系统 `python3` 直接运行（不创建 venv），自动适配 Debian/Ubuntu 的 PEP 668 限制（`--break-system-packages`），适合宝塔面板环境。

### 6. 手动启动（可选）

```bash
# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env

# 初始化数据库（输出管理员密码，请妥善保管）
python init_db.py

# 启动
python run.py
# 或
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### 7. 环境变量说明（.env）

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `ADMIN_DOMAIN` | 管理域名默认值，多个逗号分隔；**仅首次初始化或旧库迁移时写入数据库**，运行期以数据库设置为准 | 空 |
| `DATABASE_PATH` | 数据库路径 | `data/app.db` |
| `UPLOADS_DIR` | 上传目录 | `uploads` |
| `IP_API_TIMEOUT` | ip-api.com 请求超时时间（秒） | `3` |
| `IP_API_CACHE_TTL` | IP 归属地缓存时间（秒） | `300` |
| `TRUSTED_PROXIES` | 可信反向代理地址（逗号分隔），仅这些来源的 `X-Real-IP` 会被信任 | `127.0.0.1,::1,localhost` |
| `WATERMARK_FONT_PATH` | 水印字体路径，需支持简体中文；默认使用内置 Noto Sans SC | `app/assets/fonts/NotoSansSC-Regular.otf` |

> 水印字体默认随项目分发，不依赖服务器系统字体。内置字体为 Noto Sans SC，采用 SIL Open Font License 1.1，许可证见 `app/assets/fonts/OFL.txt`。如需更换字体，请将字体文件放入 `app/assets/fonts/` 或通过 `WATERMARK_FONT_PATH` 指定路径，并确认字体许可证允许分发。

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
| 网络 | 服务器可访问 `ip-api.com`（仅上传和点赞时查询，带缓存与失败降级） |

#### 资源占用参考（实测）

| 项目 | 占用 | 说明 |
|---|---|---|
| 纯源码（约 60 个文件） | ~0.25 MB | 不含 `.git` / 虚拟环境 / 数据 |
| Python 虚拟环境 + 依赖 | ~80–120 MB | Pillow、FastAPI 及传递依赖 |
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

必填项（仅首次部署时使用）：

```ini
ADMIN_DOMAIN=admin.example.com
```

> 首次启动时，若数据库尚未配置 `admin_domain`，应用会把 `ADMIN_DOMAIN` 归一化后写入数据库一次；之后**数据库中的「站点设置 → 管理域名」是唯一权威配置**，`.env` 与 systemd 中的旧值不会再覆盖它，也不会与服务配置产生冲突。

可选项（有默认值，通常无需修改）：

```ini
DATABASE_PATH=data/app.db
UPLOADS_DIR=uploads
IP_API_TIMEOUT=3
IP_API_CACHE_TTL=300
# 可信反向代理 IP、主机名或 CIDR（逗号分隔），仅这些来源的 X-Real-IP 会被信任
# 默认 127.0.0.1,::1,localhost；直连 Cloudflare 回源时需加入其官方出口网段
TRUSTED_PROXIES=127.0.0.1,::1,localhost
```

> **后台域名统一由数据库维护**：登录后台「站点设置 → 管理域名」修改后立即生效（中间件每次请求实时读取数据库），**无需修改 `.env`、systemd 的 `Environment`，也无需重启服务**。旧域名会立即失去后台访问权限，白名单中删除的域名同样立即失效。
>
> - 首次启动/旧库迁移时才会读取 `.env` 的 `ADMIN_DOMAIN` 作为数据库默认值；之后 `.env` 保留旧域名也不会覆盖数据库配置。
> - 支持多个管理域名组成白名单；域名的协议前缀、大小写、端口、末尾斜杠会被统一归一化，非法域名（含路径、通配符、空格等）会被拒绝。
> - 修改前仍需先完成 DNS 解析，并把新域名加入 Nginx 的 `server_name`；Nginx 必须使用 `proxy_set_header Host $host`，应用只信任 Host 头，不信任 `X-Forwarded-Host`。

### 5. IP 归属地查询

上传和点赞会通过 `ip-api.com` 查询公网 IP 的归属地。查询结果默认缓存 300 秒；网络不可用、私有地址或查询失败时会保留为空，不影响上传和点赞。

#### Cloudflare 代理下地区显示“未知地区”

**现象与影响**：Cloudflare DNS 记录开启“已代理”（橙色云朵）后，用户仍可正常上传和点赞，但新记录的地区可能显示为“未知地区”。这会影响上传者和点赞者地区识别；历史空地区数据不会自动回填。浏览器中第三方统计脚本、Google reCAPTCHA 或 Cloudflare Analytics 的网络错误与该后端问题无直接关系。

**原因**：Cloudflare 会将真实用户 IP 放在 `CF-Connecting-IP` 请求头中；源站默认收到的是 Cloudflare 节点 IP。Nginx 未仅对 Cloudflare 官方出口网段解析该请求头，或应用未信任 Nginx 反代来源时，`ip-api.com` 收到代理/内网 IP，无法返回地区。

**处理方法**：保留 Cloudflare 橙色云朵，不要关闭 CDN。在宝塔站点 Nginx 的 `server` 块中引入由 Cloudflare 官方 IP 段生成的真实 IP 配置，并继续传递 `$remote_addr`：

```nginx
include /www/server/nginx/conf/cloudflare-realip.conf;

location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

生成 `/www/server/nginx/conf/cloudflare-realip.conf`：

```bash
{
  curl -fsSL https://www.cloudflare.com/ips-v4 | sed 's#^#set_real_ip_from #; s#$#;#'
  curl -fsSL https://www.cloudflare.com/ips-v6 | sed 's#^#set_real_ip_from #; s#$#;#'
  printf '%s\n' 'real_ip_header CF-Connecting-IP;' 'real_ip_recursive on;'
} | tee /www/server/nginx/conf/cloudflare-realip.conf
```

在 `/www/server/python_project/vhost/env/91bus.env` 中保留或配置实际 Nginx 到应用的可信代理来源：

```ini
TRUSTED_PROXIES=127.0.0.1,::1,localhost
```

若项目经 Docker/内部网桥转发，确认实际来源后再加入对应网段，例如 `172.16.0.0/12`。不要在 Python 代码中无条件信任 `CF-Connecting-IP`，否则绕过 Cloudflare 直连源站的请求可伪造 IP。

执行并验证：

```bash
nginx -t
systemctl reload nginx
```

随后重启宝塔中的 `91bus` Python 项目。使用移动网络或另一公网网络新增一次点赞，确认新记录显示地区且应用收到的 `X-Real-IP` 是真实公网 IP。

参考：<https://developers.cloudflare.com/support/troubleshooting/restoring-visitor-ips/>、<https://www.cloudflare.com/ips/>。

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
# 仅首次初始化/旧库迁移时作为数据库默认值；之后以后台「站点设置 → 管理域名」为准
# 可保留旧值，不会覆盖数据库中已保存的新域名
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
5. 首次部署时在 `.env` 配置 `ADMIN_DOMAIN=admin.example.com`（之后统一在后台「站点设置 → 管理域名」维护，无需改文件或重启）
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
7. **安全响应头**：应用已内置 `SecurityHeadersMiddleware` 自动添加 `X-Content-Type-Options: nosniff`、`X-Frame-Options: SAMEORIGIN`、`Referrer-Policy`、`Content-Security-Policy` 及 `Strict-Transport-Security`（默认 `max-age=31536000; includeSubDomains`）。若使用 Cloudflare，请在面板中开启 HSTS 并勾选 **Include Subdomains**（覆盖管理子域名）。
8. **Cookie 安全标志**：生产环境请确保 `.env` 中 `COOKIE_SECURE=true`（默认已启用），使后台会话与 CSRF Cookie 仅在 HTTPS 下传输。
9. **依赖漏洞扫描**：定期执行 `pip-audit -r requirements.txt`（或 `safety check`），并及时升级 `python-multipart`、`starlette`、`Pillow` 等存在历史 CVE 的组件。

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

- 记录所有管理员操作（登录、审核、编辑、设置、封禁等），支持按关键词、操作类型、IP、日期范围模糊搜索；管理员操作仅记录 IP，不查询外部归属地服务
- 搜索关键词会在结果中高亮显示，方便快速定位（如按图片编号 `image#123` 检索）

#### 修改密码

- 进入「修改密码」，输入新密码并确认（至少 6 位），无需原密码

#### 密码重置（忘记密码时）

在服务器项目根目录（宝塔终端）执行：

```bash
python reset_password.py                         # 交互式输入并确认新密码
python reset_password.py --yes                   # 生成随机强密码并直接重置
python reset_password.py --password 你的密码 --yes # 指定新密码并直接重置
```

- 新密码至少 8 位
- 同时清除登录失败计数与锁定状态
- 使用 `--yes` 时会输出生成的随机密码，请妥善保存

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
├── data/                    # 数据文件（app.db）
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

**Q：启动后访问返回 502/504？**
A：502 通常是 uvicorn 未启动，检查 `systemctl status amig.service` 与日志；504 是上游处理超时，确认已配置 `proxy_read_timeout 120s`，并确认代码已更新到含性能修复的版本。

### 访问类

**Q：主域名访问 `/admin` 返回 404？**
A：这是双域名架构的正常行为，后台请通过管理域名（如 `admin.example.com`）访问。

**Q：本地如何访问后台？**
A：本地通过 `http://127.0.0.1:8000/admin/login` 访问（回环地址始终放行）。

**Q：修改管理域名后进不去后台了？**
A：管理域名统一保存在数据库中。可先在本机通过 `http://127.0.0.1:8000/admin/login`（回环地址始终放行）进入后台，把正确的域名加回白名单；无需编辑 `.env` 或 systemd 文件。若新域名尚未完成 DNS/Nginx 配置，请先完成解析并将域名加入 `server_name`。

**Q：后台改了管理域名，旧域名为什么还能访问后台？**
A：不会。数据库中的 `admin_domain` 是运行期唯一权威配置，保存后中间件立即按新白名单校验：旧域名、以及从白名单中删除的域名都会立刻返回 404。`.env` / systemd 中的 `ADMIN_DOMAIN` 只在首次初始化或旧库迁移时写入数据库一次，之后保留旧值也不会覆盖数据库配置。

**Q：需要同步修改 `.env`、systemd 或重启服务吗？**
A：不需要。后台「站点设置 → 管理域名」保存即生效。只有首次部署（数据库尚未配置 `admin_domain`）时才需要 `.env` 提供初始值。

**Q：双域名隔离靠什么实现？需要 Nginx 单独配置吗？**
A：由应用层中间件 `AdminDomainMiddleware` 保证——非白名单域名访问 `/admin` 直接返回 404，无需在 Nginx 单独加 `location /admin { return 404; }`。多个域名共用同一个 Nginx `server` 块也能正确隔离；Nginx 层的 `location /admin` 只是可选的纵深防御。

### 功能类

**Q：上传提示「图片格式与文件内容不匹配」？**
A：文件扩展名与真实内容不一致。请使用真实的 JPG/PNG/WebP 文件，不要直接改后缀名。

**Q：上传提示「操作过于频繁」？**
A：触发了上传间隔限流或冷却机制，请稍后再试；多次被拒绝会递增冷却时间。后台「站点设置 → 上传间隔(秒)」可调整（默认 30 秒）。

**Q：点赞提示「您已经赞过这张图片了」？**
A：同一 IP + 浏览器指纹对同一图片仅可点赞一次，属正常防刷机制。

**Q：人机验证未显示或校验失败？**
A：确认后台已开启「人机验证」，并正确填写 Cloudflare Turnstile 的 Site Key 与 Secret Key；同时检查站点域名已加入 Turnstile 控制台配置，且服务器可访问 `challenges.cloudflare.com`。

**Q：被拒绝的图片能恢复吗？**
A：不能。「拒绝」等同于「删除」，图片文件与记录会彻底移除，操作前请谨慎确认。

**Q：IP 归属地不显示？**
A：确认服务器可以访问 `ip-api.com`，并检查 `IP_API_TIMEOUT` 与 `IP_API_CACHE_TTL` 配置。私有地址、回环地址及查询失败不会显示归属地，也不会影响上传或点赞。

### 密码与账号类

**Q：忘记管理员密码？**
A：执行 `python reset_password.py` 后交互式设置新密码；非交互环境可使用 `python reset_password.py --yes` 生成随机强密码（详见上文「密码重置」）。

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
| `admin_logs` | 管理员操作日志（含 IP） |
| `sessions` | 管理员会话（持久化，支持多 worker 共享登录态） |
| `view_sessions` | 浏览去重记录 |
| `cooling` | 冷却机制（拒绝次数、冷却倍数） |

> 数据库文件位于 `data/app.db`，使用 SQLite，可通过 `sqlite3 data/app.db` 或图形化工具（如 DB Browser for SQLite）查看。
