# CI/CD（GitHub Flow + 自建 Runner）

## 分支模型：GitHub Flow

```
main  ──●────────●────────●──────▶   始终可部署，受保护
         \      /  \    /
          ●──●─    ●──●            feature/*、fix/* 短生命周期分支
```

- `main` 受保护：禁止直推、必须经 PR、必须 CI 通过、至少 1 个 approve。
- 分支命名：`feature/<slug>`、`fix/<slug>`、`chore/<slug>`。
- PR 合并用 squash，保持 `main` 线性。
- 合到 `main` 即触发部署到自建服务器。没有 staging 环境（社团项目，不值当），
  但部署带健康检查与自动回滚。

---

## ⚠️ 自建 Runner 的安全前提（先读这一节）

**在 public 仓库上使用 self-hosted runner 是一个已知的严重安全问题。**
默认配置下，任何人 fork 仓库、提一个 PR，其中的 workflow 就会在你家的机器上执行任意代码 ——
可以读取宿主机文件、扫内网、抓取 runner 上的凭据、留后门。

本仓库必须满足以下**全部**条件：

1. **仓库设为 private**，或者若必须 public，则：
   - `Settings → Actions → General → Fork pull request workflows`
     设为 **"Require approval for all outside collaborators"**（更严格的选 all external contributors）。
   - 涉及 self-hosted runner 的 job 加 `if: github.event.pull_request.head.repo.full_name == github.repository`，
     从源头拒绝来自 fork 的执行。
2. **绝不使用 `pull_request_target`** 来跑构建。该事件在 base 仓库上下文中执行并携带 secrets，
   如果 checkout 了 PR 的代码就等于把 secrets 直接交出去。
3. **PR 的 CI 跑在 GitHub 托管 runner 上**（lint/test/build，无 secrets、无部署权限）；
   **只有部署 job 跑在自建 runner 上**，且仅由 `push: main` 与手动 `workflow_dispatch` 触发。
   这是本项目的核心隔离原则。
4. Runner 以**非 root 专用账户**运行，不加入 docker 组以外的特权组，
   工作目录与生产数据卷分离。
5. 每个 job 结束后清理工作区（`actions/checkout` 默认会 clean，但构建缓存要自己管）。
6. Runner 用**标签**精确选中（`runs-on` 匹配 label 而不是 name），避免误跑到别的机器：
   - `superfive-ubuntu` = 192.168.0.12，构建 + 部署 + 定时建库；
   - `superfive-pi5` = 192.168.0.15，已注册但目前没有分配 job。
   两台 runner 的账户都在 `docker` 组里 —— 等价于 root，所以「哪台机器上跑什么」
   就是这个项目最重要的一道边界。**两台都不跑测试代码。**

---

## 两台机器

| 机器 | 硬件 | Runner label | 职责 |
| --- | --- | --- | --- |
| `192.168.0.12` | x86_64、NVIDIA GPU | `superfive-ubuntu` | 就地 build → migrate → up → 健康检查；定时 ingest |
| `192.168.0.15` | Raspberry Pi 5、arm64 | `superfive-pi5` | 已注册，目前没有分配 job（备用容量） |

**没有镜像仓库**：构建就在要部署的那台机器上做，产物留在本机 docker 里，
用 `sha-<short>` tag 区分版本。回滚 = 换个 tag 重新 `up`，因为旧镜像还在本机 ——
所以 `docker image prune` 的一周保留窗口就是回滚能力的唯一来源。

