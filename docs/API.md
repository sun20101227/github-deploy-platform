# 接入文档

别的项目有两种方式接入 Deploy 平台：

1. **被平台部署**：项目按[第 1 节](#1-项目要满足的约定)的约定准备好，在平台上新建或接入，以后推送代码就自动更新。
2. **调用平台接口**：CI、脚本或别的服务通过 HTTP 接口触发部署、查询结果、回滚，见[第 2 节](#2-http-接口)起。

两种方式可以一起用。例如 GitHub Actions 跑完测试后调接口部署，并等待部署结果。

下文的 `https://deploy.example.com` 换成你的平台地址，`$DEPLOY_TOKEN` 换成服务器上 `.env` 里的 `DEPLOY_ADMIN_TOKEN`。

---

## 1. 项目要满足的约定

### 1.1 选哪种接入方式

| 你的项目现在是 | 在平台上怎么做 | 平台更新时做什么 |
|---|---|---|
| 还没部署，代码在 GitHub | **新建项目** | 克隆到 `部署目录/releases/<任务号>/`，构建，切换 `current`，重启，健康检查；失败自动恢复上一版 |
| 已部署，目录是 `git clone` 出来的 | **已有项目 → 接入**（Git 仓库） | 在原目录 `fetch` + 快进合并，构建，重启，健康检查；失败自动回到旧提交 |
| 已部署，目录不是 Git 仓库（拷贝的文件、只用镜像的 Compose、自带更新器） | **已有项目 → 接入**（普通目录 / 版本目录），指定对应的 GitHub 仓库 | 不改动目录里的文件，只执行构建命令和重启（或项目自己的更新命令），然后健康检查；不能回滚 |

### 1.2 重启方式

每次部署、自动更新、回滚之后，平台都会重启服务。重启方式有三种：

- **Docker Compose**：平台执行 `docker compose -p <项目名> up -d --build --force-recreate --wait`。项目名每次都一样，不会另建一套容器和数据卷。新建项目时，项目名留空就用仓库名，Compose 文件留空会自动找仓库根目录的 `compose.yaml`、`compose.yml`、`docker-compose.yml`、`docker-compose.yaml`。
- **systemd**：平台执行 `systemctl restart <服务>`，再用 `systemctl is-active` 确认服务在运行。服务的 `WorkingDirectory` 必须在项目目录里。
- **自定义命令**：在项目目录（新建项目是新版本目录）里用 `sh -c` 执行你填的命令，例如 `pm2 restart my-app --update-env` 或 `systemctl start my-app-updater`。

自定义命令有这些要求：

- **必须自己退出**：命令要等重启或更新完成后再退出，不能直接把服务跑在前台（例如不能写 `node server.js`）。最长等 30 分钟。
- **退出码决定成败**：退出码不是 0 就算失败，平台会把输出的最后一段写进部署记录。
- **两个环境变量**：命令能读到 `DEPLOY_RELEASE_DIR`（本次版本目录；接入的项目就是原目录）和 `DEPLOY_PROJECT_ROOT`（项目目录）。

### 1.3 健康检查

建议给服务加一个健康检查地址，例如 `GET /health`。

- **判定规则**：平台每秒请求一次，返回 2xx 才算通过。不跟随重定向，单次请求超时 2 秒。
- **等待时间**：默认最长等 45 秒，可以在项目设置里改成 5–600 秒。启动慢的服务（要迁移数据库、预热缓存）请调大。
- **失败后果**：超时仍未通过就判定部署失败。新建项目和 Git 接入的项目会自动恢复上一版本。
- **不配置也可以**：不配置健康检查时，只看重启有没有成功。

健康检查要真的检查服务能不能用，例如能连上数据库，而不只是进程在。

### 1.4 配置与数据（新建项目）

每个版本目录都是一次全新克隆，不包含被 Git 忽略的文件。

- **`.env` 放在部署目录**：放在 `部署目录/.env`，不要放在版本目录里。用 Compose 时，平台会自动把它传给 Compose。用自定义命令时，请在命令里自己引用 `$DEPLOY_PROJECT_ROOT/.env`。
- **持久数据放在版本目录之外**：上传文件、SQLite 数据库等，用 Compose 命名卷，或版本目录之外的绝对路径。用相对路径挂载（如 `./data:/app/data`）的话，每次部署都会换成一个空目录。
- **数据库迁移要能兼容旧代码**：回滚只换代码，不会回退数据库。

### 1.5 自动更新（GitHub Webhook）

在项目的“设置 → 自动更新”里复制 Payload URL 和 Secret，然后到 GitHub 仓库的 Settings → Webhooks 添加：

| 字段 | 值 |
|---|---|
| Payload URL | `https://deploy.example.com/webhooks/github/<项目 ID>` |
| Content type | `application/json` |
| Secret | 项目设置里的 Secret |
| 事件 | Just the push event |

平台只处理同时满足以下条件的推送，其余一律忽略：签名正确、仓库名一致、分支是项目配置的分支、不是删除分支。部署进行中收到的推送不会丢：当前任务结束后会自动再更新一次，期间的多次推送合并成一次。

---

## 2. HTTP 接口

### 2.1 认证

脚本和 CI 调用 `/api/` 开头的接口时，带上管理密钥：

```http
Authorization: Bearer <DEPLOY_ADMIN_TOKEN>
```

- 密钥错误或缺失时返回 `401`。
- 服务器没有设置 `DEPLOY_ADMIN_TOKEN` 时，脚本无法调用接口，只有登录后的控制台能用。
- `/health` 和 `/webhooks/github/<项目 ID>` 不需要管理密钥。Webhook 用签名认证，见 [2.6](#26-webhook)。
- 网页控制台用管理员账户登录，靠会话 Cookie 认证，见 [README 的“登录”](../README.md#登录)。用 Cookie 认证的改动类请求（POST、PUT）还必须带 `X-Deploy-Request: 1` 头，否则返回 `403`。用 Bearer 密钥的脚本不需要这个头。

管理密钥能做所有操作，包括读取各项目的 Webhook Secret。只把它放在 CI 的加密变量（Secrets）里，不要写进代码或日志。

### 2.2 通用约定

- **请求和响应格式**：请求体和响应都是 JSON，请求要带 `Content-Type: application/json`。
- **时间格式**：时间是 UTC 的 ISO 8601 字符串，例如 `2026-11-18T08:30:00.123456+00:00`。还没结束的任务，`finished_at` 是空字符串 `""`。
- **出错时的响应**：返回 `{"detail": "原因"}`。`detail` 是给人看的中文说明；输入校验失败（`422`）时，`detail` 是一个错误列表。

| 状态码 | 含义 |
|---|---|
| 200 | 成功 |
| 400 | 请求内容不对，例如缺少重启命令、目录不存在 |
| 401 | 管理密钥错误、未登录，或 Webhook 签名无效 |
| 403 | 用 Cookie 登录的改动类请求缺少 `X-Deploy-Request` 头 |
| 404 | 项目或账号不存在 |
| 409 | 当前状态不允许，例如已有部署在进行、平台正在更新、没有可回滚的版本、gh 未登录 |
| 422 | 字段格式不对，例如分支名或仓库名不合法 |
| 502 | 平台访问 GitHub 失败 |

### 2.3 部署任务

部署是异步的：触发接口立刻返回任务号，部署在后台进行。需要结果的话，按 [2.4](#24-查询部署结果) 轮询。

#### 触发部署

```http
POST /api/projects/{project_id}/deploy
```

按项目配置的分支部署最新提交。请求体可以为空。

```json
{"deployment_id": 42, "status": "queued"}
```

同一项目已有部署在进行时返回 `409`，`detail` 为“已有部署正在进行”。平台自身正在更新时也返回 `409`。用 Webhook 触发时不会被拒绝，而是排队补跑。

`POST /api/projects/{project_id}/update` 是同一个操作。

#### 回滚

```http
POST /api/projects/{project_id}/rollback
```

切回上一次成功部署之前的版本，并重启服务。返回格式和触发部署相同。

没有可回滚的版本时返回 `409`。项目目录不是 Git 仓库（`mode` 为 `external`）时也不能回滚。

#### 检查有没有新提交

```http
GET /api/projects/{project_id}/check
```

```json
{
  "sha": "fe99e0d3c1...",
  "message": "Fix login redirect",
  "date": "2026-11-18T03:10:00Z",
  "current_sha": "a1b2c3d4e5...",
  "available": true
}
```

- `sha`、`message`、`date` 是 GitHub 上该分支的最新提交。
- `current_sha` 是最近一次部署成功的提交。还没部署成功过，或目录不是 Git 仓库（看不出正在运行哪个提交）时，这里是空字符串。
- `available` 表示两者不同，即有新提交可以部署。

#### 只重启服务

```http
POST /api/projects/{project_id}/restart
```

按项目的重启方式重启正在运行的版本，不拉代码、不构建，然后做健康检查。返回格式和触发部署相同，部署记录里的 `action` 为 `restart`。这条记录沿用当前版本的提交和回滚信息，所以“检查有没有新提交”和回滚都不受影响。还没部署成功过的新建项目会失败。

### 2.4 查询部署结果

```http
GET /api/projects/{project_id}/deployments?limit=50
```

返回该项目的部署记录，最新的在前。`limit` 取 1–200，默认 50。查最新一条用 `limit=1`。

```json
[
  {
    "id": 42,
    "project_id": 3,
    "action": "deploy",
    "status": "succeeded",
    "commit_sha": "fe99e0d3c1...",
    "release_dir": "/opt/apps/my-app/releases/42",
    "previous_release": "/opt/apps/my-app/releases/40",
    "previous_commit": "",
    "message": "拉取代码\n执行构建\n...\n切换版本，启动服务\n健康检查通过\n",
    "started_at": "2026-11-18T08:30:00.123456+00:00",
    "finished_at": "2026-11-18T08:31:12.456789+00:00"
  }
]
```

| 字段 | 说明 |
|---|---|
| `action` | `deploy` 手动或接口触发，`webhook` 推送触发，`rollback` 回滚，`restart` 只重启，`import` 接入时的登记记录 |
| `status` | 见下表 |
| `commit_sha` | 这次部署的提交；目录不是 Git 仓库的项目为空 |
| `release_dir` | 这次运行代码的目录 |
| `previous_release` / `previous_commit` | 回滚会用到的上一版本，前者用于新建项目，后者用于 Git 接入的项目 |
| `message` | 执行过程：平台写的步骤行、构建和重启命令的输出、失败原因。命令运行时每半秒左右追加一次，进行中轮询就能看到实时输出；最多保留最后 60000 个字符。只能给人看，不要解析它判断结果 |

| `status` | 是否结束 | 含义 |
|---|---|---|
| `queued` | 否 | 已排队，马上开始 |
| `running` | 否 | 正在拉代码、构建或重启 |
| `succeeded` | 是 | 成功，服务已重启，健康检查通过 |
| `failed` | 是 | 失败，服务没有切到新版本；平台重启打断的任务也是这个状态 |
| `rolled_back` | 是 | 新版本失败，已经自动恢复上一版本，服务仍在运行 |
| `rollback_failed` | 是 | 新版本失败，恢复旧版本也失败了，**需要人工处理** |

判断成败只看 `status`：`succeeded` 算成功，其余结束状态都算失败。

### 2.5 项目

#### 列出项目

```http
GET /api/projects
```

返回所有项目，结构同下面的“项目详情”。

#### 项目详情

```http
GET /api/projects/{project_id}
```

```json
{
  "id": 3,
  "account_id": 1,
  "github_login": "sun20101227",
  "repository": "sun20101227/my-app",
  "branch": "main",
  "deploy_root": "/opt/apps/my-app",
  "mode": "release",
  "runtime": "compose",
  "runtime_config": "{\"project\": \"my-app\", \"files\": []}",
  "build_command": "",
  "start_command": "",
  "healthcheck_url": "http://127.0.0.1:3000/health",
  "healthcheck_timeout": 45,
  "webhook_secret": "…",
  "created_at": "…",
  "updated_at": "…"
}
```

| 字段 | 说明 |
|---|---|
| `mode` | `release` 新建项目，`existing` 接入的 Git 仓库，`external` 接入的非 Git 目录 |
| `runtime` | `compose`、`systemd` 或 `command` |
| `runtime_config` | **JSON 字符串**，需要再解析一次。Compose 为 `{"project": "...", "files": [...]}`，systemd 为 `{"unit": "xxx.service"}`，自定义命令为 `{}` |

#### 新建项目

```http
POST /api/projects
```

```json
{
  "account_id": 1,
  "repository": "sun20101227/my-app",
  "branch": "main",
  "deploy_root": "/opt/apps/my-app",
  "runtime": "compose",
  "runtime_config": {"project": "my-app", "files": []},
  "build_command": "",
  "start_command": "",
  "healthcheck_url": "http://127.0.0.1:3000/health",
  "healthcheck_timeout": 60
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `account_id` | 是 | 来自 `GET /api/accounts` |
| `repository` | 是 | `owner/repo` |
| `branch` | 否 | 默认 `main` |
| `deploy_root` | 是 | 服务器上的绝对路径；不能和已有项目重叠，不能已经是 Git 仓库 |
| `runtime` | 否 | 新建项目只支持 `compose` 或 `command`，默认 `command` |
| `runtime_config` | 否 | Compose 时可填 `project`（小写字母、数字、`-`、`_`）和 `files`（仓库内的相对路径） |
| `start_command` | `command` 时必填 | 见 [1.2](#12-重启方式) |
| `build_command` | 否 | 在新版本目录执行，最长 30 分钟 |
| `healthcheck_url` / `healthcheck_timeout` | 否 | 见 [1.3](#13-健康检查)，等待时间 5–600 秒，默认 45 |
| `webhook_secret` | 否 | 不填会自动生成 |

```json
{"id": 3, "webhook_secret": "…"}
```

新建只登记配置，不会部署。之后调用 [触发部署](#触发部署) 做首次部署。

#### 修改项目

```http
PUT /api/projects/{project_id}
```

请求体字段同“新建项目”。建议先 `GET` 项目详情，修改需要改的字段后整份发回。`runtime_config` 传对象或原样发回 `GET` 得到的字符串都可以。

能改的只有 `branch`、`build_command`、`start_command`、`healthcheck_url`、`healthcheck_timeout`、`runtime`、`runtime_config`；`repository` 和 `deploy_root` 会被忽略。有部署在进行时返回 `409`。

#### 接入服务器上已部署的项目

先查服务器上有哪些应用：

```http
GET /api/discovery/running
```

```json
{
  "projects": [
    {
      "name": "sub2api",
      "path": "/opt/sub2api",
      "kind": "plain",
      "repository": "",
      "branch": "",
      "commit": "",
      "dirty": false,
      "note": "不是 Git 仓库。接入后平台不改动这里的文件，更新时执行构建和重启。",
      "compose_files": ["/opt/sub2api/docker-compose.yml"],
      "containers": [{"name": "sub2api", "status": "running", "project": "sub2api", "path": "/opt/sub2api", "files": ["/opt/sub2api/docker-compose.yml"]}],
      "services": [],
      "imported": false
    }
  ],
  "containers": [],
  "warnings": []
}
```

- **`kind`**：`git` 为 Git 工作目录，`releases` 为带 `releases/` 和 `current` 的版本目录，`plain` 为普通目录。
- **扫描指定目录**：用 `POST /api/discovery/scan`，请求体为 `{"root": "/opt", "depth": 3}`，`depth` 取 0–4。返回结构与上面相同。

然后接入：

```http
POST /api/projects/import
```

```json
{
  "path": "/opt/sub2api",
  "account_id": 1,
  "repository": "Wei-Shaw/sub2api",
  "branch": "main",
  "runtime": "compose",
  "runtime_config": {"project": "sub2api", "files": ["docker-compose.yml"]},
  "build_command": "",
  "start_command": "",
  "healthcheck_url": "",
  "healthcheck_timeout": 45
}
```

| 字段 | 说明 |
|---|---|
| `path` | 用发现结果里的 `path` |
| `repository` / `branch` | 只有目录不是 Git 仓库时才需要，Git 仓库会从 `origin` 和当前分支读取 |
| `runtime` | 接入时支持 `compose`、`systemd`、`command` 三种 |
| `runtime_config` | Compose 时 `project` 必须是原来的项目名，`files` 必须在项目目录里；systemd 时为 `{"unit": "xxx.service"}` |

返回 `{"id": 7}`。接入只登记，不会改代码，也不会重启服务。

#### 服务状态与服务日志

```http
GET /api/projects/{project_id}/status
```

```json
{
  "state": "running",
  "detail": "",
  "health": true,
  "items": [
    {"name": "sub2api", "state": "running", "health": "healthy", "since": "2026-11-14T07:07:59.1Z", "restarts": 0}
  ]
}
```

- **`state`**：`running` 全部在运行，`degraded` 部分在运行或健康检查没通过，`stopped` 都没在运行，`unknown` 读不到。
- **`items`**：Compose 项目每个容器一项，systemd 是那个服务一项；自定义命令的项目为空，只能靠健康检查判断。
- **`health`**：配置了健康检查时是这次检查的结果，没配置时为 `null`。

`GET /api/projects/status` 一次返回所有项目，格式为 `{"<项目 ID>": {"state": "...", "detail": "..."}}`。

```http
GET /api/projects/{project_id}/logs?lines=200
```

返回服务自己的输出（不是部署记录）：`{"source": "journalctl -u xxx.service", "text": "..."}`。`lines` 取 20–2000。Compose 项目每个容器的日志前面有 `== 容器名 ==`。自定义命令的项目看不到服务日志，返回 `400`。

### 2.6 Webhook

```http
POST /webhooks/github/{project_id}
X-GitHub-Event: push
X-Hub-Signature-256: sha256=<HMAC-SHA256(webhook_secret, 原始请求体) 的十六进制>
```

一般由 GitHub 直接调用，见 [1.5](#15-自动更新github-webhook)。别的系统也可以按 GitHub 的格式自己调用，签名要对原始请求体计算。请求体至少要有：

```json
{"ref": "refs/heads/main", "repository": {"full_name": "sun20101227/my-app"}}
```

| 响应 | 含义 |
|---|---|
| `{"ok": true, "deployment_id": 42}` | 已开始部署 |
| `{"ok": true, "queued": true}` | 已有部署在进行，结束后会再更新一次 |
| `{"ok": true, "ignored": true}` | 不是 push 事件、仓库不一致或删除分支，已忽略 |
| `{"ok": true, "ignored": true, "reason": "branch"}` | 不是项目配置的分支，已忽略 |
| `401` | 签名无效 |
| `404` | 项目不存在 |

### 2.7 其他

| 接口 | 用途 |
|---|---|
| `GET /health` | 平台存活检查，不需要密钥。返回 `{"ok": true, "instance": "…"}`；`instance` 每次平台重启都会变 |
| `GET /api/accounts` | 已连接的 GitHub 账号。服务器上的 `gh` 已登录时会自动连接 |
| `GET /api/accounts/{id}/repositories` | 该账号能访问的仓库，最多 1000 个 |
| `GET /api/github/status` | 服务器上 `gh` 的登录状态：`{"connected": true, "github_id": "…", "login": "…"}` 或 `{"connected": false, "message": "原因"}` |

---

## 3. 示例

### 3.1 命令行：部署并等待结果

```bash
#!/usr/bin/env bash
# 用法：DEPLOY_URL=https://deploy.example.com DEPLOY_TOKEN=... ./deploy.sh 3
set -euo pipefail
project="$1"
auth=(-H "Authorization: Bearer $DEPLOY_TOKEN")

job=$(curl -fsS -X POST "${auth[@]}" "$DEPLOY_URL/api/projects/$project/deploy" | jq -r .deployment_id)
echo "部署任务 #$job"

for _ in $(seq 1 180); do            # 最多等 15 分钟
  sleep 5
  record=$(curl -fsS "${auth[@]}" "$DEPLOY_URL/api/projects/$project/deployments?limit=20" \
    | jq --argjson id "$job" '.[] | select(.id == $id)')
  status=$(jq -r .status <<<"$record")
  case "$status" in
    queued|running) continue ;;
    succeeded) echo "部署成功：$(jq -r .commit_sha <<<"$record")"; exit 0 ;;
    *) echo "部署结束，状态 $status："; jq -r .message <<<"$record"; exit 1 ;;
  esac
done
echo "等待超时，任务仍在进行，请到控制台查看"; exit 1
```

查询时按任务号取记录，不要只看最新一条：等待期间别人的推送可能已经排进了新任务。

### 3.2 GitHub Actions：测试通过后部署

在仓库的 Settings → Secrets and variables → Actions 里添加 `DEPLOY_URL`、`DEPLOY_TOKEN` 和 `DEPLOY_PROJECT_ID`。

```yaml
name: deploy
on:
  push:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: make test            # 换成项目自己的测试命令
  deploy:
    needs: test
    runs-on: ubuntu-latest
    concurrency: deploy-production   # 同一时间只跑一个部署
    steps:
      - name: 触发部署并等待结果
        env:
          DEPLOY_URL: ${{ secrets.DEPLOY_URL }}
          DEPLOY_TOKEN: ${{ secrets.DEPLOY_TOKEN }}
          PROJECT: ${{ secrets.DEPLOY_PROJECT_ID }}
        run: |
          job=$(curl -fsS -X POST -H "Authorization: Bearer $DEPLOY_TOKEN" \
            "$DEPLOY_URL/api/projects/$PROJECT/deploy" | jq -r .deployment_id)
          for i in $(seq 1 180); do
            sleep 5
            status=$(curl -fsS -H "Authorization: Bearer $DEPLOY_TOKEN" \
              "$DEPLOY_URL/api/projects/$PROJECT/deployments?limit=20" \
              | jq -r --argjson id "$job" '.[] | select(.id == $id) | .status')
            echo "#$job $status"
            case "$status" in
              queued|running) ;;
              succeeded) exit 0 ;;
              *) exit 1 ;;
            esac
          done
          exit 1
