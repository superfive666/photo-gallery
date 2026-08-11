# 0004 — 构建机与部署机分离（.15 构建 / .12 运行）

## 背景

原先的 `deploy.yml` 假设「runner 就是部署目标」：在同一台机器上 `docker compose build`
然后 `up -d`。实际有两台内网机器：

| 机器 | 硬件 | 角色 |
| --- | --- | --- |
| `192.168.0.15` | Ubuntu，无 GPU | 构建镜像、推私有 registry |
| `192.168.0.12` | Ubuntu，有 NVIDIA GPU | 跑生产服务、存 Postgres 数据卷 |

在这个拓扑下原 workflow 跑不通 —— 它会在没有 GPU 的机器上把服务起起来。

## 目标

1. `.15` 只负责构建与推镜像，**永不接触生产数据库和 `.env`**。
2. `.12` 只负责 `pull → migrate → up -d → 健康检查`，**永不构建**。
3. 部署产物可寻址、可回滚：镜像用 `sha-<short>` tag，回滚就是换 tag 重启。
4. 一份文档能让人从裸 Ubuntu 走到上线（`docs/deployment.md`）。

## 范围

- 新增 `docker-compose.gpu.yml`（GPU 设备声明，只在 `.12` 上叠加）。
- `docker-compose.yml` 的四个可构建服务补 `image:` 字段 —— 没有它就没法 pull。
- `deploy.yml` 拆成 `build`（`zrc-ci` = .15）→ `deploy`（`zrc-prod` = .12）两个 job。
- 生产状态目录固定为 `/opt/photo-gallery`，`.env` 放那里。
- `web/nginx.conf` 加 `real_ip` —— 前面多了一层宿主机反向代理之后必须的修正。
- `docs/deployment.md`：两台机器的逐步 runbook。

## 非范围

- 不做 staging 环境。
- 不做蓝绿/多副本。单机社团项目，滚动重启 + 回滚 tag 够用。
- 不把 CI（lint/test）搬到自建 runner 上。见下面的「决策」。
- 不引入 Kubernetes、Swarm、Ansible。

## 决策与理由

### 镜像怎么从 .15 到 .12：内网 registry，不用 GHCR

`.15` 上跑一个 `registry:2`，`.12` 从 `192.168.0.15:5000` 拉。

- GHCR 要把镜像经家宽**上传**再**下载**一遍。GPU 版 embedding 镜像（onnxruntime-gpu
  + CUDA 运行时 + 300MB 模型权重）是 GB 级的，家宽上行是这条链路上最慢的一环。
- GitHub Packages 对 private 仓库的存储是**计费额度**（Free 500MB / Pro 2GB），
  一个 GB 级镜像加上历史 tag 会直接超额。
- 内网 registry 走千兆，且按层增量传输 —— 改业务代码时只有最后几层动。

代价：registry 是明文 HTTP，两台机器都要加 `insecure-registries`。可接受，因为
**镜像里不含任何 secret**（secret 全在 `.12` 的 `.env` 里），且端口只绑内网地址。
想上 TLS 的话换成给 registry 签一张内网证书即可，workflow 不用改。

不用 `docker save | ssh docker load`：每次都要传全量，且要维护跨机 SSH 密钥。

### 部署怎么触发：在 .12 上再装一个 runner，不用 SSH

`.12` 装第二个 runner，标签 `zrc-prod`；`.15` 的标签是 `zrc-ci`。
两个 job 用标签精确选中，GitHub 负责调度和日志。

不用「`.15` 通过 SSH 登到 `.12` 执行」：那需要在 GitHub secrets 里放一把能登生产机的
私钥，等于把生产机的入口交给 CI 平台，与「`.15` 永不接触生产」的目标冲突。

### CI（lint/test）留在 GitHub 托管 runner 上

`.15` 上的 runner 只执行本仓库部署分支上的构建步骤。lint/test 会跑测试代码、
装 npm 依赖 —— 那是需要隔离的东西，继续跑在托管 runner 上（CLAUDE.md 约束 9）。
`.15` 的价值是「构建不占 GPU 机的磁盘和 CPU」和「拿住那道隔离边界」，不是省 CI 分钟数。

### embedding 镜像只构建 GPU 变体

唯一的部署目标有 GPU，所以 CI 构建时固定 `EMBEDDING_GPU=true`。
`onnxruntime-gpu` 在没有 CUDA 的机器上 `import` 不会炸，providers 列表里带
`CPUExecutionProvider` 兜底（见 `embedding/app/model.py`），所以这个镜像在 CPU 机器上
仍能跑，只是慢。本地开发不受影响：`docker compose build` 默认还是 CPU 变体。

## 验收标准

- [ ] `.15` 上 `docker compose build` 后 `docker compose push` 成功，registry 里能看到
      四个 repository 的 `sha-<short>` 与 `latest`。
- [ ] `.12` 上 `docker compose pull` 拿到同一批镜像，`docker compose ps` 四个服务 healthy。
- [ ] `curl -s localhost:8000/healthz` 在 embedding 容器里返回 `gpu: true` 且
      `batch_supported: true`。
- [ ] `workflow_dispatch` 手动触发 deploy，全流程绿，`/opt/photo-gallery/.deployed-tag`
      记下了新 tag。
- [ ] 故意部署一个起不来的版本，健康检查失败后自动回滚到上一 tag，服务可用。
- [ ] `actions/checkout` 跑过之后 `/opt/photo-gallery/.env` 仍在（见下面的风险）。

## 风险

1. **`actions/checkout` 会 `git clean -ffdx`，连 gitignore 的文件一起删。**
   原设计把 `.env` 放在 runner 工作区里，第二次部署就会被清掉，服务带着默认密码重启。
   这次把生产状态挪到工作区之外的 `/opt/photo-gallery`，工作区只用于取 compose 文件。
2. **runner 账户在 `docker` 组里，等价于 root。** 这正是「`.12` 的 runner 只跑我们自己的
   部署步骤、不跑测试代码」这条纪律的原因。
3. **多了一层宿主机反向代理，`X-Forwarded-For` 会退化。** 容器里看到的 `$remote_addr`
   是 docker 网桥地址，全部用户会被限流器当成同一个 IP。`nginx.conf` 里用 `real_ip`
   修正，但这依赖「8080 只绑 127.0.0.1」—— 一旦把 8080 暴露到公网，客户端就能伪造
   `X-Forwarded-For` 绕过限流。`.env` 里的 `WEB_BIND` 是这条约束的开关。
4. **registry 磁盘只增不减。** 删 tag 之后要手动 garbage-collect，否则家用机磁盘会被
   历史层吃满。运维一节里有 cron。
5. **迁移在新镜像启动之前跑**，这个顺序不能改（理由见 `docs/cicd.md`）。
   回滚只回滚镜像、不回滚数据库，前提是 DDL 始终只做向后兼容的追加（CLAUDE.md 约束 6）。
