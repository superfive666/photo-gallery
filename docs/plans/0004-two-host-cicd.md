# 0004 — 自建 runner 落地（两台机器，.12 就地构建并部署）

## 背景

原先的 `deploy.yml` 假设「runner 就是部署目标」，且假设只有一台机器。实际有两台：

| 机器 | 硬件 | Runner label | 角色 |
| --- | --- | --- | --- |
| `192.168.0.12` | x86_64、NVIDIA GPU | `superfive-ubuntu` | 构建 + 部署 + 定时建库 |
| `192.168.0.15` | Raspberry Pi 5、arm64 | `superfive-pi5` | 已注册，暂无分配 job |

## 目标

1. 两台机器都注册成 self-hosted runner，用**唯一 label**（机器名）精确定位。
2. 部署全程在 `.12` 上：就地 `build → migrate → up -d → 健康检查`，不引入镜像仓库。
3. 版本可回滚：镜像打 `sha-<short>` tag，回滚 = 换 tag 重新 `up`。
4. 一份文档能让人从裸 Ubuntu 走到上线（`docs/deployment.md`）。

## 范围

- 新增 `docker-compose.gpu.yml`（GPU 设备声明，只在 `.12` 上叠加）。
- `docker-compose.yml` 四个可构建服务显式声明 `image`，形式
  `${IMAGE_PREFIX}-<svc>:${IMAGE_TAG}` —— 用默认名（都叫 `:latest`）就没有回滚能力。
- `deploy.yml` 单 job，跑在 `superfive-ubuntu` 上。
- 生产状态目录固定为 `/opt/photo-gallery`，`.env` 放那里。
- `web/nginx.conf` 加 `real_ip` —— 前面多了一层宿主机反向代理之后必须的修正。
- `docs/deployment.md`：runner 安装 + 生产机配置的逐步 runbook。

## 非范围

- 不做 staging 环境。
- 不做蓝绿/多副本。单机社团项目，滚动重启 + 回滚 tag 够用。
- 不把 CI（lint/test）搬到自建 runner 上。见下面的「决策」。
- 不引入镜像仓库。见下面的「决策」。
- 不引入 Kubernetes、Swarm、Ansible。

## 决策与理由

### 镜像怎么传：不传，就在部署的机器上构建

演进过三版，记录下来免得以后重复讨论：

1. **`.15` 上跑内网 `registry:2`** —— GPU 版 embedding 镜像（onnxruntime-gpu +
   CUDA 运行时 + 300MB 模型权重）是 GB 级的，走内网千兆最快。代价：多一套要运维的
   服务，两台机器都得加 `insecure-registries`。
2. **Docker Hub 私有仓库** —— 不用自运维，镜像有异地副本。代价：GB 级镜像要经家宽
   上传再下载；而且 Docker Hub 仓库名只有两级，四个服务 = 四个私有仓库，
   **免费版只含 1 个**。
3. **不要仓库，直接在 `.12` 上构建。** ← 采用

第 3 版是最省的：构建产物就在要运行它的 docker daemon 里，零传输、零凭据、零额度。
既然唯一的部署目标只有一台机器，中间那一跳本来就不产生价值。

代价，写清楚：

- 构建的 CPU / 磁盘 IO 落在 GPU 机上，会和在线检索抢资源。靠每次部署后
  `docker image prune --filter until=168h` 控制磁盘。
- **回滚只依赖本机镜像。** 没有仓库可拉，那个一周的保留窗口就是回滚能力的唯一来源。
  机器换盘或窗口过期就只能重新构建部署。
- 没有镜像的异地副本。

`IMAGE_PREFIX` 变量留着：以后要引入仓库（GHCR / Docker Hub / 内网 registry），
改这一个值就行，compose 与 workflow 都不用动。

### 部署怎么触发：`.12` 自己的 runner，不用 SSH

不用「从别的机器 SSH 登到 `.12` 执行」：那需要在 GitHub Secrets 里放一把能登生产机的
私钥，等于把生产机的入口交给 CI 平台。让 `.12` 自己跑 runner，GitHub 只负责调度和日志。

