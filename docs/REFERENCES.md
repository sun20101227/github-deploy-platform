# 参考项目与采用范围

查阅日期：2026-11-15。

| 项目 | 适合参考的部分 | 本项目的取舍 |
| --- | --- | --- |
| [Coolify](https://github.com/coollabsio/coolify) | 自托管应用管理、Git 来源、Compose 部署 | 参考代码更新与构建/部署分离，不重新实现容器编排器 |
| [Dokploy](https://github.com/Dokploy/dokploy) | 应用与 Compose 管理、部署记录 | 将项目、运行配置、任务记录拆开，复用 Docker/Compose 执行服务操作 |
| [Dokku](https://github.com/dokku/dokku) | Git 驱动部署、进程生命周期 | 拉取代码后必须启动/重启并检查结果，而不是只报告 Git 更新成功 |

| [doco-cd](https://github.com/kimdre/doco-cd) | 用 Webhook 和轮询让 Compose 项目跟随 Git 仓库更新 | 功能最接近“自动更新”这一块；参考它固定 Compose 项目名、按仓库内的 Compose 文件部署。它没有管理页面，也不接管 systemd 服务 |
| [Komodo](https://github.com/moghtech/komodo) | 多台服务器的构建、部署和 Compose 管理界面 | 需要管理多台服务器时直接评估它，本项目不扩展到多机 |
| [Dockge](https://github.com/louislam/dockge) | Compose 文件的管理界面 | 只参考“以服务器上的 Compose 文件为准”，它本身不做 Git 自动更新 |

这些都是完整的部署工具，不是可以直接引入的 Python 库。如果只需要让 Compose 项目跟随 GitHub 自动更新，doco-cd 已经够用；需要多服务器管理、代理域名与证书编排，可以直接评估 Coolify、Dokploy 或 Komodo。当前项目聚焦单台服务器、gh 登录、已有部署接入（含 systemd 服务）和带回滚的管理页面。

## 已使用的第三方库

- [GitPython](https://gitpython.readthedocs.io/en/stable/tutorial.html)：识别仓库、读取分支/提交/工作区状态、快进合并和安全回退。网络 fetch 仍通过 Git 子进程执行，以便跨平台设置超时，并在临时环境中传入 gh 凭据。
- [Docker SDK for Python](https://docker-py.readthedocs.io/en/stable/containers.html)：读取容器及 Compose 标签。只向页面返回名称、状态和项目路径；不返回容器环境变量。
- [python-on-whales](https://gabrieldemarmiesse.github.io/python-on-whales/sub-commands/compose/)：调用 Docker Compose 构建、重建和等待启动。使用原项目名，不自动 down，不移除卷。
- [Tenacity](https://tenacity.readthedocs.io/)：健康检查的限时重试，代替手写的循环和计时。
- [python-dotenv](https://github.com/theskumar/python-dotenv)：启动时加载项目根目录的 `.env`。
- FastAPI / Pydantic / HTTPX：沿用现有接口、输入验证和 HTTP 请求基础。

## 评估后暂未引入

- [githubkit](https://github.com/yanyongyu/githubkit) / PyGithub：可以替换 `github.py` 里的两个 GitHub API 调用和 Webhook 验签。目前只有约 40 行，引入后依赖体积明显变大，收益小。GitHub 调用增多时再换。
- [Huey](https://github.com/coleifer/huey)（SQLite 存储）：可以替换部署任务的线程和锁，让排队的任务在平台重启后继续。需要改任务模型并多一个消费进程或线程，属于单独的一次改造。

Compose 的配置以服务器原文件为准。参考 [Coolify 构建部署说明](https://coolify.io/docs/core/build-deployment-model) 和 [Compose 部署文档](https://coolify.io/docs/applications/builds/docker-compose)，没有复制其源码，也没有把本项目描述成具备其全部功能。
