# 架构设计

## 1. 全景

```
                          ┌──────────────────────────────────────┐
                          │        photos.zrc.sg (源站)          │
                          │      自建 / 静态相册，只读访问        │
                          └───────────────┬──────────────────────┘
                                          │ ① 按 album 拉取（限速、流式）
                                          ▼
┌─────────┐   HTTP    ┌──────────────────────────┐   HTTP   ┌──────────────────┐
│  jobs   │──────────▶│    embedding service     │◀─────────│       api        │
│ 离线建库 │  /extract │  InsightFace buffalo_l    │ /extract │  鉴权/检索/缩略图 │
└────┬────┘           │  RetinaFace + ArcFace     │          └────┬─────────────┘
     │                │  512-d, L2 normalized     │               │
     │                └──────────────────────────┘               │
     │  ② 落库 photo/face                          ③ KNN 检索      │
     ▼                                                            ▼
   ┌────────────────────────────────────────────────────────────────┐
   │              Postgres 16 + pgvector (HNSW, cosine)             │
   │   album / photo / face / person / person_alias / block_list    │
   └────────────────────────────────────────────────────────────────┘
                                                                  ▲
                                                       ④ /api/*   │
                                          ┌───────────────────────┴──┐
                                          │   web (nginx + React)    │
                                          │  上传自拍 → 结果网格      │
                                          └──────────────────────────┘
```

## 2. 组件职责

### `embedding` — 推理服务（独立容器）

**唯一**允许做人脸检测与 embedding 的地方。对外只有两个接口：

- `POST /extract` — 收图片字节，返回 `[{bbox, det_score, landmarks, embedding, quality}]`
- `GET /healthz` — 含模型加载状态与版本

设计要点：

- 模型在进程启动时加载一次并常驻内存，绝不按请求加载。
- ONNXRuntime 推理是 CPU 密集的同步调用，**必须**在 threadpool 中执行，否则会阻塞 asyncio
  event loop，让整个服务在并发下卡死。
- 服务本身单 worker（模型一份，内存可控），靠 threadpool 提供并发；不够时横向加容器实例。
- 出口统一做 L2 归一化，下游拿到的向量可以直接 cosine 比较。
- 模型文件在**构建镜像时**下载打包进镜像，不在运行时拉 —— 避免冷启动依赖外网。

**为什么独立成容器**：`api` 和 `jobs` 共用同一份推理逻辑，从物理上保证离线库与在线查询落在
同一个向量空间；模型换版本时只重建这一个镜像；可以单独限制它的 CPU/内存配额。

### `api` — 业务服务

- 邀请码换取签名 session cookie（HttpOnly / Secure / SameSite=Lax），JWT，短有效期。
- `POST /api/search` — multipart 上传 1~3 张自拍，同步返回匹配结果。
- `GET /api/photos/{id}/thumb` — 输出缩略图 WebP，带 ETag 与长缓存。
- `GET /api/photos/{id}/original` — 302 到源站的**短效签名 URL**。
- 不加载任何模型，镜像轻量，可自由扩容。

### `jobs` — 离线建库

一次性容器，`docker compose run` 或 cron 触发。CLI 子命令：

```bash
python -m jobs ingest  --album <id> [--full]   # 拉取 + 提取 + 落库（默认增量）
python -m jobs cluster [--album <id>]          # 人脸聚类成 person
python -m jobs recompute --model <name>        # 换模型后重算存量 embedding
```

### `web` — 前端

Vite 构建为纯静态产物，nginx 托管并反代 `/api` 到 `api` 容器。移动端优先。

## 3. 检索算法

朴素做法是「自拍向量 vs 每一条 face 向量取 top-k」。本项目不这么做，因为一个人的侧脸/背光照片
和其正脸自拍的 cosine 距离往往超过阈值，会漏掉大量本该命中的照片。

改为**两段式：先认人，再取图**。

```
① 离线：对全库 face embedding 做 HDBSCAN 聚类 → person 簇
        每个簇记录 centroid（成员向量均值再归一化）与成员数

② 在线：
   a. 自拍 → embedding（多张自拍则取均值后重新归一化，等价于一个更稳的查询点）
   b. 对 person.centroid 做 KNN，取 cosine 相似度 ≥ PERSON_MATCH_THRESHOLD 的候选簇
   c. 对 face.embedding 做 KNN，取相似度 ≥ FACE_MATCH_THRESHOLD 的直接命中脸
   d. 合并：候选簇内的全部 face → 其所属 photo；并上 (c) 的直接命中
   e. 每张 photo 的得分 = 该照片上所有命中脸的最高相似度
   f. 按得分降序返回，附 album / 拍摄时间 / 缩略图
```

`(c)` 这一路是给聚类失败的人脸（噪声点、单张出现的人）留的兜底，不能省。

