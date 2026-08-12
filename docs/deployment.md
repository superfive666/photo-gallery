# 部署 Runbook（两台内网机器）

从裸 Ubuntu 走到上线。命令按顺序执行即可，每一节末尾都有一条「确认」命令 ——
没通过就不要往下走。

## 拓扑

```
                    GitHub (superfive666/photo-gallery, private)
                       │  workflow 调度，靠 runner 标签选机器
        ┌──────────────┴───────────────┐
        │ zrc-ci                       │ zrc-prod
        ▼                              ▼
┌─────────────────────┐        ┌──────────────────────────────┐
│  192.168.0.15       │        │  192.168.0.12                │
│  无 GPU             │        │  NVIDIA GPU                  │
│                     │        │                              │
│  runner (build)     │        │  runner (deploy / ingest)    │
│  docker login 读写  │        │  docker login 只读           │
│                     │        │  compose: db api web         │
│  ❌ 无 .env         │        │           embedding(GPU)     │
│  ❌ 无数据库        │        │  pgdata 卷 / .env / 反向代理 │
└──────────┬──────────┘        └──────────┬───────────────────┘
           │ push                  pull   │
           └────────▶ Docker Hub ◀────────┘
                  superfive666/photo-gallery-{api,jobs,web,embedding}
                                              │ 443
                                       faces.zrc.sg（公网）
```

两条纪律，决定了后面所有配置：

1. **`.15` 只构建，永不接触 `.env` 和数据库。** 它是隔离边界，不是「什么都在上面跑」。
2. **`.12` 只 `pull → migrate → up`，永不构建。** GPU 机的磁盘和 CPU 留给推理。

为什么这样分、为什么不用 SSH 触发部署：见
[`plans/0004-two-host-cicd.md`](plans/0004-two-host-cicd.md#决策与理由)。

---

## 0. 先准备好这些信息

| 项 | 值 | 说明 |
| --- | --- | --- |
| 域名 | 如 `faces.zrc.sg` | 需要能解析到家宽公网 IP（DDNS 也可） |
| 端口转发 | 80、443 → `192.168.0.12` | 路由器上做；**不要**转发 5432 / 8080 |
| Docker Hub 账户 | 如 `superfive666` | 见下面「⚠️ 私有仓库数量」 |
| 邀请码 | 自己定 | 明文不进任何文件，只存 argon2 hash |
| 磁盘 | 两台各留 ≥ 100GB | GPU 版 embedding 镜像是 GB 级的 |

### ⚠️ 私有仓库数量：先确认你的 Docker Hub 套餐

四个服务 = 四个仓库（Docker Hub 的仓库名只有 `<用户>/<仓库>` 两级，不能像 GHCR 那样
再用斜杠分层）：

```
superfive666/photo-gallery-api
superfive666/photo-gallery-jobs
superfive666/photo-gallery-web
superfive666/photo-gallery-embedding
```

**Docker Hub 免费版（Personal）只包含 1 个私有仓库**，四个私有仓库需要 Docker Pro。
如果不想升级，两条替代路线：

- **四个服务合并到一个私有仓库，用 tag 前缀区分**
  （`superfive666/photo-gallery:api-sha-abc1234`）。需要改 `docker-compose.yml` 里的
  image 模板 —— 说一声我改。
- **回内网 registry**（`.15` 上跑一个 `registry:2`）。只改 `IMAGE_PREFIX` 一个变量，
  workflow 不用动。

镜像里**不含任何 secret**（密码、JWT、邀请码 hash 全在 `.12` 的 `.env` 里），
所以「仓库设成私有」保护的是代码，不是凭据。

---

## 1. 构建机 192.168.0.15

### 1.1 Docker

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl jq
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
                        docker-buildx-plugin docker-compose-plugin
```

给容器日志加上限 —— 默认的 json-file **没有上限**，`embedding` 这种长跑服务几个月
就能把根分区写满。`/etc/docker/daemon.json`（**两台机器都要**）：

```json
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "50m", "max-file": "5" }
}
```

```bash
sudo systemctl restart docker
```

确认：`docker compose version` 输出 v2.x。**不要**用 apt 里的 `docker.io` +
`docker-compose`（v1，语法不兼容本项目的 compose 文件）。

### 1.2 GitHub Actions runner

两台机器的步骤完全一样，**只有 `--name` 和 `--labels` 不同**：

| 机器 | `--name` | `--labels` |
| --- | --- | --- |
| 192.168.0.15 | `zrc-ci-15` | `zrc-ci` |
| 192.168.0.12 | `zrc-prod-12` | `zrc-prod` |

`self-hosted`、`linux`、`x64` 三个标签是 GitHub 自动加的，只需额外给上面那一个。
workflow 里写的是 `runs-on: [self-hosted, linux, x64, zrc-ci]` —— 标签是**与**的关系，
所以标签打错会表现为「workflow 一直 Queued」，不会报错。

**① 建专用账户**

```bash
sudo useradd -m -s /bin/bash ghrunner
sudo usermod -aG docker ghrunner
```

> ⚠️ `docker` 组等价于 root（能把宿主机根目录挂进容器）。这正是「这两台 runner 只执行
> 我们自己的构建/部署步骤，PR 的 lint/test 继续跑在 GitHub 托管 runner 上」这条纪律的
> 原因（CLAUDE.md 约束 9）。

**② 拿注册 token**

GitHub → 仓库 → **Settings → Actions → Runners → New self-hosted runner**，
选 **Linux / x64**。页面上 `./config.sh --token` 后面那一串就是注册 token，
**1 小时内有效**。两台机器各点一次、各拿一个。

**③ 下载并解压**（以 `ghrunner` 身份）

```bash
sudo -iu ghrunner
mkdir -p ~/actions-runner && cd ~/actions-runner

