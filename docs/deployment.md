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
│  registry:2  :5000  │───────▶│  compose: db api web         │
│                     │  拉镜像 │           embedding(GPU)     │
│  ❌ 无 .env         │  千兆内网│  pgdata 卷 / .env / 反向代理 │
│  ❌ 无数据库        │        │                              │
└─────────────────────┘        └──────────────┬───────────────┘
                                              │ 443
                                       faces.zrc.sg（公网）
```

两条纪律，决定了后面所有配置：

1. **`.15` 只构建，永不接触 `.env` 和数据库。** 它是隔离边界，不是「什么都在上面跑」。
2. **`.12` 只 `pull → migrate → up`，永不构建。** GPU 机的磁盘和 CPU 留给推理。

为什么这样分、为什么不用 GHCR、为什么不用 SSH：见
[`plans/0004-two-host-cicd.md`](plans/0004-two-host-cicd.md#决策与理由)。

---

## 0. 先准备好这些信息

| 项 | 值 | 说明 |
| --- | --- | --- |
| 域名 | 如 `faces.zrc.sg` | 需要能解析到家宽公网 IP（DDNS 也可） |
| 端口转发 | 80、443 → `192.168.0.12` | 路由器上做；**不要**转发 5432 / 8080 / 5000 |
| 邀请码 | 自己定 | 明文不进任何文件，只存 argon2 hash |
| 磁盘 | 两台各留 ≥ 100GB | GPU 版 embedding 镜像是 GB 级的，registry 还留历史层 |

---

## 1. 构建机 192.168.0.15

### 1.1 Docker

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
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

确认：`docker compose version` 输出 v2.x。**不要**用 apt 里的 `docker.io` +
`docker-compose`（v1，语法不兼容本项目的 compose 文件）。

### 1.2 私有 registry

```bash
sudo mkdir -p /srv/registry
docker run -d --restart=always --name registry \
  -p 192.168.0.15:5000:5000 \
  -v /srv/registry:/var/lib/registry \
  -e REGISTRY_STORAGE_DELETE_ENABLED=true \
  registry:2.8
```

- 端口**只绑内网地址**，不是 `0.0.0.0` —— 这个 registry 没有鉴权。
- `REGISTRY_STORAGE_DELETE_ENABLED` 是后面垃圾回收的前提，开了才能删层。

### 1.3 让 Docker 接受这个明文 registry

**两台机器都要做。** `/etc/docker/daemon.json`：

```json
{
  "insecure-registries": ["192.168.0.15:5000"],
  "log-driver": "json-file",
  "log-opts": { "max-size": "50m", "max-file": "5" }
}
```

```bash
sudo systemctl restart docker
```

`log-opts` 不是可选项：默认的 json-file 日志**没有上限**，`embedding` 这种长跑服务
几个月就能把家用机的根分区写满。

> 明文 HTTP 可接受的理由：镜像里不含任何 secret（secret 全在 `.12` 的 `.env` 里），
> 端口只在内网。要上 TLS 就给 registry 签一张内网证书，workflow 不用改。

确认：`curl -s http://192.168.0.15:5000/v2/_catalog` 返回 `{"repositories":[]}`。

### 1.4 GitHub Actions runner

```bash
sudo useradd -m -s /bin/bash ghrunner
sudo usermod -aG docker ghrunner
```

> ⚠️ `docker` 组等价于 root（能挂载宿主机根目录）。这正是「这台 runner 只执行我们
> 自己的构建步骤、PR 的 lint/test 继续跑在 GitHub 托管 runner 上」这条纪律的原因。

到 GitHub → **Settings → Actions → Runners → New self-hosted runner** 拿下载与
`config.sh` 的命令（带一次性 token，1 小时内有效），以 `ghrunner` 身份执行，
**只把标签改成下面这样**：

