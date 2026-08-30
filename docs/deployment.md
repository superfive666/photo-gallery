# 部署 Runbook

从裸 Ubuntu 走到上线。命令按顺序执行即可，每一节末尾都有一条「确认」命令 ——
没通过就不要往下走。

> 本项目**没有 CI/CD 自动化**：不跑 GitHub Actions，也没有自建 runner。
> 构建、部署、定时建库全部在生产机上执行（见 [`plans/0013`](plans/0013-drop-cicd-go-public.md)）。
> 文中的内网地址、域名、账户名都是占位符，按自己的环境替换。

## 拓扑

```
┌──────────────────────────────────────────┐
│ 生产机（x86_64 + NVIDIA GPU）            │
│                                          │
│ 仓库 clone ──► scripts/deploy.sh         │
│   就地 build → migrate → up → 健康检查   │
│                                          │
│ compose: api / web / embedding(GPU) /    │
│          worker                          │
│ 宿主机 Postgres · .env · 反向代理        │
│ 镜像只存在于本机，无镜像仓库             │
└────────────────┬─────────────────────────┘
                 │ 443
          <你的域名>（公网）
```

没有镜像仓库：**构建就在要部署的那台机器上做**。少一跳传输、少一套凭据。
代价是构建的磁盘和 CPU 开销落在 GPU 机上。

版本靠 tag 区分（`photo-gallery-api:sha-abc1234`），**回滚 = 换个 tag 重新 up**。
镜像按数量保留：本机永远留两套 —— 当前在线（`.deployed-tag`）与上一个
（`.previous-tag`，回滚目标），更早的在每次部署后自动删除。

---

## 0. 先准备好这些信息

| 项 | 值 | 说明 |
| --- | --- | --- |
| 域名 | 如 `faces.example.com` | 需要能解析到公网 IP（家宽用 DDNS 也可） |
| 端口转发 | 80、443 → 生产机内网 IP | 路由器上做；**不要**转发 5432 / 8080 |
| 邀请码 | 自己定 | 明文不进任何文件，只存 argon2 hash |
| 磁盘 | ≥ 100GB 可用 | 构建也在这台机器上，GPU 版 embedding 镜像是 GB 级的 |

**部署账户**：本文用 `deploy` 指代执行部署的非 root 账户。它只需要两样权限：

```bash
sudo useradd -m -s /bin/bash deploy
sudo usermod -aG docker deploy      # 要先装好 docker，见 1.1
```

**只加 `docker` 组，不要加进 `sudo`。** 运行时需要的权限只有 docker socket
以及对 `/opt/photo-gallery` 和 `/srv/backups` 的写权限（1.4 节的 `chown` 给了）。
文档里那些 `sudo` 都是**你本人**做的一次性安装动作，不是部署账户需要的能力。

---

## 1. 生产机

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

给容器日志加上限 —— 默认的 json-file **没有上限**，`embedding` 这种长跑服务几个月
就能把根分区写满。`/etc/docker/daemon.json`：

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

### 1.2 NVIDIA 驱动与容器运行时

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

### 1.3 宿主机 Postgres

数据库用这台机器上**已有的 Postgres**，不跑容器化 pg。容器（api / jobs）经
`host.docker.internal` 访问它 —— compose 里的 `extra_hosts: host-gateway` 会把这个
名字解析成宿主机在 docker 网桥上的地址（通常是 `172.17.0.1`）。

**① 装 pgvector**（HNSW 需要 ≥ 0.5，apt 的包跟着你的 pg 大版本走）：

```bash
psql --version                       # 先确认大版本，比如 16
sudo apt-get install -y postgresql-16-pgvector
```

**② 建用户、建库、预建扩展**（以 postgres 超级用户执行一次）：

```bash
sudo -u postgres psql <<'SQL'
CREATE USER gallery WITH PASSWORD '<与 .env 的 DATABASE_URL 一致>';
CREATE DATABASE photo_gallery OWNER gallery;
\c photo_gallery
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
SQL
```

