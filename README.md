# AMIG · Anonymous Moderated Image Gallery（匿名图片分享站）

一个轻量级的匿名图片分享社区：前台匿名上传/浏览/点赞，后台管理员审核与管理，全站暗色主题，采用双域名架构隔离前后台。当前发布版本为 1.1.1；1.0.5 已被 1.0.7 替代，属于不可用版本。

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
- **轻量高效**：MySQL + 纯 SQL 参数化查询，无 ORM、无重框架，低内存占用

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
| 匿名上传 | 支持 JPG/PNG/WebP（单帧；不支持 GIF/SVG/动图），自动 WebP 重编码、保留透明通道、剥离 EXIF、添加可配置水印、生成缩略图，并显示实时上传进度 |
| 自定义页面 | 「关于」「站规」「版权」等内容页，后台可编辑（内容按纯文本安全渲染） |
| 友情链接 | 页脚展示，后台可管理 |

### 图片上传处理流程

1. 前端校验：文件类型（JPG/PNG/WebP）与大小（≤10MB），并读取文件头魔数确认真实格式；显示上传进度，支持取消、超时提示与失败重试，提交时防止重复点击
2. 后端校验：扩展名 + 声明 Content-Type + 文件魔数 + Pillow 实际解码四层校验，防止伪造后缀、损坏图片与动图
3. 安全处理：剥离 EXIF（含 GPS/方向元数据）；严格校验总像素数（超限直接拒绝，防解压炸弹）；最长边缩放至 1920px、转 WebP（透明图保留 Alpha）、生成缩略图
4. 水印处理：使用项目内置的 Noto Sans SC 字体（SIL OFL 1.1），左下角添加后台自定义文字，右下角添加上传者和上传时间；根据横竖图和图片尺寸自动调整字号、边距、换行与位置，水印半透明合成，不遮挡图片主体
5. 内容去重：计算 MD5，同 IP 24 小时 / 全站 30 天内重复则标记「疑似重复」
6. 入库为 `pending` 状态，等待管理员审核

### 图片文件访问与历史数据

- 已审核通过：原图与缩略图存放在 `uploads/YYYY/MM/`，可公开访问（`/uploads/...`），使用 ETag/304、Range（206）与 `public, no-cache` 缓存。
- 待审核 / 已拒绝：新上传文件物理存放在 `private_uploads/YYYY/MM/`，普通访客访问返回 404；仅已登录管理员在管理域名下可预览，响应 `private, no-store` 且禁止索引。审核通过时应用会把原图与缩略图移动到 `uploads/`；移动失败则回滚为待审核并记录日志。
- Logo（`logo_large_*` / `logo_small_*`）属于公开文件；临时文件、孤立文件、未知路径、路径穿越与符号链接一律 404。
- 升级兼容：旧数据库中已通过图片无需迁移即可继续访问；旧待审/已拒绝文件仍留在 `uploads/` 时，访问层仍会按状态拒绝公开访问，但建议执行下面的迁移工具把它们移入私有目录，消除 Web 服务器误配静态直发造成的泄露风险。
- 反向代理不得为 `/uploads` 或 `private_uploads` 配置 `alias`/`root` 静态直发，必须交给应用鉴权。

### 待审图片迁移到私有目录（升级后执行一次）

```bash
python tools/migrate_private_uploads.py            # 预演：只输出待移动文件
python tools/migrate_private_uploads.py --apply    # 真正移动 pending/rejected 文件
```

迁移是幂等的：已在私有目录的文件会跳过；公开与私有目录同时存在同名文件时拒绝覆盖并报告冲突。迁移只移动文件，不修改数据库记录。

### 上传目录一致性检查

```bash
python tools/check_uploads.py                      # 只读检查（同时扫描 uploads/ 与 private_uploads/）
python tools/check_uploads.py --older-than-days 7  # 标记 7 天前的孤儿文件
python tools/check_uploads.py --fix-misplaced      # 修复状态与目录不一致的文件（只移动，不删除）
python tools/check_uploads.py --delete-orphans     # 删除符合条件的孤儿文件
python tools/check_uploads.py --delete-temp        # 删除超过安全期限的 .tmp
```

工具只删除“没有任何数据库记录引用且超过安全期限”的文件；数据库有记录但文件缺失只报告，不会自动删除记录；临时文件需要显式 `--delete-temp`；`--fix-misplaced` 只把被数据库引用但放错目录的图片（原图+缩略图整体）移动到状态期望的目录，移动失败自动回滚。建议在低峰期通过 cron 定期执行只读检查，清理操作始终由人工确认后显式执行。

### 数据备份与恢复

数据库为 MySQL 8.0+，备份使用 `mysqldump --single-transaction` 取得 InnoDB 一致性快照（不阻塞业务读写），**不是直接复制数据目录**；数据库与 `uploads/`、`private_uploads/` 可打包为同名配对文件。

```bash
# 创建备份（数据库 + 图片，推荐）
python tools/backup_db.py --with-uploads

# 查看备份（标记 [含上传归档] 的备份可连同图片一起恢复）
python tools/restore_db.py --list

# 校验备份：Dump completed 尾标 + 15 张业务表结构 + 行数清单 + 归档是否包含数据库引用的全部图片
python tools/restore_db.py --backup backups/backup_20260101T000000Z.sql --check
```

