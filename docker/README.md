# Dockerfile

全部 Dockerfile 集中在这里，`docker-compose.yml` 在仓库根目录。

| 文件 | 服务 | 说明 |
| --- | --- | --- |
| `Dockerfile.embedding` | `embedding` | 含 InsightFace 模型权重（约 +300MB）；`--package embedding` 不带 DB 栈 |
| `Dockerfile.api` | `api` | 不含模型，镜像轻量 |
| `Dockerfile.jobs` | `jobs` | 一次性容器，也用于跑 pytest |
| `Dockerfile.web` | `web` | 多阶段：node 构建 → nginx 托管 |

三个 Python 镜像都用 `uv sync --frozen --no-dev --package <成员>`，
所以每个镜像只装自己那个成员的依赖：`embedding` 里没有 sqlalchemy，
`api` 里没有 pillow/lxml。

## 构建上下文是仓库根目录

四个 Dockerfile 的 `context` 都是 `.` 而不是各自的子目录。原因有两个：

1. 它们都需要 `COPY libs`（`gallery-core`，api 与 jobs 共享的核心包）。
2. 依赖由 **uv workspace** 管理，`uv.lock` 是整个 workspace 共享的一份 ——
   `uv sync` 需要读到根 `pyproject.toml` 与**全部四个成员**的 `pyproject.toml`
   才能校验锁文件一致，所以这些清单都要拷进去。

单独构建时也要在根目录执行：

```bash
docker build -f docker/Dockerfile.api -t photo-gallery-api .
```

单独构建时也要在根目录执行：

```bash
docker build -f docker/Dockerfile.api -t photo-gallery-api .
```

## 层缓存顺序

Python 三个镜像统一是「workspace 清单 + libs 源码 → `uv sync --frozen` → 业务代码」。
改业务代码不会让依赖层失效。`libs` 拷的是源码（不只是清单），因为 gallery-core 是
可编辑安装的成员，构建它需要包目录在位。

`uv sync` 用 `--frozen`：锁文件与清单不一致时直接失败，而不是静默重新解析 ——
镜像里的依赖必须与 `uv.lock` 完全一致，否则 CI 通过不代表生产能跑。

前端仍是 `npm ci`（web 不在 uv workspace 里）。

## 模型权重打进镜像

`Dockerfile.embedding` 在**构建时**下载 buffalo_l，不在运行时拉取。代价是镜像大
300MB 左右，换来的是冷启动不依赖外网、首个请求不超时。

因此这个镜像只在 `embedding/` 或模型版本变化时才需要重建 —— CI 里用 `paths` 过滤，
别让每个 PR 都重建它。

## GPU 推理

默认构建的是 CPU 版。有 NVIDIA GPU 时：

```bash
# 1. 构建时把已锁定的 CPU 版 onnxruntime 换成同版本的 GPU 版
docker compose build --build-arg EMBEDDING_GPU=true embedding
# 2. 打开运行时开关（决定 providers 与 ctx_id）
echo "EMBEDDING_USE_GPU=true" >> .env
```

⚠️ 这一步**有意步出了 uv.lock**：onnxruntime 与 onnxruntime-gpu 提供同一个模块，
而 insightface 硬依赖前者，没法用互斥 extra 干净地二选一。GPU 版的版本号是从已锁定的
CPU 版读出来的，所以两者始终一致，不需要另外维护一个常量。

GPU 构建还会装齐 CUDA 运行时轮子（cublas / cudart / curand / cudnn / nvrtc，
约 +1.5GB）：onnxruntime-gpu 自己不带这些库，nvidia-container-toolkit 也只注入
驱动。缺库时 onnxruntime **不报错，静默退回 CPU** —— 首次上线就中过这个招。
两道防线：构建期 ldd 校验 CUDA provider 的依赖齐全（缺了直接构建失败）；
运行期 `EMBEDDING_USE_GPU=true` 而 CUDA provider 未生效时服务拒绝启动，
让健康检查当场失败并触发回滚。
详见 `docker/Dockerfile.embedding` 里那段注释与 `embedding/pyproject.toml`。

还需要：

- 宿主机装好 NVIDIA 驱动与 [nvidia container runtime](https://github.com/NVIDIA/nvidia-container-toolkit)；
- 在 `docker-compose.yml` 的 `embedding` 服务下加 `gpus: all`（或 `deploy.resources.reservations.devices`）。

启动后用 `curl localhost:8000/healthz` 确认 `gpu: true` 且 `batch_supported: true`。
`batch_supported` 为 false 说明识别模型的 ONNX 图 batch 维被固定成 1，批量会退化成
逐张前向 —— GPU 利用率上不去，需要换一份动态 batch 的导出。

## 非 root 运行

除 `web`（nginx 官方镜像自己处理）外都切到 uid 10001 的 `appuser`。
构建与运行同机（见 `docs/deployment.md`），容器逃逸的代价直接落在那台机器上 ——
所以这一条不是形式主义。