这个拓扑下 **GitHub Secrets 一个都不需要**。

### 用机器名做 label，`runs-on` 里不写架构

`runs-on` 匹配的是 runner 的 **label** 而不是 name，且标签之间是「与」的关系。
两台机器架构不同（`.15` 是 arm64 的 Pi 5），所以 `runs-on` 写
`[self-hosted, linux, superfive-ubuntu]` —— 不带 `x64`。

把架构写进 `runs-on` 的后果是「选错机器时永远 Queued」，不报错、不超时，
是最难定位的一类故障。用一个唯一 label 定位最直接。

### CI（lint/test）留在 GitHub 托管 runner 上

自建 runner 上只执行本仓库部署分支上的构建/部署步骤。lint/test 会跑测试代码、装 npm
依赖 —— 那是需要隔离的东西（CLAUDE.md 约束 9），继续跑在托管 runner 上。

`.15` 也不适合接这个活：`insightface` 没有 arm64 预编译 wheel，大概率要从源码编译。

### embedding 镜像只构建 GPU 变体

唯一的部署目标有 GPU，所以部署时固定 `EMBEDDING_GPU=true`。
`onnxruntime-gpu` 在没有 CUDA 的机器上 `import` 不会炸，providers 列表里带
`CPUExecutionProvider` 兜底（见 `embedding/app/model.py`），所以这个镜像在 CPU 机器上
仍能跑，只是慢。本地开发不受影响：`docker compose build` 默认还是 CPU 变体。

## 验收标准

- [ ] 两个 runner 在 Settings → Runners 里都是 **Idle**，label 分别为
      `superfive-ubuntu` / `superfive-pi5`（不只是 name）。
- [ ] `.12` 上手动 `docker compose --env-file /opt/photo-gallery/.env --profile tools build`
      能过。
- [ ] `docker compose ps` 四个服务 healthy。
- [ ] embedding 容器里打 `/healthz` 返回 `gpu: true` 且 `batch_supported: true`。
- [ ] `workflow_dispatch` 手动触发 deploy，全流程绿，`/opt/photo-gallery/.deployed-tag`
      记下了新 tag。
- [ ] 故意部署一个起不来的版本，健康检查失败后自动回滚到上一 tag，服务可用。
- [ ] `actions/checkout` 跑过之后 `/opt/photo-gallery/.env` 仍在（见下面的风险）。

## 风险

1. **`actions/checkout` 会 `git clean -ffdx`，连 gitignore 的文件一起删。**
   原设计把 `.env` 放在 runner 工作区里，第二次部署就会被清掉，服务带着默认密码重启。
   这次把生产状态挪到工作区之外的 `/opt/photo-gallery`：构建在工作区里做，
   但 `--env-file` 指向生产 `.env`，运行时的 compose 命令都 `cd` 到那个目录。
2. **runner 账户在 `docker` 组里，等价于 root。** 这正是「runner 只跑我们自己的
   部署步骤、不跑测试代码」这条纪律的原因。
3. **多了一层宿主机反向代理，`X-Forwarded-For` 会退化。** 容器里看到的 `$remote_addr`
   是 docker 网桥地址，全部用户会被限流器当成同一个 IP。`nginx.conf` 里用 `real_ip`
   修正，但这依赖「8080 只绑 127.0.0.1」—— 一旦把 8080 暴露到公网，客户端就能伪造
   `X-Forwarded-For` 绕过限流。`.env` 里的 `WEB_BIND` 是这条约束的开关。
4. **`docker system prune -a` 会毁掉回滚能力。** 它会删掉「没有容器在用」的镜像，
   也就是上一个版本。运维文档里只允许 `docker image prune`（带 `until` 过滤）。
5. **迁移在新镜像启动之前跑**，这个顺序不能改（理由见 `docs/cicd.md`）。
   回滚只回滚镜像、不回滚数据库，前提是 DDL 始终只做向后兼容的追加（CLAUDE.md 约束 6）。
