# Photo Gallery — Face Search

以自拍为查询条件，在 [photos.zrc.sg](https://photos.zrc.sg) 的历史活动相册中检索出「有我的照片」。

本项目是 photos.zrc.sg 的**旁挂部署**，不修改原站。它离线抓取原站指定 album 的照片、提取人脸
embedding 落库，再提供一个上传自拍即可实时检索的 Web 界面。

---

## 目录结构

| 路径 | 说明 |
| --- | --- |
| `docs/` | 全部设计文档（架构、隐私、CI/CD、评估方法） |
| `docs/schema/` | Postgres DDL，按 `NNN_name.sql` 顺序编号，随迭代追加 |
| `docs/plans/` | 每次迭代的计划书，一次迭代一个文件 |
| `web/` | 前端（React + Vite + TypeScript + Tailwind） |
| `api/` | 后端（FastAPI），负责鉴权、检索、缩略图分发 |
| `embedding/` | 人脸检测 + embedding 推理服务（InsightFace buffalo_l），独立容器 |
| `jobs/` | 可定时或手动执行的离线任务：拉取 album → 提取人脸 → 落库 → 聚类 |
| `libs/gallery_core/` | `api` 与 `jobs` 共享的 Python 包（DB 模型、embedding 客户端、配置） |
| `docker/` | 全部 Dockerfile |
| `.github/workflows/` | CI/CD（self-hosted runner） |

> `embedding/` 与 `libs/` 是在原定结构上追加的两个目录。前者来自「embedding 走独立容器」的选型；
> 后者用于避免 `api` 与 `jobs` 的 DB 模型和预处理逻辑各自漂移 —— 这类漂移会直接导致检索静默失效。
> 理由见 [`docs/architecture.md`](docs/architecture.md#为什么要有-libsgallery_core)。

---

## 三条主链路

```
① 离线建库   jobs → photos.zrc.sg → 人脸检测/embedding → Postgres(pgvector) → 人脸聚类成 person
② 在线检索   web → api → embedding(自拍) → pgvector KNN → 聚合到 person → 返回照片 URL + 缩略图
③ 前端界面   上传/拍摄自拍 → 展示匹配结果网格 → lightbox / 跳回原站原图
```

详见 [`docs/architecture.md`](docs/architecture.md)。

---

## 快速开始

```bash
cp .env.example .env          # 填入 SOURCE_* 与 INVITE_CODE / JWT_SECRET
make up                       # 起 postgres + embedding + api + web
make migrate                  # 执行 docs/schema 下的 DDL
make ingest ALBUM=<album-id>  # 手动跑一次离线建库
open http://localhost:8080
```

常用命令见 `make help`。

---

## 已知局限

这些是人脸识别的固有局限，不是 bug，请提前对使用者做预期管理：

- **小脸召回不了。** 短边小于 `MIN_FACE_PX`（默认 40px）的人脸 embedding 不可靠，会被丢弃。
  大合影里站在后排的人很可能搜不到。
- **侧脸、遮挡、口罩、墨镜** 会显著降低召回率。person 聚类能部分缓解（侧脸可经由同簇的正脸被间接命中）。
- **儿童照片、跨越多年的照片**准确率下降明显 —— 人脸随年龄变化，ArcFace 对此不鲁棒。
- **误报无法归零。** 阈值是 precision/recall 的权衡，调高会漏、调低会串人。阈值标定方法见
  [`docs/evaluation.md`](docs/evaluation.md)。
- **视频当前不处理**（第一期范围外），见 `docs/plans/`。

---

## 隐私

- 用户上传的自拍**只在内存中处理**，提取 embedding 后立即丢弃；不落盘、不落库、不写日志。
- 人脸 embedding 属于生物识别数据（新加坡 PDPA）。数据的用途、留存与删除方式见
  [`docs/privacy.md`](docs/privacy.md)。
- 站点需要邀请码才能使用 —— 公开开放会让任何人拿一张他人照片就能扒出该人的全部活动照片。
- 提供 opt-out：可按 person 或按照片将内容从检索结果中永久屏蔽。

---

## 模型许可

使用 [InsightFace](https://github.com/deepinsight/insightface) 的 `buffalo_l` 预训练模型包
（RetinaFace `det_10g` + ArcFace `w600k_r50`，512 维）。

> ⚠️ **InsightFace 的预训练模型仅授权用于非商业研究用途。** 本项目作为社团内部非商业使用符合该
> 授权；若日后转为商业用途，必须更换为商业可用的模型权重。此约束请勿在迭代中被静默忽略。