# 版本号从 GitHub API 取，免得抄一个过期的
V=$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest \
     | sed -n 's/.*"tag_name": *"v\([^"]*\)".*/\1/p')
curl -fsSL -o runner.tar.gz \
  "https://github.com/actions/runner/releases/download/v${V}/actions-runner-linux-x64-${V}.tar.gz"
tar xzf runner.tar.gz && rm runner.tar.gz
exit
```

**④ 装系统依赖**（Ubuntu Server 最小安装缺这些库，装了 runner 才能起）

```bash
sudo /home/ghrunner/actions-runner/bin/installdependencies.sh
```

**⑤ 注册**（以 `ghrunner` 身份，`.15` 的例子）

```bash
sudo -iu ghrunner
cd ~/actions-runner
./config.sh --url https://github.com/superfive666/photo-gallery \
            --token <第②步的 token> \
            --name zrc-ci-15 \
            --labels zrc-ci \
            --work _work \
            --unattended --replace
exit
```

`--unattended` 跳过交互提问，`--replace` 让同名 runner 可以重新注册（重装时不用先去
网页上删）。**不要**加 `--ephemeral`：那会让每个 job 后清空机器，本地 docker 层缓存就
没了 —— 而层缓存正是「改业务代码时构建只要几十秒」的原因。

**⑥ 装成 systemd 服务，开机自启**

```bash
sudo /home/ghrunner/actions-runner/svc.sh install ghrunner
sudo /home/ghrunner/actions-runner/svc.sh start
sudo /home/ghrunner/actions-runner/svc.sh status
```

runner 会自动升级自己，日常不需要维护。

确认：GitHub → Settings → Runners 里看到 `zrc-ci-15` 显示 **Idle**，标签含 `zrc-ci`。

> 要换标签或重装：`sudo -iu ghrunner; cd ~/actions-runner; ./config.sh remove --token
> <网页上的 remove token>`，然后从第⑤步重来。

### 1.3 Docker Hub 凭据（读写）

先在 Docker Hub 建一个 **Personal access token**：
Account settings → Personal access tokens → Generate new token，
描述写 `zrc-ci-15`，权限 **Read & Write**。

> 用 token 而不是账户密码：token 可以单独撤销、可以限权限，泄漏的后果小一个量级。

凭据存在机器上、不进 GitHub Secrets。**必须以 runner 的账户身份登录** ——
凭据是按系统用户存的，用 root 或你自己的账户登录，runner 是看不到的：

```bash
sudo -iu ghrunner
echo '<token>' | docker login -u superfive666 --password-stdin
chmod 700 ~/.docker && chmod 600 ~/.docker/config.json
exit
```

⚠️ `~/.docker/config.json` 里的凭据是 **base64，不是加密**。所以：token 而不是密码、
`.12` 上用只读 token、文件权限收紧。

确认：`sudo -u ghrunner docker pull superfive666/photo-gallery-api:latest`
（首次还没有镜像，报 `not found` 就说明鉴权是通的；报 `unauthorized` 才是没登录）。

### 1.4 防火墙

```bash
sudo ufw allow from 192.168.0.0/24 to any port 22 proto tcp
sudo ufw --force enable
```

这台机器不需要对外开任何端口 —— 它只主动出网（GitHub、Docker Hub、pypi/npm）。

---

## 2. 生产机 192.168.0.12

### 2.1 Docker + daemon.json

同 1.1。

### 2.2 NVIDIA 驱动与容器运行时

```bash
sudo ubuntu-drivers install          # 或 apt-get install -y nvidia-driver-<版本>-server
sudo reboot
```

重启后确认：`nvidia-smi` 能列出显卡。

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

确认（**这一步必须通过，否则后面只是静默退回 CPU 推理**）：

```bash
docker run --rm --gpus all ubuntu:24.04 nvidia-smi
```

### 2.3 Runner

与 1.2 **完全一样**，只把最后的注册参数换成：

```bash
./config.sh --url https://github.com/superfive666/photo-gallery \
            --token <这台机器新拿的 token> \
            --name zrc-prod-12 \
            --labels zrc-prod \
            --work _work \
            --unattended --replace
