# Dockerfile

全部 Dockerfile 集中在这里，`docker-compose.yml` 在仓库根目录。

| 文件 | 服务 | 说明 |
| --- | --- | --- |
| `Dockerfile.embedding` | `embedding` | 含 InsightFace 模型权重（约 +300MB） |
| `Dockerfile.api` | `api` | 不含模型，镜像轻量 |
| `Dockerfile.jobs` | `jobs` | 一次性容器，也用于跑 pytest |
| `Dockerfile.web` | `web` | 多阶段：node 构建 → nginx 托管 |

## 构建上下文是仓库根目录

四个 Dockerfile 的 `context` 都是 `.` 而不是各自的子目录，因为它们都需要
`COPY libs/`（`api` 与 `jobs` 共享的核心包）。所以 `COPY` 的路径都带子目录前缀，
例如 `COPY api/requirements.txt ./api/requirements.txt`。

单独构建时也要在根目录执行：

```bash
docker build -f docker/Dockerfile.api -t photo-gallery-api .
```

## 层缓存顺序

统一是「依赖清单 → 安装依赖 → 共享包 → 业务代码」。改业务代码不会让
`pip install` / `npm ci` 那几层失效。

## 模型权重打进镜像

`Dockerfile.embedding` 在**构建时**下载 buffalo_l，不在运行时拉取。代价是镜像大
300MB 左右，换来的是冷启动不依赖外网、首个请求不超时。

因此这个镜像只在 `embedding/` 或模型版本变化时才需要重建 —— CI 里用 `paths` 过滤，
别让每个 PR 都重建它。

## 非 root 运行

除 `web`（nginx 官方镜像自己处理）外都切到 uid 10001 的 `appuser`。
自建 runner 上尤其重要：容器逃逸的代价直接落在家用机器上。
