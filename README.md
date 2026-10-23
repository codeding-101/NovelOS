# NovelOS

面向个人长篇小说创作的运行环境与控制平台，以部署到服务器的网站为主要形态。模型 API、外部 Agent 和用户负责执行创作任务；NovelOS 管理作品资产、正式记忆、权限、预算、质量检查和执行恢复。

项目正在按新需求分阶段重组。当前已交付 M0 基线保护与 M1 控制平台基础：登录、持久会话、加密凭证、显式数据库迁移、独立 Worker 心跳和运行状态页面。作品迁移、任务执行、AI 创作及真实朱雀检测仍按后续阶段建设。M1 控制台不会调用外部模型或授予正文定稿状态。

## 文档

- [需求与开发规划](docs/planning/README.md)
- [当前执行记录和验收结果](docs/planning/execution-status.md)
- [部署与本地开发指南](docs/operations/platform-setup.md)
- [M1 架构决策](docs/decisions/0001-platform-foundation.md)
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

旧实现保留在原有模块中，旧工作台组件保存在 `frontend/legacy/Workbench.tsx`。生产服务只注册新平台接口，旧的未受保护接口不会同时开放。旧作品通过只读盘点与后续 M2 迁移进入新平台。

## 验证

```bash
cd backend
python -m pip install -r requirements-dev.txt
python -m pytest
# 配置隔离的 NOVELOS_TEST_POSTGRES_URL 后，同一组用例也会验证真实 PostgreSQL。
python -m pytest tests/test_platform_foundation.py -v
```

前端构建：在 `frontend` 执行 `npm ci` 和 `npm run build`。浏览器验收与容器冒烟见部署指南和 GitHub Actions 配置。真实模型测试默认跳过；模拟结果不能替代朱雀检测证据。