```

### 2.4 Docker Hub 凭据（只读）

在 Docker Hub 再建一个 token，描述 `zrc-prod-12`，权限 **Read only** ——
这台机器只需要拉。

```bash
sudo -iu ghrunner
echo '<只读 token>' | docker login -u superfive666 --password-stdin
chmod 700 ~/.docker && chmod 600 ~/.docker/config.json
exit
```

给生产机一个只读 token 而不是复用读写的那个：这台机器暴露在公网服务后面，
万一被拿到凭据，攻击者也改不了镜像 —— 改镜像等于下次部署时在生产机上执行任意代码。

### 2.5 生产状态目录

```bash
sudo mkdir -p /opt/photo-gallery/data/{sample-albums,eval}
sudo mkdir -p /srv/backups
sudo chown -R ghrunner:ghrunner /opt/photo-gallery /srv/backups
```

目录里最终是这些东西：

| 路径 | 谁写的 | 说明 |
| --- | --- | --- |
| `.env` | **人手动放，只放一次** | 600 权限，永不进 git，永不经过 GitHub |
| `docker-compose.yml` / `docker-compose.gpu.yml` | deploy workflow 同步 | |
| `docs/schema/*.sql` | deploy workflow 同步 | `jobs` 容器挂载它做迁移 |
| `.deployed-tag` | deploy workflow 写 | 上一次健康检查通过的镜像 tag，回滚就靠它 |

> ⚠️ **绝不要把 `.env` 放在 runner 的工作区里**（`~ghrunner/_work/...`）。
> `actions/checkout` 会执行 `git clean -ffdx`，连 gitignore 的文件一起删 ——
> 第二次部署时 `.env` 就没了，服务会带着默认密码重启。这就是要有 `/opt/photo-gallery`
> 这个目录的全部原因。

### 2.6 写 `.env`

以 `ghrunner` 身份，用仓库里的 `.env.example` 打底：

```bash
sudo -iu ghrunner
cd /opt/photo-gallery
# 从仓库拷一份 .env.example 过来（scp 或直接复制内容）
cp .env.example .env
chmod 600 .env
```

生产机上**必须**与示例不同的几项：

```dotenv
# 密码/密钥：用 openssl rand -base64 36 生成，别复用
POSTGRES_PASSWORD=<随机>
DATABASE_URL=postgresql+asyncpg://gallery:<同上>@db:5432/photo_gallery
JWT_SECRET=<随机>
AUDIT_HASH_SALT=<随机>
INVITE_CODE_HASH=<见下>

# 这台机器有 GPU：叠加 GPU 层 + 打开运行时开关
COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml
EMBEDDING_USE_GPU=true
# 推理在 GPU 上，CPU 线程只用于解码/resize
ORT_NUM_THREADS=4
# 显存够就往上调，这是一次识别前向送多少张脸
REC_BATCH_SIZE=128

# 镜像从 Docker Hub 私有仓库拉
IMAGE_PREFIX=superfive666/photo-gallery

# 前面有宿主机反向代理，端口只对本机开
WEB_BIND=127.0.0.1
WEB_PORT=8080

# jobs 容器的挂载点要真实存在，否则 docker 会建出 root 属主的空目录
SOURCE_LOCAL_DIR=/opt/photo-gallery/data/sample-albums
EVAL_DIR=/opt/photo-gallery/data/eval
```

`INVITE_CODE_HASH` 在**任意**一台装了本项目依赖的机器上生成，只把 hash 抄过来：

```bash
uv run python -m api.app.tools.hash_invite
```

`COMPOSE_FILE` 写在 `.env` 里而不是每次加 `-f`：这样 workflow、`make up`、
手敲的 `docker compose ps` 全都自动带上 GPU 叠加层，不会出现「手动起的没挂 GPU」
这种只在事后从日志里才发现的偏差。

确认：`stat -c %a /opt/photo-gallery/.env` 输出 `600`。

### 2.7 反向代理与 HTTPS

Caddy 自动签发/续期证书，配置最短：

```bash
sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
sudo apt-get update && sudo apt-get install -y caddy
```

`/etc/caddy/Caddyfile`：

```
faces.zrc.sg {
    encode zstd gzip

    # 人脸检索页面不该被搜索引擎收录，也不该被别人 iframe
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Robots-Tag "noindex, nofollow"
    }

    # 自拍上传：与 api 的 MAX_UPLOAD_BYTES 对齐（3 张 × 10MB + 余量）
    request_body {
        max_size 32MB
    }

    reverse_proxy 127.0.0.1:8080
}
```

```bash
sudo systemctl reload caddy
```

Caddy 默认会带上 `X-Forwarded-For`。容器里的 nginx 用 `real_ip` 还原真实客户端 IP
（见 `web/nginx.conf` 里那段注释）—— **前提是 8080 只绑 127.0.0.1**。
把 8080 暴露出去，任何人都能自己塞一个 `X-Forwarded-For` 绕过 api 的按 IP 限流。

### 2.8 防火墙

```bash
sudo ufw allow from 192.168.0.0/24 to any port 22 proto tcp
sudo ufw allow 80,443/tcp
sudo ufw --force enable
```

5432（Postgres）与 8080（web）都不放行：前者在 compose 内网里只 `expose` 不映射，
后者只绑 127.0.0.1。路由器上也**只**转发 80/443。

---

## 3. GitHub 侧配置

**Settings → Actions → Runners**：两个 runner 都是 Idle，标签分别为
`zrc-ci` / `zrc-prod`。

**Settings → Secrets and variables → Actions → Variables**（非 secret，可留空用默认）：

| Variable | 默认值 | 何时要改 |
| --- | --- | --- |
| `IMAGE_PREFIX` | `superfive666/photo-gallery` | 换 Docker Hub 账户，或换 GHCR / 内网 registry |
| `DEPLOY_DIR` | `/opt/photo-gallery` | 换生产目录 |
| `VITE_API_BASE_URL` | `/api` | api 不再与前端同源时 |

**Secrets：这个项目一个都不需要。** Docker Hub 凭据由两台机器各自 `docker login`
存在本地，生产 `.env` 只在 `.12` 上，部署靠 runner 而不是 SSH 密钥。
少一处存放就少一处泄漏面。
（想改成集中轮换的话，把 token 放 Secrets + 在 workflow 里用 `docker/login-action`
也能跑通 —— 代价是凭据要经过 CI 平台。）

**Settings → Actions → General**：
- Fork pull request workflows → *Require approval for all outside collaborators*
  （仓库现在是 private，属于兜底）。

**分支**：仓库目前**还没有 `main`**，默认分支就是
`claude/face-recognition-photo-gallery-gkjfbx`，所以 `push: main` 那两个触发器现在
永远不会触发。首次上线可以直接用 **Run workflow**（`workflow_dispatch`）手动跑。
正式建 `main` 时按 `docs/cicd.md` 打开分支保护。

---

## 4. 首次上线的顺序

1. 在 Docker Hub 上先把四个仓库建好并设为 **Private**（`docker push` 会自动创建，
   但**自动创建出来的是 public**，先建再推更稳妥）：
   `photo-gallery-api` / `-jobs` / `-web` / `-embedding`。

2. `.15` 上手动验一遍构建（比在 Actions 日志里翻错误快得多）：

   ```bash
   sudo -iu ghrunner
   git clone https://github.com/superfive666/photo-gallery.git && cd photo-gallery
   export IMAGE_PREFIX=superfive666/photo-gallery IMAGE_TAG=manual EMBEDDING_GPU=true
   docker compose --profile tools build
   for s in api jobs web embedding; do docker push "$IMAGE_PREFIX-$s:manual"; done
   ```

   embedding 那一层会下载 300MB 模型权重 + GPU 版 onnxruntime，第一次构建和第一次
   push 都很慢（GB 级）。之后只传变化的层 —— 依赖和权重在下层，改业务代码不动它们。

3. `.12` 上确认能拉到、GPU 能用：

   ```bash
   cd /opt/photo-gallery
   export IMAGE_TAG=manual
   docker compose --profile tools pull
   docker compose up -d db
   docker compose run --rm jobs python -m jobs migrate
   docker compose up -d embedding api web
   docker compose exec embedding python -c \
     "import urllib.request,json;print(json.load(urllib.request.urlopen('http://localhost:8000/healthz')))"
   ```

   要看到 `gpu: True` **且** `batch_supported: True`。
   `gpu: False` → 回到 2.2 与 `.env` 的 `EMBEDDING_USE_GPU`；
   `batch_supported: False` → 识别模型的 ONNX 图 batch 维被固定成 1，批量会退化成
   逐张前向（见 `docker/README.md`）。

4. 记下这个手动版本，让回滚有目标：`echo manual > /opt/photo-gallery/.deployed-tag`

5. 探源站结构 —— **上线前必做**，当前解析器还是通用实现：

   ```bash
   cd /opt/photo-gallery
   IMAGE_TAG=manual docker compose run --rm jobs python -m jobs probe --album 2026-08-10
   ```

   把输出贴回仓库，按 [`data-source.md`](data-source.md) 收敛成精确选择器。
   这一步没做就跑 `ingest`，结果会是「跑完了，一张都没入库」或者只抓到缩略图。

6. 先小规模建库，再上全量：

   ```bash
   IMAGE_TAG=manual docker compose run --rm jobs python -m jobs ingest --album 2026-08-10
   ```

7. 到 Actions 里 **Run workflow** 跑一次 Deploy，走完整链路。
8. 故意验一次回滚：临时把 `api` 的 `/readyz` 改成 500 提交并部署，
   健康检查应在 150 秒后失败并自动回滚到上一个 tag，站点保持可用。
9. 走 `docs/cicd.md` 的部署前检查清单。

---

## 5. 日常运维

### 备份（`.12`，以 `ghrunner` 身份）

`crontab -u ghrunner -e`：

```cron
# 每天 03:30 备份，保留 14 天。cron 里 % 必须转义。
30 3 * * * cd /opt/photo-gallery && docker compose exec -T db pg_dump -U gallery -Fc photo_gallery > /srv/backups/pg-$(date +\%F).dump 2>>/srv/backups/backup.log
0 4 * * 0 find /srv/backups -name 'pg-*.dump' -mtime +14 -delete
```

**演练一次恢复，否则不算有备份**（`docs/cicd.md` 的清单里有这一条）：

```bash
docker compose exec -T db createdb -U gallery restore_test
docker compose exec -T db pg_restore -U gallery -d restore_test < /srv/backups/pg-2026-08-11.dump
docker compose exec -T db psql -U gallery -d restore_test -c 'select count(*) from face;'
docker compose exec -T db dropdb -U gallery restore_test
```

缩略图是 BYTEA，备份体积基本等于 `照片数 × 缩略图大小`，几万张照片是几个 GB 量级。

### 清理 Docker Hub 上的旧 tag

Docker Hub 不会自动回收：每次部署都留一个 `sha-` tag，`embedding` 又是 GB 级的。
免费/Pro 套餐都没有保留策略，得手动删（网页上删也行）：

```bash
JWT=$(curl -s -H "Content-Type: application/json" -X POST \
  -d '{"username":"superfive666","password":"<读写 token>"}' \
  https://hub.docker.com/v2/users/login/ | jq -r .token)

# 看某个仓库的 tag，按时间排序
curl -s -H "Authorization: JWT $JWT" \
  "https://hub.docker.com/v2/repositories/superfive666/photo-gallery-embedding/tags/?page_size=100" \
  | jq -r '.results[] | "\(.last_updated) \(.name)"' | sort

# 删掉某个旧 tag
curl -s -X DELETE -H "Authorization: JWT $JWT" \
  "https://hub.docker.com/v2/repositories/superfive666/photo-gallery-embedding/tags/sha-old1234/"
```

留一周内的 tag 就够：deploy 的回滚只需要上一个版本，更早的重新构建即可。

### 磁盘

```bash
docker system df                 # 两台都看
du -sh /var/lib/docker
```

deploy workflow 每次结束都跑 `docker image prune --filter until=168h`：
保留一周内的镜像，让「换个 tag 重启」这条回滚路径不需要重新拉镜像。

### 看日志

```bash
cd /opt/photo-gallery && docker compose logs -f --tail=100 api
```

日志里**不应该**出现 embedding 数值、文件名或人脸图片 —— 只有 id、耗时、计数、
错误类型（CLAUDE.md 约束 2，`api/tests/test_no_persistence.py` 做源码级守卫）。
如果看到别的，那是 bug，按隐私事故处理。

### 换模型

`MODEL_NAME` / `MODEL_VERSION` 一变，存量 embedding 全部失效。
每条 `face` 都带 `model_name` / `model_version` / `dim`，靠这几个字段识别存量数据
并重算，而不是整库作废（CLAUDE.md 约束 5）。流程：重建 embedding 镜像 → 部署 →
`ingest --full`。

---

## 6. 故障排查

| 现象 | 大概率原因 |
| --- | --- |
| workflow 一直 Queued | 标签打错或 runner 离线：`sudo ~ghrunner/actions-runner/svc.sh status` |
| runner 装完起不来，报缺少 `.so` | 少跑了 `bin/installdependencies.sh`（1.2 第④步） |
| push / pull 报 `unauthorized` | 那台机器没 `docker login`，或 token 被撤销；也可能登录用的是别的系统账户（必须是 `ghrunner`） |
| push 报 `denied: requested access to the resource is denied` | `.15` 上用的是只读 token，或 `IMAGE_PREFIX` 的用户名与登录账户不一致 |
| Docker Hub 上仓库意外变成 public | `docker push` 自动创建的仓库默认 public。先在网页上建好并设 Private（第 4 节第 1 步） |
| `/healthz` 里 `gpu: false` | 镜像不是 `EMBEDDING_GPU=true` 构建的；或 `.env` 少了 `EMBEDDING_USE_GPU=true`；或 `.env` 少了 `COMPOSE_FILE=...gpu.yml`（容器没挂到卡） |
| `batch_supported: false` | 识别模型 ONNX 的 batch 维是固定 1，批量退化为逐张。需要换一份动态 batch 导出 |
| 服务起来了但登录说邀请码错 | `INVITE_CODE_HASH` 抄漏了字符，或生成时用的明文不是分发出去的那个 |
| 第二次部署后连不上数据库 | `.env` 被 `git clean` 删过 —— 确认它在 `/opt/photo-gallery` 而不是 runner 工作区 |
| 一个人搜几次全站就被限流 | `real_ip` 没生效或 8080 被暴露，所有请求的来源 IP 塌成了同一个 |
| `ingest` 跑完 0 张入库 | 相册页解析没收敛，先跑 `jobs probe`（见 [`data-source.md`](data-source.md)） |
| deploy 报「没有上一次成功部署的记录」 | 首次部署时健康检查就失败了，没有回滚目标；看容器日志人工处理 |
| 拉取报 `toomanyrequests` | Docker Hub 的拉取次数限制。确认是**已登录**状态在拉（未登录额度低得多） |

---

## 7. 这套配置**没有**覆盖的

说清楚免得误以为已经有了：

- **没有 staging。** 部署直接上生产，靠健康检查 + 回滚兜底。
- **镜像经公网中转。** `.15` 上传 → Docker Hub → `.12` 下载，GB 级的 embedding 镜像
  第一次会占满家宽上行一段时间。想省掉这一跳就换回内网 registry（改 `IMAGE_PREFIX`）。
- **数据库没有主从、没有 PITR。** 只有每日 `pg_dump`；最坏情况丢一天的建库结果
  （重跑 `ingest` 能重建，因为原图始终在源站）。
- **备份没有异地副本。** 家里失火/被盗就没了。真在意就把 `/srv/backups` 同步到别处。
- **没有监控告警。** 服务挂了要靠人发现或下次 workflow 失败才知道。
- **单机单副本。** 部署期间有几秒不可用。