部署靠 `.12` 上的 runner 而不是从别处 SSH 过去（否则要把生产机私钥交给 CI 平台）。
两台机器的 runner 装法完全一样，只有 `--name` / `--labels` 和架构不同 —— 步骤见
[`deployment.md` 第 1 节](deployment.md#1-两台机器都要做github-actions-runner)。

> ⚠️ 两台架构不同，所以 `runs-on` 里**不写 `x64`**：写了只会在选错机器时表现为
> 「永远 Queued」。用机器名做唯一 label 定位。

---

## Workflow 布局

| 文件 | 触发 | Runner | 作用 |
| --- | --- | --- | --- |
| `.github/workflows/ci.yml` | `pull_request`、`push: main` | GitHub 托管 | lint、typecheck、test、docker build（不 push） |
| `.github/workflows/deploy.yml` | `push: main`、`workflow_dispatch` | **自建** `superfive-ubuntu` | build → 迁移 → 滚动重启 → 健康检查 → 失败回滚 |
| `.github/workflows/ingest.yml` | `schedule`、`workflow_dispatch` | **自建** `superfive-ubuntu` | 定时增量建库（用 `.deployed-tag` 里那个版本） |

### `ci.yml` 内容

并行三个 job：

- `python` — `uv sync --all-packages --frozen` 之后跑 `ruff check`、
  `ruff format --check`、`mypy --strict`、迁移、`pytest`
  （用 service container 起 `pgvector/pgvector:pg16`）。
  `--frozen` 顺带守卫「改了依赖但忘记提交 `uv.lock`」—— 那种情况会直接失败。
- `web` — `npm ci`、`tsc --noEmit`、`eslint`、`vitest run`、`npm run build`
- `docker` — 四个 Dockerfile 都 `docker build`，验证可构建，**不 push**

不跑评估集（需要真人照片，不能上传到 GitHub runner）。

### `deploy.yml` 内容

单个 job，全程在 `.12` 上：

```
① checkout
② IMAGE_TAG = sha-<short>
③ 校验 /opt/photo-gallery/.env 存在且权限 600
④ build（在工作区里，但 --env-file 指向生产 .env；EMBEDDING_GPU=true）
⑤ 同步 compose 文件与 docs/schema 到 /opt/photo-gallery（.env 与数据卷不动）
⑥ migrate            —— 迁移必须在新代码启动之前
⑦ up -d --no-deps embedding api web
⑧ 轮询 /readyz；通过则写 .deployed-tag，失败则用 .deployed-tag 里的旧 tag 重启
⑨ image prune --filter until=168h
```

构建在工作区里做（build context 需要源码），运行在 `/opt/photo-gallery` 里做
（`.env` 和数据卷在那儿）。`--env-file` 把两者接起来，否则 compose 插值会把一堆变量
解析成空串，只留一串 warning。

生产状态（`.env`、`.deployed-tag`、数据卷）全部在 `/opt/photo-gallery`，
**不在 runner 工作区** —— `actions/checkout` 的 `git clean -ffdx` 会把 gitignore
的文件一起删掉，`.env` 放在工作区里第二次部署就没了。

回滚只换镜像 tag，不回滚数据库。这是「先迁移、后换代码」+「DDL 只追加」
（CLAUDE.md 约束 6）共同换来的：旧代码在新 schema 上能正常工作。

**迁移的位置很关键**：DDL 只以新增列/新增表的方式演进（见下），所以「先迁移、后换代码」是安全的
—— 旧代码不认识新列但不会因此报错。反过来（先换代码后迁移）会有一段窗口期新代码访问不存在的列。

破坏性变更（删列、改类型、加 NOT NULL）必须拆成两次部署：
先兼容双写 → 部署 → 数据回填 → 再删旧结构。

### `ingest.yml` 内容

```yaml
on:
  schedule:
    - cron: "0 18 * * *"     # UTC；对应新加坡时间次日 02:00
  workflow_dispatch:
    inputs:
      album: { description: "album id，留空则全部", required: false }
      full:  { description: "全量重跑", type: boolean, default: false }
```

跑 `docker compose run --rm jobs ingest`。没有后续步骤 —— 检索是实时的，
不做聚类（见 `plans/0003`）。

> cron 的时区是 UTC，写的时候记得换算 —— 这是最常踩的坑。

---

## 镜像与版本

- **没有镜像仓库。** 镜像只存在于 `.12` 的 docker 里，名字是
  `photo-gallery-{api,jobs,web,embedding}:sha-<short>`。
  以后要引入仓库（GHCR / Docker Hub / 内网 registry）只改 `IMAGE_PREFIX` 一个变量。
- Tag 规则：`sha-<short>`，每次部署一个。**不用 `latest`** —— 都叫 latest 就没法回滚。
  定时 ingest 读 `.deployed-tag`，跑的是当前在线的那个版本。
- 回滚就是换 tag 重新 `up`。前提是旧镜像还在本机，所以
  `docker image prune --filter until=168h` 的窗口别调短，也别用
  `docker system prune -a`（会连可回滚的旧版本一起删）。
- `embedding` 镜像含模型权重（约 +300MB），只在 `embedding/`、`uv.lock` 或模型版本
  变化时重建 —— 用 `paths` 过滤避免每次 PR 都重建它。
- 依赖变更由 `uv.lock` 唯一决定，所以「同一个 commit 构建出的镜像装的是同一批版本」
  这件事是有保证的（Dockerfile 用 `uv sync --frozen`）。

## Secrets 与 Variables

**这个拓扑下 GitHub Secrets 一个都不需要。** 没有镜像仓库凭据、部署靠 `.12` 上的
runner 而不是 SSH 密钥、生产 `.env` 只存在于 `.12` 上 —— 没有任何机密需要经过 GitHub。
少一处存放就少一处泄漏面。

需要的是两个非机密的 Variables（都有默认值，可以不设）：

| Variable | 默认 | 用途 |
| --- | --- | --- |
| `DEPLOY_DIR` | `/opt/photo-gallery` | 生产状态目录 |
| `VITE_API_BASE_URL` | `/api` | 前端构建期注入的 api 地址 |

生产 `.env` 里需要填的机密（都在 `.12` 上生成、不外传）：
`POSTGRES_PASSWORD`、`JWT_SECRET`、`INVITE_CODE_HASH`（argon2 hash，不存明文）、
`AUDIT_HASH_SALT`。
源站公开无鉴权，所以没有 `SOURCE_TOKEN`；不签名原图链接，所以没有 `SIGNED_URL_SECRET`。

- `.env` 权限 `600`，属主为 runner 账户，放在 `/opt/photo-gallery`
  **而不是 runner 工作区**（会被 `git clean -ffdx` 删掉）。
- `.env.example` 进 git，`.env` 永不进 git（`.gitignore` 已覆盖）。

## 部署前检查清单

首次上线前逐项确认。逐步配置见 [`deployment.md`](deployment.md)。

- [ ] 仓库为 private，或已按上文配置 fork PR 审批
- [ ] 两个 runner 都以非 root 专用账户运行，**label**（不只是 name）分别为
      `superfive-ubuntu` / `superfive-pi5`
- [ ] `main` 分支保护规则已开启（仓库目前还没有 `main`）
- [ ] `.env` 已就位、权限 600，且**不在** runner 工作区里
- [ ] `.12` 上 `docker run --rm --gpus all ubuntu nvidia-smi` 能出卡
- [ ] embedding 的 `/healthz` 返回 `gpu: true` 且 `batch_supported: true`
- [ ] 已演练过一次「健康检查失败 → 自动回滚」
- [ ] 反向代理 + HTTPS 证书就绪（子域名，如 `faces.zrc.sg`）
- [ ] Postgres 数据卷已纳入备份，**且已完整演练过一次恢复**
- [ ] 隐私告知文案与同意勾选已上线
- [ ] 邀请码已通过安全渠道分发给成员
- [ ] 阈值已用评估集标定（见 `evaluation.md`），不是默认占位值
