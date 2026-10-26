# NovelOS

面向个人长篇小说创作的运行环境与控制平台，以部署到服务器的网站为主要形态。模型 API、外部 Agent 和用户负责执行创作任务；NovelOS 管理作品资产、正式记忆、权限、预算、质量检查和执行恢复。

项目正在按新需求分阶段重组。已建立登录、持久会话、加密凭证和显式迁移；作品空间支持草稿编辑、不可变版本、比较恢复、原稿导入、附件、版本引用、文字检索与带校验清单的导出。后台可重试文件同步和索引重建，旧 SQLite 可通过管理命令只读导入独立新库。任务中心支持人工、本地校验与离线 Agent、依赖、检查点、有限重试和候选隔离；预算中心提供累计费用／token 上限、预留、结算去重与未知费用对账。执行者设置支持能力声明、作品选择规则和预算检查；离线 Agent 使用任务专属令牌领取与回报，配有可恢复参考客户端。模型 API 配置、预算预留和用量解析已通过本机模拟服务联调。真实公网模型调用、付费 Agent 及朱雀检测尚待后续工作包；当前不调用外部模型、不授予正文定稿状态，容器部署验收也尚未完成。

## 文档

模型任务已支持跨尝试累计的费用与 token 上限，与作品总预算共同生效；额度不足会阻止继续执行，网页可调整并手动恢复。按用户当前要求，真实模型调用继续保持关闭。

月度配额页面支持多个模型执行者跨作品共享 token 上限，提供固定月界、未知用量占用、预警及配置历史。这是本地额度控制，不代表已接入朱雀或获得供应商余额。

- [需求与开发规划](docs/planning/README.md)
- [当前执行记录和验收结果](docs/planning/execution-status.md)
- [部署与本地开发指南](docs/operations/platform-setup.md)
- [M1 架构决策](docs/decisions/0001-platform-foundation.md)
- [作品编辑、导入与历史恢复](docs/operations/work-assets.md)
- [M2 草稿版本决策](docs/decisions/0002-immutable-drafts.md)
- [附件、后台恢复与旧库导入决策](docs/decisions/0003-asset-delivery.md)
- [任务中心与中断恢复](docs/operations/task-runtime.md)
- [持久任务与候选结果决策](docs/decisions/0004-task-runtime.md)
- [作品预算与未知费用对账](docs/operations/work-budgets.md)
- [预算与调用账本决策](docs/decisions/0005-work-budgets.md)
- [执行者登记与作品路由](docs/operations/executor-routing.md)
- [执行者路由决策](docs/decisions/0006-executor-routing.md)
- [离线 Agent 接入与恢复](docs/operations/agent-gateway.md)
- [任务专属 Agent 网关决策](docs/decisions/0007-agent-gateway.md)
- [模型 API 配置与本机协议验收](docs/operations/model-api-rehearsal.md)
- [模型调用事务与发布边界决策](docs/decisions/0008-model-api-rehearsal.md)
- [任务累计预算与恢复](docs/operations/task-budgets.md)
- [任务累计预算决策](docs/decisions/0009-task-budgets.md)
- [共享月度配额操作指南](docs/operations/monthly-quotas.md)
- [月度配额与调用归属决策](docs/decisions/0010-monthly-quotas.md)
- [旧版使用说明归档](docs/legacy/README.md)

## 服务器部署

使用 Docker Compose，按 [部署指南](docs/operations/platform-setup.md) 配置域名、数据库密码和加密主密钥，再执行：

```bash
cp .env.example .env
# 填写 .env 中的必需配置后继续
# 初始化迁移是独立服务；API 启动只检查数据库版本。
docker compose --profile https up -d --build
docker compose exec backend python -m app.platform.cli init-owner --username owner
```

生产模式要求 HTTPS 与 PostgreSQL。账户通过服务器命令初始化，网站没有开放注册。模型密钥不下发到浏览器，数据库只保存加密后的凭证。

## 本地开发

后端入口为 `app.platform.api:create_app --factory`，配置、数据库初始化及启动命令见部署指南。前端使用 `npm run dev`。新平台开发库默认位于 `data/platform/novelos.db`，不会自动加载旧版 `backend/.env` 或迁移原有 `data/novelos.db`。

旧实现保留在原有模块中，旧工作台组件保存在 `frontend/legacy/Workbench.tsx`。生产服务只注册新平台接口，旧的未受保护接口不会同时开放。旧作品须通过显式的只读导入命令进入新库；启动服务不会自动迁移用户作品。

## 验证

```bash
cd backend
python -m pip install -r requirements-dev.txt
python -m pytest
# 配置隔离的 NOVELOS_TEST_POSTGRES_URL 后，同一组用例也会验证真实 PostgreSQL。
python -m pytest tests/test_platform_foundation.py tests/test_platform_works.py tests/test_platform_delivery.py tests/test_platform_tasks.py tests/test_platform_budgets.py tests/test_platform_executors.py tests/test_platform_agents.py tests/test_platform_models.py tests/test_platform_task_budgets.py tests/test_platform_quotas.py -v
```

前端构建：在 `frontend` 执行 `npm ci` 和 `npm run build`。浏览器验收与容器冒烟见部署指南和 GitHub Actions 配置。真实模型测试默认跳过；模拟结果不能替代朱雀检测证据。
