# Deploy

在自己的服务器上管理 GitHub 项目。使用 GitHub CLI 登录，支持部署、更新、查看执行记录和回滚。

## 运行

需要 Python 3.11+、Git 和 GitHub CLI。请用运行平台的系统用户登录：

```bash
gh auth login --hostname github.com --web
git clone https://github.com/sun20101227/github-deploy-platform.git
cd github-deploy-platform
python3 -m venv .venv
.venv/bin/pip install -e .
export DEPLOY_ADMIN_TOKEN='替换成随机管理密钥'
.venv/bin/uvicorn deploy_platform.api:app --host 127.0.0.1 --port 8000
```

Windows 使用 `.venv/Scripts/python`、`.venv/Scripts/uvicorn`，环境变量用 PowerShell 的 `$env:DEPLOY_ADMIN_TOKEN` 设置。配置通过环境变量读取；`.env.example` 不会自动加载。可显式使用 uvicorn 的 `--env-file .env`。

在 HTTPS 反向代理后使用，管理密钥在网页解锁时输入，仅存在当前浏览器会话。未设置密钥只允许本机直连开发访问；反向代理必须传递 `X-Forwarded-For`。当前使用单进程运行，不要设置多个 workers。

## 页面

- **项目**：搜索项目、查看最新任务状态。
- **项目详情**：概览、部署记录、设置分开显示；部署进行中自动刷新记录。
- **新建项目**：选择账号和仓库，填写运行配置。
- **GitHub**：连接服务器的 `gh` 账号；不要求创建 OAuth App。服务用户必须能读取自己的 gh 凭据，容器不能自动读取宿主机的登录状态。
- **平台更新**：检查并快进更新平台自身的 Git 分支。代码更新后需手动重启平台服务，界面会明确提示。

## 部署约定

每个项目使用独立目录：

```text
项目目录/
  current              # 文本文件，内容是当前版本的绝对路径
  releases/<任务号>/   # 每次部署的独立代码目录
```

构建和启动命令在新版本目录执行。启动命令必须调用进程管理器并及时退出（例如 Docker Compose），不能直接运行前台常驻进程。启动时额外提供 `DEPLOY_RELEASE_DIR`、`DEPLOY_PROJECT_ROOT` 环境变量。若使用 systemd，自行配置服务读取正确的版本路径；`current` 是文本指针，不是目录链接。

设置健康检查 URL 后，启动成功还必须返回 HTTP 2xx。切换失败会恢复旧指针、重新执行旧版本启动命令并检查健康；结果写入部署记录。首次部署没有旧版可恢复，可能需要在进程管理器中清理失败进程。回滚只处理代码，不回退数据库。持久数据应放在版本目录之外。

同一项目的部署与回滚串行执行。当前任务运行在线程内，平台重启会中断任务；请在任务结束后更新平台。构建输出在命令结束后显示，尚不是逐行实时日志。

## 自动更新

在项目设置展开 Webhook，复制 URL 和 Secret。GitHub 仓库 Settings → Webhooks 中设置 `application/json`、Push 事件。只接受签名正确、仓库和分支匹配的推送；删除分支事件忽略。

## 验证

```bash
python -m pip install pytest
python -m pytest -q
node --check deploy_platform/web/app.js
```

测试使用临时数据库和模拟启动命令，不会部署真实项目。
