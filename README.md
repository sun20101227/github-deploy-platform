# GitHub Deploy Platform

一个面向自托管服务器的 GitHub 项目部署平台原型。

目标流程：

```text
GitHub OAuth 登录
  → 选择账号可访问的仓库
  → 创建部署项目
  → 部署到服务器
  → 检查新 Commit
  → 在新版本目录构建和自检
  → 原子切换并重启
  → 失败自动回滚
```

当前版本先实现后端基础层：

- SQLite 保存 GitHub 账号、项目、部署记录和版本指针；
- GitHub OAuth 登录接口和仓库查询接口；
- 每个项目独立的发布目录、`current` 指针和 `previous` 回滚信息；
- 通过项目配置执行 clone、构建、健康检查和启动命令；
- 更新过程写入可轮询的部署状态；
- 项目创建后可配置 GitHub Push Webhook，自动触发对应分支更新。

## 开发运行

```powershell
cd github-deploy-platform
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
$env:DEPLOY_DATABASE = "$pwd\data\platform.db"
uvicorn deploy_platform.api:app --reload
```

GitHub OAuth 需要配置：

```text
GITHUB_CLIENT_ID
GITHUB_CLIENT_SECRET
GITHUB_REDIRECT_URI=http://127.0.0.1:8000/auth/github/callback
DEPLOY_SECRET_KEY
```

部署命令目前故意保持为项目配置的一部分，后续可以增加 Docker、systemd 和多服务器 Agent 适配器。

## Webhook

创建项目后，页面会显示 Webhook 地址。到 GitHub 仓库的 Settings → Webhooks 配置：

- Payload URL：`https://你的域名/webhooks/github/<项目ID>`
- Content type：`application/json`
- Secret：项目创建时自动生成的密钥
- Events：只勾选 Push

其他分支的 Push 会被忽略。匹配分支的 Push 会进入和手动更新相同的版本准备流程。