- 备份默认保存在 `backups/`（可用 `BACKUP_DIR` 修改），保留最近 `BACKUP_KEEP` 份（默认 7）。该目录**不得**位于 `uploads/`、`private_uploads/` 或任何 Nginx 静态目录内。
- 生成文件为 `backup_<UTC 时间>.sql`、同名 `.manifest.json`（各表行数清单）与 `backup_<UTC 时间>_uploads.tar.gz`。备份先写 `.part`，完整性校验通过后才改名；失败不会覆盖已有的有效备份，也不会留下半份文件。
- 只备份数据库时去掉 `--with-uploads`；此时 `--check` 会提示没有上传归档（仅能恢复数据库）。
- 数据库快照与上传归档**非同一时刻生成**（先快照后打包）：快照之后新上传的文件会进入归档但不在数据库记录中，恢复后可能产生少量孤儿文件（无害，`python tools/check_uploads.py` 可识别）。
- 备份/恢复依赖 MySQL 客户端工具（`mysqldump`、`mysql`）。未加入 PATH 时通过 `MYSQLDUMP_PATH` / `MYSQL_CLIENT_PATH` 指定完整路径；密码通过 `MYSQL_PWD` 环境变量传递，不出现在命令行参数中。

恢复必须停服务后执行（恢复会整表替换数据库，运行中的请求可能写入旧数据）：

```bash
systemctl stop amig

# 预演：只打印将执行的操作
python tools/restore_db.py --backup backups/backup_xxx.sql

# 正式恢复：数据库 + 图片
python tools/restore_db.py --backup backups/backup_xxx.sql --yes \
    --restore-uploads --force-uploads

# 复核后再启动
python tools/check_db.py --deep
python tools/check_uploads.py
systemctl start amig
```

恢复注意事项：

- 恢复前会自动用 `mysqldump` 把当前库导出为 `backups/pre_restore_<时间>.sql`；导入失败或恢复后校验（行数清单、孤儿点赞）失败会自动用该副本回滚。
- 恢复目标就是当前 `.env` 中 `MYSQL_DATABASE` 指向的库，预演时会明确打印；导入使用备份内的 `DROP TABLE` + `CREATE TABLE`，即整表替换。
- 上传目录先解包到同盘临时目录并确认可读，再整体改名切换；旧目录保留为 `uploads.pre_restore_<时间>`、`private_uploads.pre_restore_<时间>`，确认数据无误后可人工清理。
- 上传归档必须与备份文件同名配对；找不到配对归档时恢复会中止，不会误用其他时间点的图片。恢复后还会核对“数据库引用的图片文件是否都在归档中”，缺失会明确报告。
- `--force-uploads` 表示允许覆盖现有上传目录（旧目录仍会保留）；不写该参数时，目标目录已存在会拒绝恢复。
- Linux 恢复后请修正属主：`chown -R www-data:www-data uploads private_uploads`。

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
| 修改密码 | 必须验证当前密码；新密码至少 12 位并通过弱口令/重复模式检查；成功后撤销其他设备会话 |

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
| 数据库 | MySQL 8.0+（InnoDB + utf8mb4，驱动 PyMySQL） |
| 图片处理 | Pillow |
| IP 归属地 | HTTPS 公网 API（主 `ip.zxinc.org`，回退 `ipapi.co`；带缓存、失败退避与降级） |
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
  └── 数据层：MySQL 8.0+ (amig 库) + 图片文件 (uploads/，经 app/routes/media.py 鉴权访问)