> ⚠️ **扩展必须在这里预建。** 迁移脚本里虽然写了 `CREATE EXTENSION IF NOT EXISTS`，
> 但 `vector` 不是 trusted 扩展，`gallery` 这种普通用户**建不了**，只能跳过已存在的。
> 漏了这步，第一次 migrate 就会报 permission denied。

**③ 让 pg 接受来自 docker 网段的连接**。改两个文件（路径随大版本，如
`/etc/postgresql/16/main/`）：

`postgresql.conf`：

```
listen_addresses = '*'        # 或至少加上 172.17.0.1；只听 localhost 是容器连不上的头号原因
```

`pg_hba.conf` 加一行（放在默认 reject 规则之前）：

```
host  photo_gallery  gallery  172.16.0.0/12  scram-sha-256
```

```bash
sudo systemctl restart postgresql
```

`172.16.0.0/12` 覆盖 docker 默认网桥和 compose 自建网络的整个私有段。
配合 1.6 的 ufw 规则，pg 对外仍然是关着的 —— 能到 5432 的只有本机和容器。

**④ 验证**（用一个临时容器从 docker 网段里连一次，模拟 api/jobs 的真实路径）：

```bash
docker run --rm --add-host=host.docker.internal:host-gateway postgres:16-alpine \
  psql "postgresql://gallery:<密码>@host.docker.internal:5432/photo_gallery" \
  -c "SELECT extname FROM pg_extension"
```

要看到 `vector` 和 `pgcrypto`。连接被拒 → 查 ③ 的两个文件和 ufw；
密码错 → 和 `.env` 的 `DATABASE_URL` 对一遍。

### 1.4 生产状态目录与仓库 clone

```bash
sudo mkdir -p /opt/photo-gallery/data/{sample-albums,eval}
sudo chown -R deploy:deploy /opt/photo-gallery
# 备份目录归 postgres：备份 cron 以 postgres 身份跑（见「日常运维」）
sudo install -d -o postgres -g postgres -m 750 /srv/backups
```

构建需要源码，所以生产机上还要有一份仓库 clone（与生产状态目录分开）：

```bash
sudo -iu deploy
git clone https://github.com/superfive666/photo-gallery.git ~/photo-gallery
exit
```

`/opt/photo-gallery` 里最终是这些东西：

| 路径 | 谁写的 | 说明 |
| --- | --- | --- |
| `.env` | **人手动放，只放一次** | 600 权限，永不进 git |
| `docker-compose.yml` / `docker-compose.gpu.yml` | `deploy.sh` 同步 | |
| `docs/schema/*.sql` | `deploy.sh` 同步 | `jobs` 容器挂载它做迁移 |
| `.deployed-tag` | `deploy.sh` 写 | 当前在线的镜像 tag，回滚就靠它 |
| `.previous-tag` | `deploy.sh` 写 | 上一个在线版本，清理镜像时保留的第二套 |

> ⚠️ **生产状态目录与仓库 clone 必须分开。** `.env` 放在 clone 里的话，
> 一次 `git clean -ffdx` 就会连 gitignore 的文件一起删掉，服务会带着默认密码重启。
> 构建在 clone 里做，但读的是 `/opt/photo-gallery/.env`。

### 1.5 写 `.env`

以 `deploy` 身份，用仓库里的 `.env.example` 打底：

```bash
sudo -iu deploy
cd /opt/photo-gallery
cp ~/photo-gallery/.env.example .env
chmod 600 .env
```

生产机上**必须**与示例不同的几项：

