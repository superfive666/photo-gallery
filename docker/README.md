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

## GPU 推理

默认构建的是 CPU 版。有 NVIDIA GPU 时：

```bash
# 1. 用 GPU 版 onnxruntime 构建 embedding 镜像
ORT_PACKAGE=onnxruntime-gpu docker compose build embedding
# 2. 打开 GPU 开关
echo "EMBEDDING_USE_GPU=true" >> .env
```

还需要：

- 宿主机装好 NVIDIA 驱动与 [nvidia container runtime](https://github.com/NVIDIA/nvidia-container-toolkit)；
- 在 `docker-compose.yml` 的 `embedding` 服务下加 `gpus: all`（或 `deploy.resources.reservations.devices`）。

启动后用 `curl localhost:8000/healthz` 确认 `gpu: true` 且 `batch_supported: true`。
`batch_supported` 为 false 说明识别模型的 ONNX 图 batch 维被固定成 1，批量会退化成
逐张前向 —— GPU 利用率上不去，需要换一份动态 batch 的导出。

## 非 root 运行

除 `web`（nginx 官方镜像自己处理）外都切到 uid 10001 的 `appuser`。
自建 runner 上尤其重要：容器逃逸的代价直接落在家用机器上。