```

**双域名架构**：通过中间件校验 `Host` 头，非管理域名的 `/admin` 请求一律伪装为 404，实现前后台物理隔离。

### 模块划分

| 模块 | 职责 |
|------|------|
| `app/main.py` | 应用入口、中间件注册、静态文件挂载、异常处理 |
| `app/config.py` | 全局配置常量与默认值 |
| `app/database.py` | MySQL 连接池、事务/SAVEPOINT、行对象适配、建表与结构版本初始化 |
| `app/models.py` | 数据操作（纯 SQL 参数化查询） |
| `app/middleware.py` | 管理域名校验、CSRF/Origin 校验、安全响应头、敏感词过滤 |
| `app/password_policy.py` | 统一的服务端密码策略（长度、弱口令、bcrypt 字节上限） |
| `app/auth.py` | 登录、Session（数据库持久化、空闲/绝对超时、CSRF 绑定）、密码哈希、数据库共享登录限流 |
| `app/limiter.py` | 上传/点赞限流、冷却机制 |
| `app/ban.py` | 封禁检查 |
| `app/ip_geo.py` | HTTPS 归属地查询、失败退避、客户端 IP 获取 |
| `app/image_utils.py` | 图片校验、WebP 转换、缩略图、EXIF 剥离、安全路径解析、文件删除与审核发布移动 |
| `app/routes/media.py` | 受控 `/uploads` 访问：审核状态 + 管理员身份鉴权、缓存、ETag 与 Range |
| `app/upload_consistency.py` | 公开/私有上传目录与数据库一致性检查、受控孤儿文件清理 |
| `tools/check_uploads.py` | 一致性检查命令行工具（默认只读） |
| `tools/migrate_private_uploads.py` | 历史待审/已拒绝文件一次性迁移到私有目录（默认预演） |
| `tools/backup_db.py` | 数据库与图片备份（mysqldump --single-transaction，原子写入、自动校验、行数清单） |
| `tools/restore_db.py` | 备份校验与离线恢复（恢复前安全副本、失败自动回滚、上传目录整体切换） |
| `tools/check_db.py` | 数据库只读健康检查（版本/字符集/表与索引/计数漂移/孤儿点赞/备份状态） |
| `tools/migrate_sqlite_to_mysql.py` | 一次性迁移工具：把旧 SQLite 数据完整迁入 MySQL（只读源库、保留 ID、内容级校验） |
| `tools/mysql_cli.py` | mysqldump / mysql 客户端定位与调用（密码经 MYSQL_PWD 传递） |
| `tools/generate_watermark_samples.py` | 开发辅助：生成水印效果样例图到 docs/watermark_samples/（无破坏性，仅本地使用） |
| `app/stats.py` | 统计数据、热度榜缓存 |

### 安全设计

- **CSRF 防护**：匿名表单使用 Cookie/表单双提交令牌；后台表单使用与会话绑定的服务端令牌；所有修改请求额外校验 Origin/Referer
- **蜜罐字段**：隐藏表单字段，机器人填写即拒绝
- **人机验证**：上传页可选 Cloudflare Turnstile（后台配置开关、Site Key 和 Secret Key）
- **限流**：上传按 IP/UA/全站三层限制 + 冷却倍数递增；点赞按分钟限流
- **封禁**：IP 精确、CIDR 段、UA 哈希三种维度
- **登录限流**：失败次数按来源 IP 记录在数据库（多 worker 共享），默认 5 次/15 分钟；成功登录清零；不再全局锁定管理员账户，避免多 IP 攻击者造成拒绝服务
- **管理员会话**：Session 持久化到数据库，绝对有效期 24 小时 + 空闲超时 120 分钟；登录/改密轮换 Session，退出、改密、密码重置均真正撤销服务端会话
- **密码策略**：至少 12 个字符、不超过 bcrypt 72 字节、拒绝全空格/单一重复/常见弱口令；旧密码哈希继续兼容
- **安全响应头**：CSP（脚本使用一次性 nonce，不再放开 `script-src 'unsafe-inline'`）、HSTS（仅 HTTPS）、X-Frame-Options、Referrer-Policy、Permissions-Policy 等
- **后台域名隔离**：数据库中的管理域名白名单为唯一权威配置，支持多个域名；保存后旧域名立即失效；仅信任 `Host` 头，回环 `Host` 只对本机客户端放行
- **退出登录**：使用 POST + CSRF 校验，避免被跨站请求触发
- **图片安全**：扩展名 + Content-Type + 魔数 + 实际解码校验；像素上限防解压炸弹；拒绝动图；剥离 EXIF/GPS；透明通道保留
- **受控文件访问**：`/uploads` 不再静态直发。已审核图片公开；待审核/已拒绝图片存放在私有目录，仅管理员可预览；普通访客统一 404；Logo 等公开文件放行；路径穿越、符号链接、未知文件、临时文件一律拒绝
- **文件一致性**：上传入库失败清理已落盘文件；审核发布移动文件失败回滚为待审核；删除先删数据库再清理文件并记录失败；提供只读的孤立文件检查与一次性迁移工具
- **图片缓存**：公开图片 `Cache-Control: public, no-cache` + ETag/304 + Range；私有预览 `private, no-store`，避免私有内容被公共缓存
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
- **MySQL 8.0+（8.0 / 8.4 LTS 均可）**，需要先创建数据库与专用账号（见下方“MySQL 初始化”）

### 1.1 MySQL 初始化

以 root 登录 MySQL 后执行（密码请自行替换；**不要**让应用使用 root）：

```sql
CREATE DATABASE IF NOT EXISTS amig
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'amig'@'127.0.0.1' IDENTIFIED BY '请替换为强密码';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, DROP, REFERENCES
  ON amig.* TO 'amig'@'127.0.0.1';
