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
6. Runner 打上标签 `self-hosted, linux, x64, zrc-prod`，用标签精确选中，
   避免误跑到别的机器。

---

## Workflow 布局

| 文件 | 触发 | Runner | 作用 |
| --- | --- | --- | --- |
| `.github/workflows/ci.yml` | `pull_request`、`push: main` | GitHub 托管 | lint、typecheck、test、docker build（不 push） |
| `.github/workflows/deploy.yml` | `push: main`、`workflow_dispatch` | **自建** `zrc-prod` | 构建并推镜像 → 迁移 → 滚动重启 → 健康检查 |
| `.github/workflows/ingest.yml` | `schedule`、`workflow_dispatch` | **自建** `zrc-prod` | 定时增量建库 |

### `ci.yml` 内容

并行三个 job：

- `python` — `ruff check`、`ruff format --check`、`mypy --strict`、`pytest`
  （用 service container 起 `pgvector/pgvector:pg16` 跑集成测试）
- `web` — `npm ci`、`tsc --noEmit`、`eslint`、`vitest run`、`npm run build`
- `docker` — 四个 Dockerfile 都 `docker build`，验证可构建，**不 push**

不跑评估集（需要真人照片，不能上传到 GitHub runner）。

### `deploy.yml` 内容

```
① checkout（仅 main）
② docker compose build
③ make migrate        —— 迁移必须在新代码启动之前
④ docker compose up -d --no-deps api embedding web
⑤ 健康检查轮询 /readyz，失败则回滚到上一个镜像 tag
⑥ 通知（可选）
```

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

跑 `docker compose run --rm jobs ingest`。聚类在 ingest 之后自动跟一次
（新照片会产生新的 face，簇需要更新）。

> cron 的时区是 UTC，写的时候记得换算 —— 这是最常踩的坑。

---

## 镜像与版本

- 镜像推到 **GHCR**（`ghcr.io/superfive666/photo-gallery/<service>`）。
  比自建 registry 省事，且自建 runner 拉取自己家网络出口也够快。
- Tag 规则：`sha-<short>`（每次构建）+ `latest`（main）。
  部署时用 `sha-` tag，回滚就是换 tag 重启。
- `embedding` 镜像含模型权重（约 +300MB），只在 `embedding/` 或模型版本变化时重建 ——
  用 `paths` 过滤避免每次 PR 都重建它。

## Secrets

| Secret | 用途 |
| --- | --- |
| `POSTGRES_PASSWORD` | DB 密码 |
| `JWT_SECRET` | session 签名 |
| `INVITE_CODE_HASH` | 邀请码的 argon2 hash（不存明文） |
| `SOURCE_BASE_URL` / `SOURCE_TOKEN` | 源站访问 |
| `SIGNED_URL_SECRET` | 原图短效链接签名 |

- 生产 `.env` 存在自建服务器上，由 runner 读取，**不经过 GitHub**。
  GitHub secrets 只放部署本身需要的东西。
- `.env` 权限 `600`，属主为 runner 账户。
- `.env.example` 进 git，`.env` 永不进 git（`.gitignore` 已覆盖）。

## 部署前检查清单

首次上线前逐项确认：

- [ ] 仓库为 private，或已按上文配置 fork PR 审批
- [ ] Runner 以非 root 专用账户运行，标签为 `zrc-prod`
- [ ] `main` 分支保护规则已开启
- [ ] `.env` 已就位且权限 600
- [ ] 反向代理 + HTTPS 证书就绪（子域名，如 `faces.zrc.sg`）
- [ ] Postgres 数据卷已纳入备份，**且已完整演练过一次恢复**
- [ ] 隐私告知文案与同意勾选已上线
- [ ] 邀请码已通过安全渠道分发给成员
- [ ] 阈值已用评估集标定（见 `evaluation.md`），不是默认占位值