**为什么簇心投票有效**：簇内包含同一人的正脸、侧脸、不同光照的多个样本，簇心比任何单张
照片都更接近这个人的「平均长相」，因此对查询自拍的角度差异更鲁棒。

### 质量门控

在入库阶段就丢掉不可靠的人脸，比在检索阶段调阈值有效得多：

- `det_score < MIN_DET_SCORE`（默认 0.5）→ 丢弃
- 人脸框短边 `< MIN_FACE_PX`（默认 40px）→ 丢弃并计数（这是「后排的人搜不到」的根因）
- 极端长宽比（>2.5，通常是误检）→ 丢弃

被丢弃的人脸要记数并暴露在 job 报告里，否则「召回低」会无从下手排查。

## 4. 数据模型要点

完整 DDL 见 [`schema/001_init.sql`](schema/001_init.sql)。几个关键决策：

**`photo` 与 `face` 必须分表。** 一张十人合影产生 1 条 photo + 10 条 face。把 embedding 挂在
photo 上是从一开始就走不通的。

**缩略图存 `BYTEA` 而非 base64 text。** base64 会带来 33% 的体积膨胀，而且会被塞进每一次
搜索响应的 JSON 里。存二进制 + 由 `GET /api/photos/{id}/thumb` 单独分发的好处：
Postgres 的 TOAST 机制会把 >2KB 的 bytea 自动移到行外存储，主堆保持紧凑，顺序扫描不受影响；
同时浏览器可以按 ETag 缓存缩略图，搜索响应只带 URL。
256px 长边 WebP q75 约 8~15KB/张，5 万张约 600MB —— 完全在 Postgres 的舒适区内。

**向量索引用 HNSW，不用 IVFFlat。** HNSW 无需预训练、召回率更高、增量插入友好；
IVFFlat 需要有代表性的数据才能建好 list，在数据边灌边查的场景里表现不稳。
需要 pgvector ≥ 0.5，本项目基线是 0.8。

**规模估算（用于确认选型没有过度设计）：** 5 万张照片 × 平均 3 张脸 ≈ 15 万条 512 维向量
≈ 300MB 原始向量。这个量级 pgvector HNSW 单次 KNN 在毫秒级，甚至暴力扫描也能接受。
不需要引入独立向量数据库。

## 5. 幂等与增量

`jobs` 必须能在任意时刻被杀掉再重跑，不产生重复也不丢数据：

- `photo.source_asset_id` 唯一约束，写入走 `ON CONFLICT ... DO UPDATE`。
- `photo.source_checksum` 记录源站给出的 etag/hash（若源站没有，则用「文件大小 + 修改时间」
  的组合作为弱校验）。checksum 未变则跳过，不重新下载、不重新推理。
- `photo.processing_status` ∈ `pending|embedded|failed|skipped`，`processing_error` 存错误摘要。
  失败的照片不阻塞整批，job 结束时汇总报告。
- `source_sync_state` 按 album 记录上次同步游标与时间，支撑增量。
- 源站不再存在的资产标记 `deleted_at`（软删除），不物理删除 —— 便于排查「照片怎么突然搜不到了」。

**磁盘纪律**：原图流式下载到临时文件，提取完人脸和缩略图后**立即删除**。不做原图落盘缓存，
否则磁盘必然被打满。

## 6. 为什么要有 `libs/gallery_core`

`api` 与 `jobs` 是两个独立镜像，但共享：DB 表模型、embedding 服务的 HTTP 客户端、向量归一化
工具、配置读取。

复制两份的失效模式非常隐蔽：某天有人只在 `jobs` 里改了归一化或阈值常量，在线检索仍然「正常
返回结果」，只是结果全错。没有任何测试会红。所以这部分强制共享，通过
`pip install -e ./libs` 装进两个镜像。

## 7. 安全

- **上传接口防护**：文件大小上限（默认 10MB）、真实 MIME 嗅探（不信 `Content-Type`）、
  Pillow 的 decompression bomb 防护（`Image.MAX_IMAGE_PIXELS`）、按 session + IP 双维度限流。
- **EXIF 剥离**：自拍的 EXIF（含 GPS）在进入推理前就丢弃，且本来也不会被持久化。
- **原图鉴权**：`/api/photos/{id}/original` 返回短效签名 URL，不暴露源站裸地址。
  若源站 album 有可见性属性，需在 `album.visibility` 中同步，private album 不进检索库。
- **屏蔽名单**：`block_list` 支持按 person 或 photo 屏蔽，检索时在 SQL 层过滤。

## 8. 可观测性

- `/healthz`（存活）与 `/readyz`（DB 可连 + embedding 服务可达）。
- 结构化 JSON 日志，字段：`request_id`、`route`、`latency_ms`、`faces_detected`、
  `candidates`、`results`。**绝不含图片或向量。**
- `job_run` 表记录每次离线任务：处理数、跳过数、失败数、丢弃的小脸数、耗时。
  这张表是排查「召回率变差」的第一现场。