FLUSH PRIVILEGES;
```

- 应用启动时会自动建表（`CREATE TABLE IF NOT EXISTS`），无需手工导入 SQL。
- 备份/恢复工具需要 `mysqldump`、`mysql` 客户端。Windows 默认在 `C:\Program Files\MySQL\MySQL Server 8.x\bin`；未加入 PATH 时可设置 `MYSQLDUMP_PATH` / `MYSQL_CLIENT_PATH`。
- 生产环境建议给该账号只授予 `amig.*` 上的必要权限，不要授予全局权限。
- 已有旧 SQLite 数据的站点，先按《SQLite → MySQL 迁移》一节执行一次迁移。

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
| `ADMIN_DOMAIN` | 管理域名默认值，多个逗号分隔；**仅首次初始化或旧库迁移时写入数据库**，运行期以数据库设置为准；留空时后台仅允许本机回环访问 | 空 |
| `HOST` / `PORT` / `WORKERS` | `run.py` 从 `.env`/环境变量读取全部三项；一键启动脚本只读取 shell 环境变量 `PORT`/`WORKERS`（不解析 `.env`）。生产环境只监听回环 | `127.0.0.1` / `8000` / `1` |
| `MYSQL_HOST` / `MYSQL_PORT` | MySQL 地址与端口 | `127.0.0.1` / `3306` |
| `MYSQL_DATABASE` | 数据库名（启动时自动建库建表，字符集 utf8mb4） | `amig` |
| `MYSQL_USER` / `MYSQL_PASSWORD` | 数据库账号与密码；**密码不要提交到 Git**，由部署环境注入 | `amig` / 空 |
| `DB_TYPE` | 数据库类型，仅支持 `mysql` | `mysql` |
| `MYSQL_POOL_SIZE` | 连接池常驻连接数 | `10` |
| `MYSQL_POOL_MAX_OVERFLOW` | 连接池可溢出的最大连接数（总量不建议超过 MySQL `max_connections`） | `20` |
| `MYSQL_POOL_TIMEOUT` | 取连接超时（秒），超时返回明确错误而不是无限排队 | `10` |
| `MYSQL_PING_IDLE_SECONDS` | 连接空闲超过该秒数后，下次使用前先 ping 并自动重连 | `60` |
| `MYSQL_QUOTA_LOCK_TIMEOUT` | 上传额度串行化（`GET_LOCK`）等待秒数 | `5` |
| `MYSQL_CHARSET` / `MYSQL_COLLATION` | 连接字符集与排序规则 | `utf8mb4` / `utf8mb4_unicode_ci` |
| `MYSQLDUMP_PATH` / `MYSQL_CLIENT_PATH` | 备份恢复所需的客户端完整路径（留空自动探测） | 空 |
| `DATABASE_PATH` | 旧 SQLite 库路径，**仅供迁移工具作为默认数据源**，运行时不读取 | `data/app.db` |
| `UPLOADS_DIR` | 公开上传目录（已审核图片与 Logo） | `uploads` |
| `PRIVATE_UPLOADS_DIR` | 私有上传目录（待审核/已拒绝图片） | `private_uploads` |
| `UPLOAD_MAX_CONCURRENT` | 上传接口（含 multipart 解析阶段）每进程最大并发请求数，超出返回 503 | `8` |
| `IP_API_TIMEOUT` | IP 归属地 HTTPS 查询超时时间（秒） | `3` |
| `IP_API_CACHE_TTL` | IP 归属地缓存时间（秒） | `300` |
| `TRUSTED_PROXIES` | 可信反向代理地址（逗号分隔，**仅支持 IP/CIDR，不支持主机名**），仅这些来源的 `X-Real-IP`、`X-Forwarded-Proto` 会被信任；`0.0.0.0/0`、`::/0` 会被拒绝 | `127.0.0.1,::1` |
| `COOKIE_SECURE` | Cookie 仅通过 HTTPS 传输；生产必须为 `true` | `true` |
| `HSTS_MAX_AGE` | HSTS 有效期（秒，仅 HTTPS 响应生效） | `31536000` |
| `HSTS_INCLUDE_SUBDOMAINS` | HSTS 是否包含子域名 | `true` |
| `SESSION_TTL_HOURS` | 管理员 Session 绝对有效期（小时） | `24` |
| `SESSION_IDLE_MINUTES` | 管理员 Session 空闲超时（分钟） | `120` |
| `LOGIN_IP_MAX_ATTEMPTS` | 同一 IP 登录失败上限（窗口内） | `5` |
| `LOGIN_IP_WINDOW_MINUTES` | 登录失败限流窗口（分钟） | `15` |
| `DB_SLOW_QUERY_MS` | 慢查询日志阈值（毫秒，`0` 关闭） | `200` |
| `BACKUP_DIR` | 备份目录 | `backups` |
| `BACKUP_KEEP` | 备份保留份数 | `7` |
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
| 数据库 | MySQL 8.0+（需独立安装，建议 8.0 或 8.4 LTS） |
| 反向代理 | Nginx（1.18+，含 `proxy_pass` 模块） |
| 进程管理 | systemd |
| 网络 | 服务器可访问 HTTPS 归属地服务（仅上传和点赞时查询，带缓存与失败退避） |

#### 资源占用参考（实测）

| 项目 | 占用 | 说明 |
|---|---|---|
| 纯源码（约 60 个文件） | ~0.25 MB | 不含 `.git` / 虚拟环境 / 数据 |
| Python 虚拟环境 + 依赖 | ~80–120 MB | Pillow、FastAPI 及传递依赖 |
| MySQL 数据库 | 数百 MB 起（含 InnoDB 日志与缓冲） | 随图片记录数量增长 |
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

`requirements.txt` 核心依赖：`fastapi`、`uvicorn`、`starlette`、`python-multipart`、`Pillow`、`bcrypt`、`aiofiles`、`Jinja2`、`httpx`、`PyMySQL`、`cryptography`（MySQL 8 默认的 `caching_sha2_password` 认证需要后者）。

> 运行测试（需要可用的 MySQL；测试会自动创建 `amig_test_*` 随机库，不会影响开发库）：
> ```bash
> python -m pytest tests -q
> ```

> 若服务器为 Debian/Ubuntu 且无法创建虚拟环境，需先安装 `python3-venv`、`python3-full`；pip 安装若受 PEP 668 限制，可加 `--break-system-packages` 或直接使用系统 `python3`。

### 4. 配置环境变量（.env）

```bash
cd /www/wwwroot/amig
cp .env.example .env
vim .env
```

首次部署时配置管理域名（只影响首次初始化）：

```ini
# 可留空：留空时后台仅允许本机 127.0.0.1/localhost 访问，之后可在后台补配
ADMIN_DOMAIN=admin.example.com

# 生产 HTTPS 必须保持 true
COOKIE_SECURE=true

# 应用只监听回环，由 Nginx 对外提供服务
HOST=127.0.0.1
PORT=8000
WORKERS=1