```

用 Actions 部署时，不要在这个项目上同时配置 GitHub Webhook，否则每次推送会部署两次，而且会跳过测试。

### 3.3 Python

```python
import time
import httpx

class Deploy:
    def __init__(self, url: str, token: str):
        self.http = httpx.Client(base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=30)

    def _call(self, method: str, path: str, **kw):
        r = self.http.request(method, path, **kw)
        if r.is_error:
            raise RuntimeError(f"{r.status_code}: {r.json().get('detail', r.text)}")
        return r.json()

    def deploy(self, project_id: int, wait: float = 900) -> dict:
        job = self._call("POST", f"/api/projects/{project_id}/deploy")["deployment_id"]
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            time.sleep(5)
            records = self._call("GET", f"/api/projects/{project_id}/deployments", params={"limit": 20})
            record = next(r for r in records if r["id"] == job)
            if record["status"] not in ("queued", "running"):
                return record
        raise TimeoutError(f"部署 #{job} 还在进行")

d = Deploy("https://deploy.example.com", "DEPLOY_ADMIN_TOKEN 的值")
result = d.deploy(3)
print(result["status"], result["commit_sha"])
```

### 3.4 自己发 Webhook（不经过 GitHub）

```python
import hashlib, hmac, json, httpx

secret = "项目设置里的 Webhook Secret"
body = json.dumps({"ref": "refs/heads/main", "repository": {"full_name": "sun20101227/my-app"}}).encode()
signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
r = httpx.post("https://deploy.example.com/webhooks/github/3", content=body, headers={
    "Content-Type": "application/json", "X-GitHub-Event": "push", "X-Hub-Signature-256": signature})
print(r.json())
```

这种方式只需要单个项目的 Webhook Secret，不需要管理密钥。适合只允许触发部署、不允许做别的操作的场景。

---

## 4. 限制

- **同一项目串行**：同一项目的部署和回滚一次只跑一个；不同项目可以同时部署。
- **平台重启会中断任务**：被中断的任务在平台启动时标记为 `failed`。服务可能停在中间状态，请检查后重新部署。
- **补跑标记只在内存里**：部署中收到推送后要补跑的标记，平台重启后会丢失，需要再推送一次或手动部署。
- **没有接口版本号**：接口和字段有变化时，会同步更新这份文档。