```dotenv
# 密码/密钥：用 openssl rand -base64 36 生成，别复用
JWT_SECRET=<随机>
AUDIT_HASH_SALT=<随机>
# ⚠️ 必须整体用单引号包住 —— hash 里全是 $，不加引号会被 compose 插值啃烂，
# 登录直接 500。hash_invite 工具输出的就是带引号的整行，原样粘贴。
INVITE_CODE_HASH='<见下>'

# 宿主机 pg（1.3 建好的那个库）。密码与 CREATE USER 时一致。
# POSTGRES_* 三个变量是本地容器化 pg 用的，生产可以直接删掉。
DATABASE_URL=postgresql+asyncpg://gallery:<密码>@host.docker.internal:5432/photo_gallery

# 这台机器有 GPU：叠加 GPU 层 + 打开运行时开关
COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml
EMBEDDING_USE_GPU=true
# 推理在 GPU 上，CPU 线程只用于解码/resize
ORT_NUM_THREADS=4
# 显存够就往上调，这是一次识别前向送多少张脸
REC_BATCH_SIZE=128

# 前面有宿主机反向代理，端口只对本机开
WEB_BIND=127.0.0.1
WEB_PORT=8080
# ⚠️ 还没上 HTTPS、用 http://<内网IP>:8080 直测时，这一项必须 false ——
# 否则浏览器拒收 Secure cookie（登录 200 但一切后续请求 401）。上 Caddy 后改回 true。
SESSION_COOKIE_SECURE=false

# jobs 容器的挂载点要真实存在，否则 docker 会建出 root 属主的空目录
SOURCE_LOCAL_DIR=/opt/photo-gallery/data/sample-albums
EVAL_DIR=/opt/photo-gallery/data/eval
```

`IMAGE_PREFIX` 保持默认。`IMAGE_TAG` 不用管：每次部署成功后 `deploy.sh` 会把它
写成当前上线的 `sha-<short>`，所以手敲的 `docker compose` 命令默认就指向在线版本。
⚠️ 首次部署**之前**它还是 `dev` —— 那时手动 `up`/`run` 必须显式带
`IMAGE_TAG=$(cat .deployed-tag)`（或 manual 之类的真实 tag），否则 compose 找不到
`:dev` 镜像会试图在没有源码的生产目录里现场构建。

`INVITE_CODE_HASH` 在**任意**一台装了本项目依赖的机器上生成，只把 hash 抄过来：

```bash
uv run python -m api.app.tools.hash_invite
```

`INVITE_CODE_HASH` 是**全相册管理码**（站主自用）。发给成员的码用绑定相册的
邀请码 —— 存数据库、逐相册隔离、可单独吊销：

```bash
# 在生产机上（jobs 容器里带齐了依赖和 DATABASE_URL）
docker compose run --rm jobs python -m jobs invite create --album 2026-08-10 --label "发给张三"
# 输出的完整码只显示这一次，立即发给对方；库里只存 hash，无法找回
docker compose run --rm jobs python -m jobs invite list
docker compose run --rm jobs python -m jobs invite disable --prefix <8位hex>
# 吊销不影响已登录的 session，会随 JWT 过期（SESSION_TTL_HOURS）自然失效
```

持这种码登录的用户：只能检索绑定的那个相册、相册下拉锁定为该相册、
拿别的相册的照片一律 404。

剪辑码（一码一相册，见 [`media-edit.md`](media-edit.md)）：

```bash
docker compose run --rm jobs python -m jobs invite create --role edit \
    --album 2026-08-10 --label "发给谁"
```

`COMPOSE_FILE` 写在 `.env` 里而不是每次加 `-f`：这样 `deploy.sh`、`make up`、
手敲的 `docker compose ps` 全都自动带上 GPU 叠加层，不会出现「手动起的没挂 GPU」
这种只在事后从日志里才发现的偏差。

确认：`stat -c %a /opt/photo-gallery/.env` 输出 `600`。