# 数据库（必须填写实际账号密码，请勿使用 root）
DB_TYPE=mysql
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_DATABASE=amig
MYSQL_USER=amig
MYSQL_PASSWORD=替换为实际密码
```

> 首次启动时，若数据库尚未配置 `admin_domain`，应用会把 `ADMIN_DOMAIN` 归一化后写入数据库一次；之后**数据库中的「站点设置 → 管理域名」是唯一权威配置**，`.env` 与 systemd 中的旧值不会再覆盖它。
>
> 如果首次启动时 `ADMIN_DOMAIN` 为空，应用**不会**写入"已初始化"标记，也不会静默放行任意域名：`/admin` 只允许本机回环访问，并在日志中输出明确警告；之后补充 `.env` 并重启仍可完成初始化。
>
> `WORKERS` 修改后需要重启服务。多 worker 下 Session、登录限流、上传额度与图片审核均通过 MySQL 共享，不会出现登录态不一致。
>
> 注意：上传接口设有两层并发保护：multipart 解析与图片处理阶段的并发闸门默认**单进程 8 个**（可用 `UPLOAD_MAX_CONCURRENT` 调整），图片解码/转码为单进程内 4 个。多 worker 部署时全局上限约为两者乘以 worker 数。Worker 数较多时请评估服务器的 CPU 峰值；每日上传配额与 IP 间隔限制通过数据库原子预约校验，多 worker 一致。上传请求体大小还受应用层限制（单文件 10MB，请求体含表单字段共 11MB），Nginx 层的 `client_max_body_size 12m` 与 `client_body_timeout` 共同拦截超大/慢速请求。

可选项（有默认值，通常无需修改）：

```ini
UPLOADS_DIR=uploads
PRIVATE_UPLOADS_DIR=private_uploads
BACKUP_DIR=backups
MYSQL_POOL_SIZE=10
MYSQL_POOL_MAX_OVERFLOW=20
IP_API_TIMEOUT=3
IP_API_CACHE_TTL=300
# 可信反向代理：仅支持 IP/CIDR（不支持主机名），默认仅本机回环
# 经 Docker/内部网桥转发时填写实际代理网段；禁止 0.0.0.0/0 或 ::/0
TRUSTED_PROXIES=127.0.0.1,::1
```

> **后台域名统一由数据库维护**：登录后台「站点设置 → 管理域名」修改后立即生效（中间件每次请求实时读取数据库），**无需修改 `.env`、systemd 的 `Environment`，也无需重启服务**。旧域名会立即失去后台访问权限，白名单中删除的域名同样立即失效。
>
> - 首次启动/旧库迁移时才会读取 `.env` 的 `ADMIN_DOMAIN` 作为数据库默认值；之后 `.env` 保留旧域名也不会覆盖数据库配置。
> - 支持多个管理域名组成白名单；域名的协议前缀、大小写、端口、末尾斜杠会被统一归一化，非法域名（含路径、通配符、空格等）会被拒绝。
> - 修改前仍需先完成 DNS 解析，并把新域名加入 Nginx 的 `server_name`；Nginx 必须使用 `proxy_set_header Host $host`，应用只信任 Host 头，不信任 `X-Forwarded-Host`。

### 5. IP 归属地查询

上传和点赞会通过 HTTPS 归属地服务（主 `ip.zxinc.org`，失败回退 `ipapi.co`）查询公网 IP 的归属地。查询结果默认缓存 300 秒；网络不可用、私有地址或查询失败时会保留为空，不影响上传和点赞。

#### Cloudflare 代理下地区显示“未知地区”

**现象与影响**：Cloudflare DNS 记录开启“已代理”（橙色云朵）后，用户仍可正常上传和点赞，但新记录的地区可能显示为“未知地区”。这会影响上传者和点赞者地区识别；历史空地区数据不会自动回填。浏览器中第三方统计脚本、Google reCAPTCHA 或 Cloudflare Analytics 的网络错误与该后端问题无直接关系。

**原因**：Cloudflare 会将真实用户 IP 放在 `CF-Connecting-IP` 请求头中；源站默认收到的是 Cloudflare 节点 IP。Nginx 未仅对 Cloudflare 官方出口网段解析该请求头，或应用未信任 Nginx 反代来源时，归属地服务收到代理/内网 IP，无法返回地区。

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
TRUSTED_PROXIES=127.0.0.1,::1
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

脚本会：建库建表（InnoDB + utf8mb4）→ 创建管理员 `admin` 并打印随机密码 → 写入默认配置 → 预置页面与友情链接。重复执行是幂等的，不会覆盖已有数据。

> **请立即保存输出的管理员密码**。若遗忘，用 `python reset_password.py` 重置（会撤销该管理员全部旧会话）。

> **数据库结构初始化**：服务启动时 `init_db()` 会执行 `CREATE TABLE IF NOT EXISTS` 建齐 15 张业务表与全部索引，并在 `schema_migrations` 记录结构版本（4/5/6）。MySQL 侧不再需要逐列 `ALTER TABLE` 补列。
>
> 建表清单：`images`、`likes`、`ban_list`、`admins`、`sessions`、`login_rate_limits`、`settings`、`upload_reservations`、`pages`、`friend_links`、`daily_stats`、`admin_logs`、`view_sessions`、`cooling`、`schema_migrations`。`likes.image_id` 有真实外键指向 `images.id`（`ON DELETE CASCADE`）。
>
> **第三阶段图片访问升级**：已通过图片继续留在 `uploads/`；新待审/已拒绝图片写入 `private_uploads/`。升级后建议执行 `python tools/migrate_private_uploads.py --apply` 把历史待审文件移入私有目录（先不带 `--apply` 预演）。必须删除 Nginx 中旧的 `location /uploads { alias ... }` 静态配置，否则待审核图片仍可被直接访问。可用 `python tools/check_uploads.py` 检查两个目录的缺失/孤儿情况。

#### SQLite → MySQL 数据迁移（从旧版本升级的站点）

旧版本使用 `data/app.db`（SQLite）。升级到 MySQL 版本时执行一次迁移工具：

```bash
# 0) 先确认 MySQL 已按「1.1 MySQL 初始化」建好库与账号，并在 .env 填好 MYSQL_*
# 1) 停止旧服务，避免迁移过程中继续写入旧库
systemctl stop amig

# 2) 迁移（默认读取 DATABASE_PATH=data/app.db，也可用 --source 指定）
python tools/migrate_sqlite_to_mysql.py --source data/app.db

