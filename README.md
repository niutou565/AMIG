# 匿名图片分享站 Anonymous Moderated Image Gallery （简称AMIG）

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
| 操作日志 | 记录所有管理员操作，可追溯 |
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
| `app/database.py` | SQLite 连接、WAL、建表 |
| `app/models.py` | 数据操作（纯 SQL 参数化查询） |
| `app/middleware.py` | 管理域名校验、CSRF 中间件、敏感词过滤 |
| `app/auth.py` | 登录、Session、密码哈希、登录锁定 |
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

将项目代码放置到任意目录（以下以 `C:\bus` 为例）。

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

# 下载 ip2region.xdb 并放入 data/ 目录（可选）
# https://github.com/lionsoul2014/ip2region/releases

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

---

## 生产环境部署

> 更详细的生产部署步骤（宝塔面板 + systemd + 真实路径）见 [DEPLOY.md](DEPLOY.md)。

### 架构说明

- **主域名**（如 `example.com`）：前台访问，`/admin` 一律返回 404
- **管理域名**（如 `admin.example.com`）：后台管理入口

### 方案一：普通 nginx

#### 主域名站配置 `/etc/nginx/sites-available/example.com`

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

    root /path/to/bus;
    client_max_body_size 12m;

    # 后台路由伪装 404
    location /admin { return 404; }

    # 静态文件直发
    location /uploads/ {
        alias /path/to/bus/uploads/;
        expires 1y;
        add_header Cache-Control "public, immutable";
    }

    location /static/ {
        alias /path/to/bus/app/static/;
        expires 30d;
    }

    # 反代应用
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

#### 管理域名站配置 `/etc/nginx/sites-available/admin.example.com`

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
        alias /path/to/bus/uploads/;
        expires 1y;
    }

    location /static/ {
        alias /path/to/bus/app/static/;
        expires 30d;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

### 方案二：宝塔面板

1. **主域名建站**：宝塔 → 网站 → 添加站点（`example.com`），设置反向代理到 `http://127.0.0.1:8000`
2. 在配置文件 `server` 块中添加：

```nginx
location /admin { return 404; }

location /uploads/ {
    alias /path/to/bus/uploads/;
    expires 1y;
    add_header Cache-Control "public, immutable";
}

location /static/ {
    alias /path/to/bus/app/static/;
    expires 30d;
}
```

3. 申请 SSL 证书并强制 HTTPS
4. **管理域名建站**：添加站点（`admin.example.com`），反向代理到 `http://127.0.0.1:8000`，申请 SSL
5. 配置 `.env` 中 `ADMIN_DOMAIN=admin.example.com`
6. 域名解析：两条 A 记录分别指向服务器 IP

### systemd 服务

创建 `/etc/systemd/system/91bus.service`：

```ini
[Unit]
Description=匿名图片分享站
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/path/to/bus
Environment="ADMIN_DOMAIN=admin.example.com"
ExecStart=/usr/bin/python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable 91bus
sudo systemctl start 91bus
```

### 安全建议

1. **HTTPS**：前后台域名均强制 HTTPS
2. **防火墙**：仅开放 80/443，应用端口 8000 仅监听 127.0.0.1
3. **静态目录禁执行脚本**：
   ```nginx
   location /uploads/ {
       location ~ \.php$ { return 403; }
   }
   ```
4. **定期备份**：
   ```bash
   tar -czf backup-$(date +%Y%m%d).tar.gz data/app.db uploads/
   ```

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
bus/
├── app/
│   ├── main.py              # 应用入口
│   ├── config.py            # 全局配置
│   ├── database.py          # 数据库连接与建表
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

### 访问类

**Q：主域名访问 `/admin` 返回 404？**
A：这是双域名架构的正常行为，后台请通过管理域名（如 `admin.example.com`）访问。

**Q：本地如何访问后台？**
A：本地通过 `http://127.0.0.1:8000/admin/login` 访问（回环地址始终放行）。

**Q：修改管理域名后进不去后台了？**
A：通过 SSH 编辑 `.env` 修正 `ADMIN_DOMAIN`，重启应用即可。

### 功能类

**Q：上传提示「图片格式与文件内容不匹配」？**
A：文件扩展名与真实内容不一致。请使用真实的 JPG/PNG/WebP 文件，不要直接改后缀名。

**Q：上传提示「操作过于频繁」？**
A：触发了上传间隔限流或冷却机制，请稍后再试；多次被拒绝会递增冷却时间。

**Q：点赞提示「您已经赞过这张图片了」？**
A：同一 IP + 浏览器指纹对同一图片仅可点赞一次，属正常防刷机制。

**Q：验证码数字不显示？**
A：确认后台已开启「上传验证码」；若数字消失，刷新页面即可（每次渲染会重新生成）。

**Q：被拒绝的图片能恢复吗？**
A：不能。「拒绝」等同于「删除」，图片文件与记录会彻底移除，操作前请谨慎确认。

### 密码与账号类

**Q：忘记管理员密码？**
A：执行 `python reset_password.py` 重置为默认密码 `abcd1234`（详见上文「密码重置」）。

**Q：修改密码需要原密码吗？**
A：不需要。登录状态下直接输入新密码 + 确认即可修改。

**Q：管理员登录被锁定？**
A：连续输错 5 次会锁定 15 分钟，等待后重试，或运行密码重置脚本解除锁定。

---

## 附录：数据表结构

| 表名 | 用途 |
|------|------|
| `images` | 图片信息（文件名、尺寸、大小、简介、状态、IP、点赞/浏览计数等） |
| `likes` | 点赞记录（图片 ID、IP、UA 哈希、地区） |
| `ban_list` | 封禁列表（IP/CIDR/UA 哈希） |
| `admins` | 管理员账号（bcrypt 密码哈希、登录锁定） |
| `settings` | 站点配置（键值对） |
| `pages` | 自定义页面 |
| `friend_links` | 友情链接 |
| `daily_stats` | 每日统计（上传/通过/拒绝/点赞/浏览） |
| `admin_logs` | 管理员操作日志 |
| `view_sessions` | 浏览去重记录 |
| `cooling` | 冷却机制（拒绝次数、冷却倍数） |

> 数据库文件位于 `data/app.db`，使用 SQLite，可通过 `sqlite3 data/app.db` 或图形化工具（如 DB Browser for SQLite）查看。