```bash
sudo -iu ghrunner
mkdir -p ~/actions-runner && cd ~/actions-runner
# …页面给的 curl / tar 两行…
./config.sh --url https://github.com/superfive666/photo-gallery \
            --token <页面上的 token> \
            --name zrc-ci-15 \
            --labels zrc-ci \
            --work _work --unattended
exit

# 装成 systemd 服务，开机自启
sudo /home/ghrunner/actions-runner/svc.sh install ghrunner
sudo /home/ghrunner/actions-runner/svc.sh start
```

`self-hosted,linux,x64` 是 GitHub 自动加的，只需要额外给 `zrc-ci`。

确认：Settings → Runners 里 `zrc-ci-15` 显示 **Idle**，标签含 `zrc-ci`。

### 1.5 防火墙

```bash
sudo ufw allow from 192.168.0.0/24 to any port 22 proto tcp
sudo ufw allow from 192.168.0.0/24 to any port 5000 proto tcp
sudo ufw --force enable
```

---

## 2. 生产机 192.168.0.12

### 2.1 Docker + daemon.json

同 1.1 与 1.3（`insecure-registries` 这台也要）。

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

同 1.4，但标签是 `zrc-prod`、名字 `zrc-prod-12`：

```bash
./config.sh --url https://github.com/superfive666/photo-gallery \
            --token <token> --name zrc-prod-12 --labels zrc-prod \
            --work _work --unattended
```

### 2.4 生产状态目录

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

### 2.5 写 `.env`

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

# 镜像从内网 registry 拉
IMAGE_REPO=192.168.0.15:5000/photo-gallery

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

### 2.6 反向代理与 HTTPS

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

### 2.7 防火墙

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
| `IMAGE_REPO` | `192.168.0.15:5000/photo-gallery` | 换 registry 或换机器 IP |
| `DEPLOY_DIR` | `/opt/photo-gallery` | 换生产目录 |
| `VITE_API_BASE_URL` | `/api` | api 不再与前端同源时 |

**Secrets：这个项目一个都不需要。** 生产 `.env` 只在 `.12` 上，镜像从内网拉，
部署靠 runner 而不是 SSH 密钥 —— 没有任何东西需要经过 GitHub。
（`docs/cicd.md` 里那张 Secrets 表是给「用 GHCR + SSH 部署」那条路线留的，本拓扑用不上。）

**Settings → Actions → General**：
- Fork pull request workflows → *Require approval for all outside collaborators*
  （仓库现在是 private，属于兜底）。

**分支**：仓库目前**还没有 `main`**，默认分支就是
`claude/face-recognition-photo-gallery-gkjfbx`，所以 `push: main` 那两个触发器现在
永远不会触发。首次上线可以直接用 **Run workflow**（`workflow_dispatch`）手动跑。
正式建 `main` 时按 `docs/cicd.md` 打开分支保护。

---

## 4. 首次上线的顺序

1. `.15` 上手动验一遍构建（比在 Actions 日志里翻错误快得多）：

   ```bash
   sudo -iu ghrunner
   git clone https://github.com/superfive666/photo-gallery.git && cd photo-gallery
   export IMAGE_REPO=192.168.0.15:5000/photo-gallery IMAGE_TAG=manual EMBEDDING_GPU=true
   docker compose --profile tools build
   for s in api jobs web embedding; do docker push $IMAGE_REPO/$s:manual; done
   ```

   embedding 那一层会下载 300MB 模型权重 + GPU 版 onnxruntime，第一次很慢。

2. `.12` 上确认能拉到、GPU 能用：

   ```bash
   cd /opt/photo-gallery
   IMAGE_TAG=manual docker compose --profile tools pull
   IMAGE_TAG=manual docker compose up -d db
   IMAGE_TAG=manual docker compose run --rm jobs python -m jobs migrate
   IMAGE_TAG=manual docker compose up -d embedding api web
   docker compose exec embedding python -c \
     "import urllib.request,json;print(json.load(urllib.request.urlopen('http://localhost:8000/healthz')))"
   ```

   要看到 `gpu: True` **且** `batch_supported: True`。
   `gpu: False` → 回到 2.2 与 `.env` 的 `EMBEDDING_USE_GPU`；
   `batch_supported: False` → 识别模型的 ONNX 图 batch 维被固定成 1，批量会退化成
   逐张前向（见 `docker/README.md`）。