# 3) 核对迁移结果后启动新版本
python tools/check_db.py
systemctl start amig
```

迁移工具的行为：

- 以只读模式（`mode=ro`）打开 SQLite，迁移前后校验文件 SHA-256 不变，**绝不修改或删除源库**；
- 先做 `PRAGMA integrity_check` 与迁移前各表行数统计，并提示先备份 `uploads/`、`private_uploads/`；
- 目标库已有业务数据时拒绝执行，防止重复导入或覆盖；旧版本缺少的表会自动跳过并在报告中提示；
- 迁移前检查字符串长度是否超出目标 `VARCHAR` 上限（旧库 TEXT 可能超长），提前报错而不是中途失败；
- 每张表一个事务，失败时输出表名、行号与主键值，且该表整体回滚；保留原始自增 ID 并重置 `AUTO_INCREMENT`；
- 迁移后逐表对比行数，并对全部行做逐字段内容级校验（中文、emoji、NULL、时间字符串均参与比对）；
- 源 SQLite 文件可保留作为存档；确认新站运行正常前不要删除。
>
> **第三阶段图片访问升级**：已通过图片继续留在 `uploads/`；新待审/已拒绝图片写入 `private_uploads/`。升级后建议执行 `python tools/migrate_private_uploads.py --apply` 把历史待审文件移入私有目录（先不带 `--apply` 预演）。必须删除 Nginx 中旧的 `location /uploads { alias ... }` 静态配置，否则待审核图片仍可被直接访问。可用 `python tools/check_uploads.py` 检查两个目录的缺失/孤儿情况。

### 7. 配置 systemd 服务

创建 `/etc/systemd/system/amig.service`：

```ini
[Unit]
Description=AMIG anonymous image gallery
After=network.target

[Service]
Type=simple
User=www-data
Group=www-data
WorkingDirectory=/www/wwwroot/amig
# 仅首次初始化/旧库迁移时作为数据库默认值；之后以后台「站点设置 → 管理域名」为准
Environment="ADMIN_DOMAIN=admin.example.com"
# 只监听回环，由 Nginx 对外提供服务；WORKERS 按需调整
Environment="HOST=127.0.0.1"
Environment="PORT=8000"
Environment="WORKERS=1"
ExecStart=/www/wwwroot/amig/venv/bin/python /www/wwwroot/amig/run.py
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

> `WORKERS` 默认 `1`（配合线程池已满足性能需求）。后台 Session、登录限流与审核状态均持久化在数据库，多 worker 共享正常；图片处理为 CPU 密集操作，提高 worker 数前请评估内存与 CPU。修改 `WORKERS` 后执行 `sudo systemctl restart amig.service`。
>
> `HOST` 必须保持 `127.0.0.1`，不要改成 `0.0.0.0`，否则会绕过 Nginx 直接把应用端口暴露到公网。

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

    client_max_body_size 12m;
    # 请求体读取超时：防止恶意客户端长时间挂起连接占用资源
    client_body_timeout 30s;

    # 后台路由伪装 404
    location /admin { return 404; }

    # 敏感路径兜底拒绝：即使以后误加 root/try_files 也不会直接泄露
    # （项目根目录永远不要配置成 Nginx 的 root）
    location ~ ^/(?:data|backups|private_uploads|logs|tools|tests)(?:/|$) { return 404; }
    location ~ ^/(?:\.git|\.svn|\.env)(?:/|$) { return 404; }

    # /uploads 必须由应用鉴权（待审核图片仅管理员可见），
    # 严禁使用 alias/root 直接把 uploads 目录暴露给 Nginx
    # （不要为 /uploads 单独配置静态 location，交给下面的 location / 反代）

    location /static/ {
        alias /www/wwwroot/amig/app/static/;
        expires 30d;
    }

    # 反代应用
    location / {
        proxy_pass http://127.0.0.1:8000;
        # 使用 $host（不含端口）；非 443 端口部署请改用 $http_host
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
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
    # 请求体读取超时：防止恶意客户端长时间挂起连接占用资源
    client_body_timeout 30s;

    # 与主域名相同：拒绝敏感路径，且不配置 /uploads、private_uploads 的静态直发
    location ~ ^/(?:data|backups|private_uploads|logs|tools|tests)(?:/|$) { return 404; }
    location ~ ^/(?:\.git|\.svn|\.env)(?:/|$) { return 404; }

    location /static/ {
        alias /www/wwwroot/amig/app/static/;
        expires 30d;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        # 使用 $host（不含端口）；非 443 端口部署请改用 $http_host
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
    }
}
```

> **不要将 `/www/wwwroot/amig` 配置为 Nginx 的 `root` 或 `alias`**。应用只需要 `proxy_pass` 和 `/static/` 的 `alias`；`data/`、`backups/`、`private_uploads/`、`.env`、`tools/` 等必须由应用或 Nginx 的拒绝规则保护。
>
> 若使用非 443 端口（如 `:8443`），`Host` 必须改为 `$http_host` 以保留端口，否则后台 CSRF 的同源校验会比较 `Host` 与 `Origin` 的端口失败。标准 443 端口使用 `$host` 即可。

#### 方案二：宝塔面板

1. **主域名建站**：宝塔 → 网站 → 添加站点（`example.com`），设置反向代理到 `http://127.0.0.1:8000`
2. 在配置文件 `server` 块中添加：

```nginx
location /admin { return 404; }

# 敏感路径兜底拒绝（站点根目录不要指向项目目录）
location ~ ^/(?:data|backups|private_uploads|logs|tools|tests)(?:/|$) { return 404; }
location ~ ^/(?:\.git|\.svn|\.env)(?:/|$) { return 404; }

# /uploads 不要配置 alias/静态直发；由应用按审核状态鉴权后返回文件
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
mkdir -p data uploads private_uploads backups
chown -R www-data:www-data data uploads private_uploads backups
chmod -R 755 data uploads private_uploads backups
# .env 可能包含密钥，仅运行用户可读
chmod 600 .env
```

> 备份工具需要写入 `backups/`。若以 root 执行备份，完成后请把新文件属主改回 `www-data`；恢复后同样需要修正 `uploads/`、`private_uploads/` 的属主。

### 10. 更换域名

前台域名更换：