### 1.6 反向代理与 HTTPS

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
<你的域名> {
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

### 1.7 防火墙

```bash
sudo ufw allow from <你的内网网段>/24 to any port 22 proto tcp
sudo ufw allow 80,443/tcp
sudo ufw --force enable
```

8080（web）不放行 —— 它只绑 127.0.0.1。5432（Postgres）只对 docker 网段开：

```bash
sudo ufw allow from 172.16.0.0/12 to any port 5432 proto tcp
```

（容器到宿主机服务的流量走 INPUT 链，ufw 默认 deny 会拦掉它 —— pg_hba 放行了
也没用，这条规则不能省。）路由器上也**只**转发 80/443。

---

## 2. 部署

在生产机的仓库 clone 里执行：

```bash
sudo -iu deploy
cd ~/photo-gallery
git pull
./scripts/deploy.sh
```

脚本做的事，按顺序：

```
① 校验 /opt/photo-gallery/.env 存在且权限 600
② IMAGE_TAG = sha-<short>，就地 build（--env-file 指向生产 .env；EMBEDDING_GPU=true）
③ 同步 compose 文件与 docs/schema 到 /opt/photo-gallery（.env 与数据卷不动）
④ migrate            —— 迁移必须在新代码启动之前
⑤ up -d --no-deps embedding api web worker
⑥ 轮询 /readyz（最多 150 秒）
⑦ 失败：先抓容器日志，再用 .deployed-tag 里的旧 tag 回滚
⑧ 成功：写 .deployed-tag / .previous-tag，把 IMAGE_TAG 写回 .env
⑨ 清理旧镜像，只保留当前在线 + 上一个
```

**为什么迁移在换代码之前**：DDL 只以新增列/新增表的方式演进（CLAUDE.md 约束 6），
所以「先迁移、后换代码」是安全的 —— 旧代码不认识新列但不会因此报错。
反过来（先换代码后迁移）会有一段窗口期新代码访问不存在的列。

破坏性变更（删列、改类型、加 NOT NULL）必须拆成两次部署：
先兼容双写 → 部署 → 数据回填 → 再删旧结构。

回滚只换镜像 tag，不回滚数据库 —— 这正是上面那个顺序换来的好处。

手动回滚到上一个版本：

```bash
cd /opt/photo-gallery
IMAGE_TAG=$(cat .previous-tag) docker compose up -d --no-deps --force-recreate \
    embedding api web worker
```

---

## 3. 定时建库

原先由 workflow 每天触发，现在是生产机上的一条 cron。以 `deploy` 身份
（`crontab -u deploy -e`）：

```cron
# 每天 02:00 增量建库。IMAGE_TAG 取当前在线版本，别让 compose 去找不存在的 :dev。
0 2 * * * cd /opt/photo-gallery && IMAGE_TAG=$(cat .deployed-tag) \
  docker compose run --rm jobs python -m jobs ingest >> /opt/photo-gallery/ingest.log 2>&1
```

手动跑一次（或指定相册 / 全量重跑）：

```bash
cd /opt/photo-gallery
IMAGE_TAG=$(cat .deployed-tag) docker compose run --rm jobs python -m jobs ingest
IMAGE_TAG=$(cat .deployed-tag) docker compose run --rm jobs python -m jobs ingest --album 2026-08-10
IMAGE_TAG=$(cat .deployed-tag) docker compose run --rm jobs python -m jobs ingest --full
```

建库不能并发跑（两个 ingest 会互相争抢 embedding 服务并产生冗余下载）。
cron 的间隔远大于单次耗时，正常不会重叠；手动触发前先确认没有在跑的 jobs 容器。

---

## 4. 首次上线的顺序

1. 先手动构建一遍（比在脚本输出里翻错误快）：

   ```bash
   sudo -iu deploy
   cd ~/photo-gallery
   export IMAGE_TAG=manual EMBEDDING_GPU=true
   docker compose --env-file /opt/photo-gallery/.env --profile tools build
   ```

   embedding 那一层会下载 300MB 模型权重 + GPU 版 onnxruntime，第一次很慢。

2. 起库、迁移、起服务，确认 GPU 真的在用：

   ```bash
   cd /opt/photo-gallery
   export IMAGE_TAG=manual
   docker compose run --rm jobs python -m jobs migrate
   docker compose up -d embedding api web
   docker compose exec embedding python -c \
     "import urllib.request,json;print(json.load(urllib.request.urlopen('http://localhost:8000/healthz')))"
   ```

   要看到 `gpu: True` **且** `batch_supported: True`。
   `gpu: False` → 回到 1.2 与 `.env` 的 `EMBEDDING_USE_GPU`；
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

6. 跑一次 `./scripts/deploy.sh`，走完整链路。
7. 故意验一次回滚：临时把 `api` 的 `/readyz` 改成 500 再部署，
   健康检查应在 150 秒后失败并自动回滚到上一个 tag，站点保持可用。
8. 走下面的上线前检查清单。

### 上线前检查清单

- [ ] `.env` 已就位、权限 600，且**不在**仓库 clone 里
- [ ] `docker run --rm --gpus all ubuntu nvidia-smi` 能出卡
- [ ] embedding 的 `/healthz` 返回 `gpu: true` 且 `batch_supported: true`
- [ ] 已演练过一次「健康检查失败 → 自动回滚」
- [ ] 反向代理 + HTTPS 证书就绪
- [ ] 宿主机 Postgres 已纳入备份（pg_dump cron），**且已完整演练过一次恢复**
- [ ] 定时建库的 cron 已就位，且手动跑通过一次
- [ ] 隐私告知文案与同意勾选已上线
- [ ] 邀请码已通过安全渠道分发给成员
- [ ] 阈值已用评估集标定（见 `evaluation.md`），不是默认占位值

---

## 5. 日常运维

### 备份（宿主机 pg，以 `postgres` 身份）

数据库在宿主机上，备份就是普通的 `pg_dump`，不经过任何容器。
`sudo crontab -u postgres -e`：

```cron
# 每天 03:30 备份，保留 14 天。cron 里 % 必须转义。
30 3 * * * pg_dump -Fc photo_gallery > /srv/backups/pg-$(date +\%F).dump 2>>/srv/backups/backup.log
0 4 * * 0 find /srv/backups -name 'pg-*.dump' -mtime +14 -delete
```

（postgres 本地 peer 认证免密码；`/srv/backups` 在 1.4 已经 chown 给它了。）

**演练一次恢复，否则不算有备份**：

```bash
sudo -u postgres createdb -O gallery restore_test
sudo -u postgres pg_restore -d restore_test /srv/backups/pg-2026-08-11.dump
sudo -u postgres psql -d restore_test -c 'select count(*) from face;'
sudo -u postgres dropdb restore_test
```

缩略图是 BYTEA，备份体积基本等于 `照片数 × 缩略图大小`，几万张照片是几个 GB 量级。

### 磁盘

构建也在这台机器上，所以磁盘是要盯的东西：

```bash
docker system df
du -sh /var/lib/docker
```

每次部署结束自动清理：**只保留两套镜像** —— 当前在线与上一个（回滚目标），
更早的 tag 直接删；悬空层清掉；buildkit 构建缓存上限 20GB（CUDA 轮子与模型权重的
缓存层别清，它们是「改业务代码构建只要几十秒」的原因）。
GPU 版 embedding 镜像单个约 5.5GB，两套 + 缓存的稳态占用约 35GB。

手工清理时**别用 `docker system prune -a`** —— 那会把回滚要用的上一版镜像一起删掉。

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
并重算，而不是整库作废（CLAUDE.md 约束 5）。流程：部署 → `ingest --full`。

---

## 6. 故障排查

| 现象 | 大概率原因 |
| --- | --- |
| `deploy.sh` 报 `.env 权限是 xxx` | `chmod 600 /opt/photo-gallery/.env` |
| 构建时一堆 `variable is not set` warning | 没带 `--env-file /opt/photo-gallery/.env`（脚本里带了，手敲时容易漏） |
| `docker` 报 permission denied | 部署账户没加进 `docker` 组，或加完没重新登录 |
| embedding 起不来，日志报 `CUDA provider 未生效` | 这是**有意的拒绝启动**（不再静默退回 CPU）。按报错里的排查顺序：onnxruntime 缺库日志 → 镜像是否 GPU 构建 → 驱动 ≥580 → 是否叠加 gpu.yml |
| embedding 日志有 `Failed to load library ... libcublasLt` | 旧版镜像缺 CUDA 运行时库 —— 重新部署即可，新构建会装齐并在构建期校验 |
| `/healthz` 里 `gpu: false` 但没报错 | `.env` 少了 `EMBEDDING_USE_GPU=true`（这属于「没要求 GPU」，不触发拒绝启动） |
| `batch_supported: false` | 识别模型 ONNX 的 batch 维是固定 1，批量退化为逐张。需要换一份动态 batch 导出 |
| 服务起来了但登录说邀请码错（401） | `INVITE_CODE_HASH` 抄漏了字符，或生成时用的明文不是分发出去的那个 |
| 登录成功（200）但之后所有 /api 都 401 | 走 http 直测但 `SESSION_COOKIE_SECURE` 还是 true，浏览器拒收 Secure cookie；设 false 并重建 api（上 HTTPS 后改回 true） |
| 登录直接 500 | `INVITE_CODE_HASH` 没用单引号包住，`$argon2id` 等被 compose 当变量啃掉了；`docker compose logs api` 里会有 `invite_code_hash_invalid` |
| 一个人搜几次全站就被限流 | `real_ip` 没生效或 8080 被暴露，所有请求的来源 IP 塌成了同一个 |
| web 容器 unhealthy 但页面正常 | 旧镜像的探活用 `localhost`（在 alpine 里先解析成 ::1，nginx 只听 IPv4）——纯误报，重新部署即可 |
| `ingest` 跑完 0 张入库 | 相册页解析没收敛，先跑 `jobs probe`（见 [`data-source.md`](data-source.md)） |
| migrate 报 connection refused | pg 只听 localhost（`listen_addresses`），或 ufw 没放行 docker 网段（1.3③ / 1.7） |
| migrate 报 no pg_hba.conf entry | `pg_hba.conf` 少了 172.16.0.0/12 那行，或加在了 reject 之后 |
| migrate 报 permission denied to create extension | 扩展没预建 —— 回 1.3② 以超级用户建 `vector` / `pgcrypto` |
| 回滚报「本机已经没有 xxx 的镜像」 | 旧镜像被清掉了。只能人工修，或直接重跑一次部署 |
| 手动 up/recreate 报 `pull access denied` 后开始 Building，再报 `lstat .../docker: no such file` | `IMAGE_TAG` 落在了 `dev`：显式带 `IMAGE_TAG=$(cat .deployed-tag)`，或跑一次 `deploy.sh` 让它把 tag 写回 .env |
| `docker compose` 刷一屏 `variable is not set` 警告 | `.env` 里有值含 `$` 且没加单引号（典型是 INVITE_CODE_HASH）——正是登录 500 那个坑 |
| 剪辑任务卡住/失败 | `job_run` 表是第一现场，见 [`media-edit.md`](media-edit.md) |

---

## 7. 这套配置**没有**覆盖的

说清楚免得误以为已经有了：

- **没有 CI/CD 自动化。** 没有 PR 门禁（改成本地 `make check`），部署要人手跑
  `deploy.sh`。取舍见 [`plans/0013`](plans/0013-drop-cicd-go-public.md)。
- **没有 staging。** 部署直接上生产，靠健康检查 + 回滚兜底。
- **构建与运行同机。** 构建期间 CPU / 磁盘 IO 会和在线检索抢资源。
- **回滚只靠本机镜像。** 机器换盘就没有回滚目标了 —— 那时只能重新构建部署。
  要更稳就得引入镜像仓库（改 `IMAGE_PREFIX` 一个变量）。
- **数据库没有主从、没有 PITR。** 只有每日 `pg_dump`；最坏情况丢一天的建库结果
  （重跑 `ingest` 能重建，因为原图始终在源站）。
- **备份没有异地副本。** 机器所在地失火/被盗就没了。真在意就把 `/srv/backups` 同步到别处。
- **没有监控告警。** 服务挂了要靠人发现。
- **单机单副本。** 部署期间有几秒不可用。
