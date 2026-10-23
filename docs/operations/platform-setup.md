# NovelOS 控制平台部署与开发

适用阶段：M1。作品管理、AI 任务和朱雀检测尚未开放。本指南使用新入口，不使用旧版 `app.main:app`。

## 1. 服务器部署

准备 Docker Compose、指向服务器的域名及可用的 80／443 端口。复制根目录 `.env.example` 为 `.env`，填写数据库密码、域名、公共 HTTPS 地址和 Fernet 主密钥。主密钥可在已安装后端依赖的环境中生成：

```bash
cd backend
python -m app.platform.cli generate-key
```

将输出保存在 `.env` 的 `NOVELOS_MASTER_KEY`，并在仓库外单独安全保存恢复副本；不提交 `.env`。`POSTGRES_PASSWORD` 也须设置。应用通过结构化 URL 编码使用数据库密码，无须自行转义特殊字符。

```bash
docker compose config --quiet
docker compose --profile https up -d --build
docker compose exec backend python -m app.platform.cli init-owner --username owner
```

初始化命令交互输入至少 12 字符的密码；现有账户不会被覆盖。自动化初始化可以通过进程环境变量 `NOVELOS_BOOTSTRAP_PASSWORD` 提供临时密码，避免将密码放进命令行参数、Git 或日志。完成后移除该临时环境变量。

打开配置的 HTTPS 地址登录。前端 3000 端口仅绑定服务器回环地址；公网入口由 Caddy 提供。数据库、后端和 Worker 不映射公网端口。

服务顺序为 PostgreSQL 健康 → migrate 完成 → API 与 Worker → 网站。数据库使用名为 `platform-postgres` 的持久卷，PostgreSQL 18 的卷挂载点为 `/var/lib/postgresql`。`docker compose down` 保留卷；不要为普通停止增加 `-v`。

生产必须设置 `NOVELOS_ENV=production`，使用 PostgreSQL 和 HTTPS 公共地址。主密钥缺失、格式错误、数据库版本不匹配会使服务拒绝启动。

## 2. Windows 本地开发

以下命令从 `backend` 目录运行。新平台不自动读取旧版 `backend/.env`。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:NOVELOS_ENV = 'development'
$env:NOVELOS_PUBLIC_ORIGIN = 'http://localhost:3000'
# 首次生成后妥善保存；以后启动必须使用相同主密钥。
$env:NOVELOS_MASTER_KEY = (& .\.venv\Scripts\python.exe -m app.platform.cli generate-key)
.\.venv\Scripts\python.exe -m app.platform.cli upgrade
.\.venv\Scripts\python.exe -m app.platform.cli init-owner --username owner
.\.venv\Scripts\python.exe -m uvicorn app.platform.api:create_app --factory --host 127.0.0.1 --port 8000 --no-proxy-headers
```

默认新数据库为 `data/platform/novelos.db`。如果已有旧后端占用 8000，请选择新的端口，并在前端环境中设置对应的 `NOVELOS_BACKEND_URL`。

前端在另一个终端启动：

```powershell
cd frontend
npm.cmd ci
# 如后端使用不同端口：$env:NOVELOS_BACKEND_URL = 'http://127.0.0.1:8001'
npm.cmd run dev
```

Worker 在另一个终端设置相同环境变量后运行：

```powershell
cd backend
.\.venv\Scripts\python.exe -m app.platform.worker
```

M1 Worker 仅报告心跳，不执行创作。运行 `--once` 会检查数据库并记录正常停止状态，供运维检查使用。正常停止后控制台不将其显示为在线工作进程。

本地也可设置 `NOVELOS_DATABASE_URL=postgresql+psycopg://...` 使用 PostgreSQL。Compose 则通过 `NOVELOS_DB_HOST/PORT/NAME/USER/PASSWORD` 分项传递连接信息。禁止将实际连接密码提交到示例或文档。

## 3. 旧数据保护与迁移预检

仓库根目录执行：

```powershell
.\backend\.venv\Scripts\python.exe scripts\baseline_snapshot.py
.\backend\.venv\Scripts\python.exe scripts\baseline_snapshot.py --verify data\restructure-backups\<快照目录>
```

脚本保存代码、未提交差异、未跟踪文件、配置和数据，并生成 Git bundle。SQLite 使用在线备份包含已提交 WAL，备份数据库执行完整性检查；其他文件逐个校验 SHA-256。快照位于忽略目录，包含私人作品和配置，不能上传到 GitHub。旧文件镜像与活跃数据库不是跨文件事务快照，原文恢复优先依据数据库及已记录差异，不能宣称它们来自同一全局时点。

只读盘点命令：

```powershell
cd backend
.\.venv\Scripts\python.exe -m app.platform.cli inspect-legacy ..\data\novelos.db --output ..\data\legacy-inventory.json
```

更稳妥的做法是盘点在线备份生成的数据库副本。报告包含数量、章节标识、原文哈希和迁移目标状态，不打印正文和密钥。M1 只盘点；实际写入新作品模型由 M2 实施。

## 4. 验证命令

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest tests\test_platform_foundation.py -v
```

默认测试 SQLite；设置 `NOVELOS_TEST_POSTGRES_URL` 后也运行 PostgreSQL 用例。该地址必须指向隔离测试数据库；每个 PostgreSQL 用例创建唯一 schema，结束时只删除自己的 schema。覆盖登录保护、持久限流、CSRF、会话过期和撤销、凭证加密、不泄露校验输入、未知迁移版本、重复初始化和只读旧库盘点。

浏览器验收使用独立临时数据库、临时端口和测试账户：

```powershell
.\backend\.venv\Scripts\python.exe -m pip install -r backend\requirements-browser.txt
.\backend\.venv\Scripts\python.exe scripts\platform_smoke.py
```

Windows 优先使用已安装 Edge 或 Chrome；也可通过 `NOVELOS_BROWSER_PATH` 指定兼容浏览器。其他系统使用 Playwright 已安装的 Chromium。脚本验证登录、密钥清空、凭证保存、API 重启后的会话和数据、移动布局、删除与退出，并在 `data/validation` 保存报告及截图。测试用服务结束后停止，不改动用户原库。

## 5. 升级与恢复边界

升级前保留数据库及主密钥备份；版本变更通过 `python -m app.platform.cli upgrade` 或 migrate 服务执行。`python -m app.platform.cli check` 仅检查版本。未知版本和未受管理的非空库会被拒绝，不能用强制 stamp 掩盖不兼容。

M1 尚无完整生产备份调度和异机恢复工具。现阶段在正式存放新作品前，需完成 M2 的资产版本、M3 的任务恢复和 M9 的备份恢复验收。不要把 M0 旧库快照脚本用于宣称已覆盖 PostgreSQL 全平台灾难恢复。

## 6. 当前限制

- 不调用真实模型，不领取 Agent 任务，不自动导入旧作品，不开放定稿。
- 凭证支持加密保存、列出元数据和删除；供应商联通性、轮换与任务使用后续实施。
- 当前登录限流为每个服务端观察到的客户端地址每 15 分钟最多 5 次尝试；成功尝试也计数。服务处在反向代理后，单用户共享限制符合当前使用范围。
- Docker Compose 容器运行、HTTPS 证书和卷持久化的实测状态以执行记录为准；配置文件存在不等于部署验收已完成。