1. 完成新域名的 DNS 解析、Nginx `server_name` 与 HTTPS 证书配置。
2. 应用代码无需修改：模板与接口均使用相对路径，Cookie 不绑定固定域名（Host-only），图片地址也不含绝对域名。
3. 用户在新域名重新登录即可；如需平滑过渡，可在旧域名 Nginx 配置 `return 301 https://新域名$request_uri;`。
4. 前台域名与后台管理域名相互独立，更换前台域名不影响后台白名单。

管理域名更换：

1. 先完成新管理域名的 DNS、Nginx 与证书，**保留旧管理域名暂时可访问**。
2. 用旧管理域名登录后台 →「站点设置 → 管理域名」→ 用新域名替换旧域名 → 保存。
3. 保存后立即生效：新域名可访问 `/admin`，旧域名与已删除域名访问 `/admin` 一律 404；无需改 `.env`、无需重启。会话 Cookie 为 Host-only，管理员需在新域名重新登录。
4. `.env` / systemd 中的 `ADMIN_DOMAIN` 只在首次初始化或旧库迁移时写入数据库，之后修改不会覆盖数据库配置。
5. 如果旧管理域名已经无法访问：在本机通过 `http://127.0.0.1:8000/admin/login` 修改（回环地址始终放行）。服务器没有本机浏览器时，可先建立 SSH 隧道：
   ```bash
   ssh -L 8000:127.0.0.1:8000 user@server
   # 然后在本地浏览器访问 http://127.0.0.1:8000/admin/login
   ```

### 11. 部署后验证

先在服务器执行 `nginx -t && systemctl reload nginx`，再逐项确认：

```bash
# HTTP -> HTTPS 跳转
curl -I http://example.com/
# HTTPS 正常响应，且包含 HSTS
curl -sI https://example.com/ | grep -i strict-transport-security
# 主域名后台 404，管理域名登录页 200
curl -I https://example.com/admin
curl -I https://admin.example.com/admin/login
# 敏感路径不可访问（应为 404）
curl -I https://example.com/backups/
curl -I https://example.com/private_uploads/
curl -I https://example.com/.env
curl -I https://example.com/tools/backup_db.py
# 已通过图片公开访问、待审核图片 404（替换为实际文件相对路径）
curl -I https://example.com/uploads/2026/11/approved-example.webp
curl -I https://example.com/uploads/2026/11/pending-example.webp
```

应用层复核：

```bash
python tools/check_db.py --deep
python tools/check_uploads.py
```

> 上述 Nginx/HTTPS/证书/跳转验证必须在真实服务器执行；本地开发环境只能验证应用层行为。浏览器开发者工具中确认管理员 Cookie 带有 `Secure`、`HttpOnly`、`SameSite=Lax`。

### 安全建议

1. **HTTPS**：前后台域名均强制 HTTPS
2. **防火墙**：仅开放 80/443，应用端口 8000 仅监听 127.0.0.1
3. **端口隔离**：uvicorn 仅绑定 `127.0.0.1:8000`，不对外暴露 8000 端口
4. **上传目录与敏感目录不得由 Nginx 直接公开**：
   - `/uploads` 由应用路由按审核状态和管理员会话鉴权；
   - 不要配置 `location /uploads` 的 `alias`/`root`，否则会绕过私有图片保护；
   - `private_uploads/` 必须只由应用进程访问，**不要**加入任何 Nginx 静态 location；
   - 旧部署如存在 `alias /www/wwwroot/amig/uploads/`，升级时必须删除；
   - 不要把项目根目录配置为 Nginx 的 `root`：`data/`（数据库）、`backups/`、`.env`、`tools/` 都可能被直接下载；示例配置已加入显式拒绝规则。
5. **反向代理**：`proxy_set_header Host $host` 必须配置，否则后台域名隔离与 CSRF 同源校验会失效；`X-Real-IP` 与 `X-Forwarded-Proto` 只在来源属于 `TRUSTED_PROXIES` 时被信任，禁止把该配置设为 `0.0.0.0/0`。
6. **定期备份**：使用专用工具（不要在业务高峰直接复制数据目录）：
   ```bash
   python tools/backup_db.py --with-uploads
   python tools/restore_db.py --backup backups/backup_xxx.sql --check
   ```
   详见「数据备份与恢复」一节。
7. **回滚**：`git revert <commit>` 后重启服务；或恢复代码备份后重启。
8. **安全响应头**：应用已内置 `SecurityHeadersMiddleware` 自动添加 `X-Content-Type-Options: nosniff`、`X-Frame-Options: SAMEORIGIN`、`Referrer-Policy`、`Content-Security-Policy` 及 `Strict-Transport-Security`（默认 `max-age=31536000; includeSubDomains`）。CSP 的 `script-src` 与 `style-src` 均使用每请求一次性 nonce，不再允许任意内联脚本或内联样式。若使用 Cloudflare，请在面板中开启 HSTS 并勾选 **Include Subdomains**（覆盖管理子域名）。若 Nginx/Cloudflare 也要配置 CSP，请移除上游 CSP 或使用相同 nonce 机制，避免覆盖应用生成的 nonce 导致页面脚本被浏览器拦截。
9. **Cookie 安全标志**：生产环境请确保 `.env` 中 `COOKIE_SECURE=true`（默认已启用），使后台会话与 CSRF Cookie 仅在 HTTPS 下传输。
10. **依赖漏洞扫描**：定期执行 `pip-audit -r requirements.txt`（或 `safety check`），并及时升级 `python-multipart`、`starlette`、`Pillow` 等存在历史 CVE 的组件。

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

- 进入「修改密码」，必须输入当前密码进行验证
- 新密码至少 12 个字符（UTF-8 不超过 72 字节），不能全部为空格、单一重复或常见弱口令
- 修改成功后撤销全部旧会话，并为当前设备签发新的 Session 与 CSRF 令牌