3. 记下这个手动版本，让回滚有目标：`echo manual > /opt/photo-gallery/.deployed-tag`

4. 探源站结构 —— **上线前必做**，当前解析器还是通用实现：

   ```bash
   cd /opt/photo-gallery
   IMAGE_TAG=manual docker compose run --rm jobs python -m jobs probe --album 2026-08-10
   ```

   把输出贴回仓库，按 [`data-source.md`](data-source.md) 收敛成精确选择器。
   这一步没做就跑 `ingest`，结果会是「跑完了，一张都没入库」或者只抓到缩略图。

5. 先小规模建库，再上全量：

   ```bash
   IMAGE_TAG=manual docker compose run --rm jobs python -m jobs ingest --album 2026-08-10
   ```

6. 到 Actions 里 **Run workflow** 跑一次 Deploy，走完整链路。
7. 故意验一次回滚：临时把 `api` 的 `/readyz` 改成 500 提交并部署，
   健康检查应在 150 秒后失败并自动回滚到上一个 tag，站点保持可用。
8. 走 `docs/cicd.md` 的部署前检查清单。

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

### registry 垃圾回收（`.15`）

删 tag 不会释放磁盘，要显式 GC：

```bash
docker exec registry bin/registry garbage-collect \
  --delete-untagged /etc/docker/registry/config.yml
```

`crontab -e`（root）里每周一次即可。GB 级的 embedding 镜像会让 `/srv/registry`
增长得比预期快。

### 磁盘

```bash
docker system df                 # 两台都看
du -sh /srv/registry /var/lib/docker
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
| deploy 卡在「拉取镜像」，报 `http: server gave HTTP response to HTTPS client` | `.12` 的 `daemon.json` 少了 `insecure-registries`，或改完没 `systemctl restart docker` |
| `/healthz` 里 `gpu: false` | 镜像不是 `EMBEDDING_GPU=true` 构建的；或 `.env` 少了 `EMBEDDING_USE_GPU=true`；或 `.env` 少了 `COMPOSE_FILE=...gpu.yml`（容器没挂到卡） |
| `batch_supported: false` | 识别模型 ONNX 的 batch 维是固定 1，批量退化为逐张。需要换一份动态 batch 导出 |
| 服务起来了但登录说邀请码错 | `INVITE_CODE_HASH` 抄漏了字符，或生成时用的明文不是分发出去的那个 |
| 第二次部署后连不上数据库 | `.env` 被 `git clean` 删过 —— 确认它在 `/opt/photo-gallery` 而不是 runner 工作区 |
| 一个人搜几次全站就被限流 | `real_ip` 没生效或 8080 被暴露，所有请求的来源 IP 塌成了同一个 |
| `ingest` 跑完 0 张入库 | 相册页解析没收敛，先跑 `jobs probe`（见 [`data-source.md`](data-source.md)） |
| deploy 报「没有上一次成功部署的记录」 | 首次部署时健康检查就失败了，没有回滚目标；看容器日志人工处理 |
| workflow 一直 Queued | 对应标签的 runner 离线：`sudo ~ghrunner/actions-runner/svc.sh status` |

---

## 7. 这套配置**没有**覆盖的

说清楚免得误以为已经有了：

- **没有 staging。** 部署直接上生产，靠健康检查 + 回滚兜底。
- **registry 没有鉴权也没有 TLS。** 内网 + 镜像不含 secret 的前提下可接受。
- **数据库没有主从、没有 PITR。** 只有每日 `pg_dump`；最坏情况丢一天的建库结果
  （重跑 `ingest` 能重建，因为原图始终在源站）。
- **备份没有异地副本。** 家里失火/被盗就没了。真在意就把 `/srv/backups` 同步到别处。
- **没有监控告警。** 服务挂了要靠人发现或下次 workflow 失败才知道。
- **单机单副本。** 部署期间有几秒不可用。
