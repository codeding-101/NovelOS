# NovelOS 控制平台部署与开发

适用阶段：M1—M3 路由基础工作包。作品资产、人工／本地任务、预算中心、执行者登记和本地路由已开放，真实 AI 执行和朱雀检测尚未开放。本指南使用新入口，不使用旧版 `app.main:app`。

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

Worker 的派生文件目录挂在 `platform-files` 卷，文件可从数据库重建。这个卷不替代 PostgreSQL 与加密主密钥备份。容器和 HTTPS 的实测待办见执行记录。

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

Worker 报告心跳，处理原稿／附件文件副本和检索索引，恢复过期任务并执行本地确定性校验。运行 `--once` 会处理最多 20 个派生同步待办、扫描最多 100 个待恢复任务并执行至多 1 个本地任务，然后记录正常停止状态；正常停止后控制台不将其显示为在线工作进程。文件同步与任务步骤分别捕获异常，文件适配器故障不会跳过任务处理。人工任务由网站领取；使用方法与期限见 [任务中心指南](task-runtime.md)。

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

更稳妥的做法是盘点在线备份生成的数据库副本。报告包含数量、章节标识、原文哈希和迁移目标状态，不打印正文和密钥。显式导入、来源标识和隔离演练见 [作品资产指南](work-assets.md)。启动应用不会自动导入旧库。

## 4. 验证命令

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest tests\test_platform_foundation.py tests\test_platform_works.py tests\test_platform_delivery.py tests\test_platform_tasks.py tests\test_platform_budgets.py tests\test_platform_executors.py tests\test_platform_agents.py tests\test_platform_models.py tests\test_platform_task_budgets.py tests\test_platform_quotas.py tests\test_platform_memory.py -v
```

默认测试 SQLite；设置 `NOVELOS_TEST_POSTGRES_URL` 后也运行 PostgreSQL 用例。该地址必须指向隔离测试数据库；每个 PostgreSQL 用例创建唯一 schema，结束时只删除自己的 schema。覆盖登录保护、持久限流、CSRF、会话过期和撤销、凭证加密、不泄露校验输入、未知迁移版本、重复初始化和只读旧库盘点。

浏览器验收使用独立临时数据库、临时端口和测试账户：

```powershell
.\backend\.venv\Scripts\python.exe -m pip install -r backend\requirements-browser.txt
.\backend\.venv\Scripts\python.exe scripts\platform_smoke.py
```

Windows 优先使用已安装 Edge 或 Chrome；也可通过 `NOVELOS_BROWSER_PATH` 指定兼容浏览器。其他系统使用 Playwright 已安装的 Chromium。脚本验证登录、凭证、API 重启后持久化、作品编辑、双页面冲突、历史比较与恢复、导入导出、人工检查点刷新接续、暂停恢复、候选不改正文、本地自动执行、取消、手机布局和退出，并在 `data/validation` 保存报告及截图。测试用服务结束后停止，不改动用户原库。

## 5. 升级与恢复边界

升级前保留数据库及主密钥备份；版本变更通过 `python -m app.platform.cli upgrade` 或 migrate 服务执行。`python -m app.platform.cli check` 仅检查版本。未知版本和未受管理的非空库会被拒绝，不能用强制 stamp 掩盖不兼容。

当前 schema 版本为 `0014_task_contexts`；本次迁移新增不可变任务与快照绑定表及索引，旧任务保持未绑定；创建、领取、恢复、发送登记与回报校验绑定依据，不修改历史调用或正文。已有记忆、故事状态与旧请求幂等行为保留。旧正文与任务结果不会自动成为正式记忆，旧 CANON／COMPLETED 也不自动升级；原预算、调用与资产保持不变。此前历史模型任务仍须补齐任务预算才能恢复。公网模型调用保持关闭。API 与 Worker 都须升级后再启动。迁移不提供破坏性降级，恢复旧应用版本须使用升级前数据库备份。操作见 [记忆提案与人工审核](memory-review.md) 、[故事状态与时点查询](story-states.md) 、[上下文快照](context-snapshots.md) 和 [任务上下文](task-contexts.md)。

当前尚无完整生产备份调度和异机恢复工具。M2 的资产与派生副本恢复、M3 首个工作包的本地任务恢复已通过本地验收；生产使用仍需完成部署验证、真实执行者与预算故障恢复、M9 备份恢复验收。M0 旧库快照脚本不能证明 PostgreSQL 全平台灾难恢复已经覆盖。

## 6. 当前限制

- 作品空间、后台重建、旧库导入限制和升级说明见 [作品与版本指南](work-assets.md)；本地验收不能代替服务器部署验收。
- 预算配置和未知费用处理见 [作品预算指南](work-budgets.md)；当前是作品累计额度，未实现供应商月度免费额度、真实价格和用量解析。浏览器预算验收使用标明无真实调用的隔离样例。
- 执行者声明和作品规则见 [执行者路由指南](executor-routing.md)；可用状态来自用户声明，外部登记不意味着适配器已接通。浏览器验收包含登记、规则刷新持久化、排除原因和实际本地自动任务。
- 离线 Agent 可凭任务专属令牌主动领取和回报，接入方式见 [Agent 指南](agent-gateway.md)。浏览器验收包含一次性令牌签发、独立客户端经网站代理完成任务、重交去重和撤销拒绝；客户端不调用模型。
- 模型 API 配置及本机联调见 [模型协议指南](model-api-rehearsal.md)。仅隔离测试环境的回环模拟服务可执行；普通开发和生产 Worker 不执行模型调用，不能通过环境开关启用公网请求。
- 不调用真实模型，不自动导入旧作品，不开放定稿。
- 凭证支持加密保存、列出元数据和删除；供应商联通性、轮换与任务使用后续实施。
- 当前登录限流为每个服务端观察到的客户端地址每 15 分钟最多 5 次尝试；成功尝试也计数。服务处在反向代理后，单用户共享限制符合当前使用范围。
- Docker Compose 容器运行、HTTPS 证书和卷持久化的实测状态以执行记录为准；配置文件存在不等于部署验收已完成。