#### 密码重置（忘记密码时）

在服务器项目根目录（宝塔终端）执行：

```bash
python reset_password.py                         # 交互式输入并确认新密码
python reset_password.py --yes                   # 跳过确认，仍会交互式输入密码
echo '你的新密码' | python reset_password.py --stdin --yes  # 自动化场景，避免密码进入命令行参数
```

- 已移除 `--password` 参数，避免密码出现在进程参数和 shell 历史中
- 新密码至少 12 个字符，并通过与后台一致的密码策略
- 同一事务内清除登录失败统计与锁定状态，并撤销全部旧会话
- 脚本不会在输出中打印新密码

---

## 目录结构

```
amig/
├── app/
│   ├── main.py              # 应用入口
│   ├── config.py            # 全局配置
│   ├── database.py          # MySQL 连接池、事务、建表与结构版本
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
├── uploads/                 # 已审核图片与 Logo（按年/月分目录）
├── private_uploads/         # 待审核/已拒绝图片（仅应用可读，Nginx 不得直接暴露）
├── data/                    # 旧 SQLite 存档与迁移工具默认数据源（运行时不再写入）
├── backups/                 # 备份文件（数据库快照 + 图片归档，勿提交/勿公开）
├── tools/                   # 运维工具（备份、恢复、健康检查、迁移、一致性检查）
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
A：确认服务器可以访问 HTTPS 归属地服务（主 `ip.zxinc.org`，回退 `ipapi.co`），并检查 `IP_API_TIMEOUT` 与 `IP_API_CACHE_TTL` 配置。私有地址、回环地址及查询失败不会显示归属地，也不会影响上传或点赞。

### 密码与账号类

**Q：忘记管理员密码？**
A：执行 `python reset_password.py` 后交互式设置新密码；自动化环境可通过 `--stdin` 从标准输入提供密码（详见上文「密码重置」）。重置会同时撤销全部旧会话。

**Q：修改密码需要原密码吗？**
A：需要。必须验证当前密码，防止仅凭被盗 Cookie 直接改密；新密码需至少 12 个字符。

**Q：管理员登录被锁定？**
A：第二阶段取消了全局账户锁定，失败次数只按来源 IP 限流（默认 5 次/15 分钟），窗口结束后自动恢复；不会再因多 IP 攻击把管理员账户锁死。若旧数据库仍有 `locked_until`，登录成功后会自动清除。

### 部署与迁移类

**Q：代码更新后不生效？**
A：Python 进程不会热加载，务必 `sudo systemctl restart amig.service`。

**Q：如何验证数据库迁移（SQLite → MySQL）是否成功？**
A：执行只读健康检查，会输出 MySQL 版本、字符集、表与索引数量、行数、点赞计数漂移等：

```bash
cd /www/wwwroot/amig && python3 tools/check_db.py --deep
```

预期输出 `[通过] 未发现问题`。迁移工具本身也会在结束时逐表对比行数并做逐字段内容级校验。

**Q：启动报“无法连接 MySQL”或“连接池已耗尽”？**
A：前者检查 `.env` 的 `MYSQL_HOST/PORT/DATABASE/USER/PASSWORD`、MySQL 是否运行、账号是否允许从该主机登录；后者说明并发超过 `MYSQL_POOL_SIZE + MYSQL_POOL_MAX_OVERFLOW`，请适度调大但仍需保证总量小于 MySQL `max_connections`。

**Q：备份时报找不到 mysqldump？**
A：安装 MySQL 客户端工具，或在 `.env` 设置 `MYSQLDUMP_PATH` / `MYSQL_CLIENT_PATH` 为可执行文件完整路径（Windows 默认在 `C:\Program Files\MySQL\MySQL Server 8.x\bin`）。

---

## 附录：数据表结构

| 表名 | 用途 |
|------|------|
| `images` | 图片信息（文件名、尺寸、大小、标题、简介、状态、IP、地区、点赞/浏览计数等） |
| `likes` | 点赞记录（图片 ID、IP、UA 哈希、地区） |
| `ban_list` | 封禁列表（IP/CIDR/UA 哈希） |
| `admins` | 管理员账号（bcrypt 密码哈希、失败统计；旧 `locked_until` 字段保留兼容但不再全局锁定） |
| `settings` | 站点配置（键值对，含管理域名） |
| `pages` | 自定义页面 |
| `friend_links` | 友情链接 |
| `daily_stats` | 每日统计（上传/通过/拒绝/点赞/浏览） |
| `admin_logs` | 管理员操作日志（含 IP） |
| `sessions` | 管理员会话（持久化、空闲/绝对超时、会话绑定 CSRF） |
| `login_rate_limits` | 登录失败限流（按来源 IP，多 worker 共享） |
| `view_sessions` | 浏览去重记录（`(image_id, ip)` 唯一） |
| `cooling` | 冷却机制（拒绝次数、冷却倍数） |
| `upload_reservations` | 上传额度预约（限额检查与占用原子化） |
| `schema_migrations` | 结构版本记录 |

> 全部表为 InnoDB + `utf8mb4` / `utf8mb4_unicode_ci`；主键 `INT AUTO_INCREMENT`（`sessions` 用 `VARCHAR(64)` 会话 ID 作主键）；`likes` 对 `images` 有真实外键级联删除。时间字段沿用 ISO 8601 字符串（`VARCHAR(40)`），保证与历史数据格式一致。

> 数据库为 MySQL 8.0+（库名 `amig`，InnoDB + utf8mb4）。可用 `mysql -u amig -p amig` 或任意 MySQL 客户端查看数据；旧 SQLite 文件仅作迁移存档保留在 `data/app.db`。
