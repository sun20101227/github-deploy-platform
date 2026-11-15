# 参考项目与采用范围

查阅日期：2026-11-15。

| 项目 | 适合参考的部分 | 本项目的取舍 |
| --- | --- | --- |
| [Coolify](https://github.com/coollabsio/coolify) | 自托管应用管理、Git 来源、Compose 部署 | 参考代码更新与构建/部署分离，不重新实现容器编排器 |
| [Dokploy](https://github.com/Dokploy/dokploy) | 应用与 Compose 管理、部署记录 | 将项目、运行配置、任务记录拆开，复用 Docker/Compose 执行服务操作 |
| [Dokku](https://github.com/dokku/dokku) | Git 驱动部署、进程生命周期 | 拉取代码后必须启动/重启并检查结果，而不是只报告 Git 更新成功 |

这三个都是完整部署平台，不是直接引入的 Python 库。如果需要完整的多服务器管理、代理域名与证书编排，可以直接评估 Coolify 或 Dokploy。当前项目聚焦单台服务器、gh 登录和已有部署接入。

## 已使用的第三方库

- [GitPython](https://gitpython.readthedocs.io/en/stable/tutorial.html)：识别仓库、读取分支/提交/工作区状态、快进合并和安全回退。网络 fetch 仍通过 Git 子进程执行，以便跨平台设置超时，并在临时环境中传入 gh 凭据。
- [Docker SDK for Python](https://docker-py.readthedocs.io/en/stable/containers.html)：读取容器及 Compose 标签。只向页面返回名称、状态和项目路径；不返回容器环境变量。
- [python-on-whales](https://gabrieldemarmiesse.github.io/python-on-whales/sub-commands/compose/)：调用 Docker Compose 构建、重建和等待启动。使用原项目名，不自动 down，不移除卷。
- FastAPI / Pydantic / HTTPX：沿用现有接口、输入验证和 HTTP 请求基础。

Compose 的配置以服务器原文件为准。参考 [Coolify 构建部署说明](https://coolify.io/docs/core/build-deployment-model) 和 [Compose 部署文档](https://coolify.io/docs/applications/builds/docker-compose)，没有复制其源码，也没有把本项目描述成具备其全部功能。
