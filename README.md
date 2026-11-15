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
.venv/bin/python -m deploy_platform
```

Windows 使用 `.venv/Scripts/python -m deploy_platform`，环境变量用 PowerShell 的 `$env:DEPLOY_ADMIN_TOKEN` 设置。通过 `DEPLOY_HOST`、`DEPLOY_PORT` 调整监听地址和端口。配置通过环境变量读取；`.env.example` 不会自动加载。Linux 常驻运行可参考 [systemd 配置](deploy/deploy-platform.service)，由 systemd 的 `EnvironmentFile` 加载环境变量。

在 HTTPS 反向代理后使用，管理密钥在网页解锁时输入，仅存在当前浏览器会话。未设置密钥只允许本机直连开发访问；反向代理必须传递 `X-Forwarded-For`。当前使用单进程运行，不要设置多个 workers。

## 页面

- **项目**：搜索项目、查看最新任务状态。
- **项目详情**：概览、部署记录、设置分开显示；部署进行中自动刷新记录。部署记录显示最近 50 条。
- **新建项目**：选择账号和仓库，填写运行配置。仓库列表按最近更新排序，最多读取 1000 个。
- **已有项目**：扫描当前服务器上的目录，识别 Git 仓库、关联的 Compose 容器和 systemd 服务，再接入管理。
- **GitHub**：连接服务器的 `gh` 账号；不要求创建 OAuth App。服务用户必须能读取自己的 gh 凭据，容器不能自动读取宿主机的登录状态。
- **平台更新**：检查并快进更新平台自身的 Git 分支，安装更新后的 Python 依赖，自动重启。页面用进程实例编号确认服务恢复；正在部署其他项目时禁止更新平台。

旧安装需要先把启动命令改成 `python -m deploy_platform` 并重启一次。直接使用 uvicorn 启动仍可管理项目，但平台更新按钮会停用，避免更新后没有进程重新启动。启动器的依赖安装失败会退出并写入服务日志，不能保证任意错误版本自动恢复；此时需要终端修复。不要设置多个 workers。

## 接入已部署项目

在“已有项目”输入 `/opt/apps` 等目录。扫描只读取仓库信息、容器标签和服务的工作目录，不读取 `.env` 内容，也不会立即重启服务。目录指的是平台运行所在机器；容器内运行的平台只能看到挂载进去的目录和授予它的 Docker 接口。

接入时选择运行方式：

- **Docker Compose**：确认原项目名和配置文件顺序。每次更新执行 `up --build --force-recreate --wait`，沿用项目名，不删除卷。需要 Docker Engine 和 Compose v2。非默认 profiles、额外 `--env-file` 或远程 Docker context 的部署请使用自定义命令保留原参数；SDK 扫描使用 `DOCKER_HOST` 环境配置。
- **systemd**：选择工作目录匹配的服务。更新后执行 `systemctl restart` 并检查 `is-active`。运行平台的用户需要对应服务的管理权限。
- **自定义命令**：填写现有服务的重启命令，例如 `pm2 restart my-app --update-env`。命令需等待重启结果并退出；推荐另设 HTTP 健康检查。

已有项目保留原目录。更新会确认仓库、分支和工作区，再拉取 GitHub 提交并快进合并，构建后重启。失败时尝试恢复旧提交、重新构建和重启旧版本。被 Git 忽略的 `.env` 和数据文件保留；已跟踪的配置属于代码，会随提交更新。有本地修改、未跟踪文件、分叉或子模块时停止更新，不强制覆盖。回滚不撤销数据库迁移。

扫描深度最多四层，一次最多检查 500 个目录、返回 100 个仓库。无 Git 元数据的拷贝目录不能自动接入；没有源码路径标签的容器可以查看，但需要手动找到对应 Git 目录。

## 部署约定

每个项目使用独立目录：

```text
项目目录/
  current              # 文本文件，内容是当前版本的绝对路径
  releases/<任务号>/   # 每次部署的独立代码目录
```

这一目录结构仅用于“新建项目”；接入已有项目不会改造目录。构建和启动命令在新版本目录执行。启动命令必须执行启动或重启并及时退出，不能直接运行前台常驻进程。Compose 命令应固定项目名，例如 `docker compose -p my-app up -d --build --force-recreate --wait`，防止每个版本另建一套容器。启动时额外提供 `DEPLOY_RELEASE_DIR`、`DEPLOY_PROJECT_ROOT` 环境变量。若使用 systemd，自行配置服务读取正确的版本路径；`current` 是文本指针，不是目录链接。

设置健康检查 URL 后，启动成功还必须返回 HTTP 2xx。每秒检查一次，默认最长等待 45 秒，可在项目设置里调整为 5–600 秒。切换失败会恢复旧指针、重新执行旧版本启动命令并检查健康；结果写入部署记录。首次部署没有旧版可恢复，可能需要在进程管理器中清理失败进程。回滚只处理代码，不回退数据库。持久数据应放在版本目录之外。

部署成功后清理旧的版本目录：保留最近 5 个任务目录（`DEPLOY_KEEP_RELEASES` 可调整），当前版本和可回滚的上一版本始终保留。只删除 `releases/` 下以任务号命名的目录。

同一项目的部署与回滚串行执行。当前任务运行在线程内，外部强制重启仍会中断任务；平台下次启动时把这些未结束的任务标记为失败并写明原因，项目可以继续修改配置和重新部署，但服务可能停在中间状态，需要先检查。网页平台更新会等待任务结束后才允许执行。构建输出在命令结束后显示，尚不是逐行实时日志。

## 自动更新

在项目设置展开 Webhook，复制 URL 和 Secret。GitHub 仓库 Settings → Webhooks 中设置 `application/json`、Push 事件。只接受签名正确、仓库和分支匹配的推送；删除分支事件忽略。

部署进行中收到的推送不会丢弃：当前任务结束后自动再更新一次，多次推送合并为一次。这个标记只在内存里，平台重启后需要再推送或手动更新。

## 验证

```bash
python -m pip install pytest
python -m pytest -q
node --check deploy_platform/web/app.js
node --check deploy_platform/web/discovery.js
```

测试使用临时数据库、真实临时 Git 仓库和模拟服务调用，不会部署真实项目。Linux systemd 和 Docker Engine 的实际重启需要在目标服务器验收。

## 参考与依赖

同类平台包括 [Coolify](https://github.com/coollabsio/coolify)、[Dokploy](https://github.com/Dokploy/dokploy) 和 [Dokku](https://github.com/dokku/dokku)。本项目借鉴它们将代码更新与服务生命周期分开的方式，实际使用 GitPython、Docker SDK 和 python-on-whales；不内嵌或冒充这些完整平台。对比和采用范围见 [参考说明](docs/REFERENCES.md)。
