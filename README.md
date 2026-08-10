# Photo Gallery — Face Search

以自拍为查询条件，在 [photos.zrc.sg](https://photos.zrc.sg) 的历史活动相册中检索出「有我的照片」。

本项目是 [photos.zrc.sg](https://photos.zrc.sg) 的**旁挂部署**，不修改原站。
原站是一个公开、无需鉴权的跑团照片墙，照片和视频按 album 分组
（`photos.zrc.sg/album/2026-08-10`）。

本项目离线抓取指定 album 的照片、批量提取人脸 embedding 落库，
再提供一个上传自拍即可实时检索的 Web 界面。

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
① 离线建库   jobs → photos.zrc.sg → 批量人脸检测/embedding → Postgres(pgvector) → 聚类成 person
② 在线检索   web → api → embedding(自拍) → pgvector KNN → 聚合到 person → 照片 URL + 缩略图
③ 前端界面   上传/拍摄自拍（可选限定相册）→ 结果网格 → lightbox / 跳回原站原图
```

数据模型只有两张主表：

| 表 | 一行是什么 | 关键字段 |
| --- | --- | --- |
| `photo` | 一张照片/视频 | `id`(uuidv7) `album` `photo_url` `thumbnail` |
| `face` | **一张脸** | `id`(uuidv7) `photo_id` `album` `embedding(512)` |

一张合影有多个人 → 一条 photo 对应多条 face，这是分表的原因。
`face` 按 `album` 做 LIST 分区，使「只在某次活动里找」能被裁剪成单分区精确检索。

详见 [`docs/architecture.md`](docs/architecture.md)。

---

## 快速开始

```bash
cp .env.example .env               # 填入 INVITE_CODE_HASH / JWT_SECRET / AUDIT_HASH_SALT
make up                            # 起 postgres + embedding + api + web
make migrate                       # 执行 docs/schema 下的 DDL
make probe  ALBUM=2026-08-10       # 先探查源站页面结构，确认解析正确
make ingest ALBUM=2026-08-10       # 批量拉取 + embedding + 落库
make cluster                       # 人脸聚类成 person
open http://localhost:8080
```

`INVITE_CODE_HASH` 用 `python -m api.app.tools.hash_invite` 生成（明文邀请码不进 .env）。

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
- **视频当前不处理**（只登记不提取），见 `docs/plans/`。
- **相册页解析目前是通用实现。** 源站的确切标记结构还没确认，`jobs probe` 用于验证；
  见 [`docs/data-source.md`](docs/data-source.md)。

---

## 隐私

- 用户上传的自拍**只在内存中处理**，提取 embedding 后立即丢弃；不落盘、不落库、不写日志。
  这条纪律由 `api/tests/test_no_persistence.py` 做源码级守卫，不只是文档承诺。
- 人脸 embedding 属于生物识别数据（新加坡 PDPA）。数据的用途、留存与删除方式见
  [`docs/privacy.md`](docs/privacy.md)。
- **源站公开不等于本站可以公开。** 原站上找某人的照片得一张张翻，而这里输入一张自拍就能
  把他在所有活动里的照片一次性聚齐 —— 可及性的量变就是隐私上的质变。
  所以本站需要邀请码。
- 本项目只存 256px 缩略图，原图始终只是一个指向源站的 URL，不复制、不缓存。
- 提供 opt-out：可按 person 或按照片将内容从检索结果中永久屏蔽。

---

## 模型许可

使用 [InsightFace](https://github.com/deepinsight/insightface) 的 `buffalo_l` 预训练模型包
（RetinaFace `det_10g` + ArcFace `w600k_r50`，512 维）。

> ⚠️ **InsightFace 的预训练模型仅授权用于非商业研究用途。** 本项目作为社团内部非商业使用符合该
> 授权；若日后转为商业用途，必须更换为商业可用的模型权重。此约束请勿在迭代中被静默忽略。
