# 架构设计

## 1. 全景

```
                    ┌──────────────────────────────────────────────┐
                    │            photos.zrc.sg (源站)              │
                    │   公开、无鉴权的静态照片墙，只读访问          │
                    │   /album/<slug>  例：/album/2026-08-10        │
                    └──────────────────┬───────────────────────────┘
                                       │ ① 按 album 拉取（限速、并发受控）
                                       ▼
┌─────────┐  批量HTTP  ┌──────────────────────────┐  单张HTTP  ┌──────────────────┐
│  jobs   │───────────▶│    embedding service     │◀──────────│       api        │
│ 离线建库 │ /extract   │  InsightFace buffalo_l    │ /extract  │ 鉴权/检索/缩略图  │
│  批量   │   /batch   │  整批人脸拼一次识别前向    │           └────┬─────────────┘
└────┬────┘            │  512-d, L2 normalized     │                │
     │                 └──────────────────────────┘                │
     │  ② 落库                                      ③ KNN 检索      │
     ▼                                                              ▼
   ┌────────────────────────────────────────────────────────────────┐
   │              Postgres 16 + pgvector (HNSW, cosine)             │
   │   photo (album 索引)   face (按 album LIST 分区)   person       │
   │   block_list  album_sync_state  job_run  search_audit          │
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

**唯一**允许做人脸检测与 embedding 的地方。三个接口：

| 接口 | 用途 | 优化目标 |
| --- | --- | --- |
| `POST /extract` | 在线检索的单张自拍 | 延迟 |
| `POST /extract/batch` | 离线建库的一批照片 | 吞吐 / GPU 利用率 |
| `GET /healthz` | 模型状态、GPU 开关、批量能力、批量上限 | — |

设计要点：

- 模型在进程启动时加载一次并常驻内存，绝不按请求加载。
- 推理是同步的密集调用，**必须**在 threadpool 中执行，否则会阻塞 asyncio event loop，
  让整个服务在并发下卡死。
- 服务本身单 worker（模型一份，内存可控），靠 threadpool 提供并发；不够时横向加实例。
- 出口统一做 L2 归一化，下游拿到的向量可以直接 cosine 比较。
- 模型文件在**构建镜像时**下载打包进镜像，不在运行时拉 —— 避免冷启动依赖外网。

**为什么独立成容器**：`api` 和 `jobs` 共用同一份推理逻辑，从物理上保证离线库与在线查询
落在同一个向量空间；模型换版本时只重建这一个镜像；可以单独限制它的 CPU/内存/GPU 配额。

### 批量 embedding 怎么切分

一次推理有两段：检测（SCRFD `det_10g`）和识别（ArcFace `w600k_r50`）。

**识别做批量。** ArcFace 对每一张脸各跑一次前向。跑团合影动辄十几个人，识别的总计算量
因此远超检测。把一整批照片里所有对齐后的人脸拼成一个 batch 丢进一次前向，是 GPU 利用率
提升最大的一处。

**检测不做批量。** SCRFD 的前后处理（尺度归一、anchor 解码、NMS）都在 insightface 内部
按单图实现。要批量就得把这段重写一遍 —— 那是在服务里复制一份预处理逻辑，跨 insightface
版本极易出错，收益又小于识别侧。改为多线程并发跑检测：ORT 的 `session.run` 会释放 GIL。

**批量能力是探测出来的，不是假设的。** `w600k_r50.onnx` 通常导出为动态 batch，但如果
被固定成 1，批量前向会直接报形状错误。服务启动时检查识别模型输入的 batch 维，
不支持则退化为逐张前向并在 `/healthz` 里报 `batch_supported: false`
（`jobs ingest` 会把这一点提示出来，因为它意味着 GPU 利用率上不去）。

**批大小受两个上限约束。** 只限张数的话，一批高像素原图会直接顶穿内存
（每张解码后是 W×H×3 字节）。所以 `jobs` 同时看 `INGEST_BATCH_IMAGES` 和
`INGEST_BATCH_MAX_BYTES`，服务端还有 `MAX_BATCH_IMAGES` 兜底；实际批大小取三者最小。

### `api` — 业务服务

- 邀请码换取签名 session cookie（HttpOnly / Secure / SameSite=Lax），JWT，短有效期。
- `POST /api/search` — multipart 上传 1~3 张自拍 + 可选 `album`，同步返回匹配结果。
- `GET /api/albums` — 有可检索人脸的相册列表，供前端筛选器使用。
- `GET /api/photos/{id}/thumb` — 输出缩略图，带 ETag 与长缓存。
- `GET /api/photos/{id}/original` — 302 到源站原图。
- 不加载任何模型，镜像轻量，可自由扩容。

### `jobs` — 离线建库

一次性容器，`docker compose run` 或 cron 触发：

```bash
python -m jobs migrate                          # 执行未应用的迁移
python -m jobs probe  --album 2026-08-10        # 探查源站页面结构，不写库
python -m jobs ingest --album 2026-08-10        # 批量拉取 + 提取 + 落库
python -m jobs cluster                          # 人脸聚类成 person
python -m jobs eval   --sweep                   # 阈值标定
python -m jobs block  --person <uuid>           # opt-out
```

### `web` — 前端

Vite 构建为纯静态产物，nginx 托管并反代 `/api` 到 `api` 容器。移动端优先。

## 3. 检索算法

朴素做法是「自拍向量 vs 每一条 face 向量取 top-k」。本项目不这么做，因为一个人的侧脸/背光
照片和其正脸自拍的 cosine 距离往往超过阈值，会漏掉大量本该命中的照片。

改为**两段式：先认人，再取图**。

```
① 离线：对全库 face embedding 做 DBSCAN 聚类 → person 簇
        每个簇记录 centroid（成员向量均值再归一化）与成员数

② 在线：
   a. 自拍 → embedding（多张自拍则取均值后重新归一化，等价于更稳的查询点）
   b. 对 person.centroid 做 KNN，取相似度 ≥ PERSON_MATCH_THRESHOLD 的候选簇
   c. 对 face.embedding 做 KNN，取相似度 ≥ FACE_MATCH_THRESHOLD 的直接命中脸
   d. 合并：候选簇内的全部 face → 其所属 photo；并上 (c) 的直接命中
   e. 每张 photo 的得分 = 该照片上所有命中脸的最高相似度
   f. 按得分降序返回，附 album 与缩略图 URL
```

`(c)` 这一路是给聚类失败的人脸（噪声点、只出现过一次的人）留的兜底，不能省。

**为什么簇心投票有效**：簇内包含同一人的正脸、侧脸、不同光照的多个样本，簇心比任何单张
照片都更接近这个人的「平均长相」，因此对查询自拍的角度差异更鲁棒。

### album 过滤与分区

`face` 按 `album` 做 LIST 分区，所以带 `album =` 条件的检索会被裁剪到单个分区 ——
只在那一个相册里做向量检索，又快又精确。这是前端相册筛选器的实现基础。

不带 album 的全库检索（主流程）无法裁剪，需要对每个分区各做一次 HNSW 索引扫描再
MergeAppend。在本项目量级下是几十毫秒的代价，但随相册数量线性增长。
**权衡与退出路径见 [`schema/README.md`](schema/README.md#分区的代价)。**

### 质量门控

在入库阶段就丢掉不可靠的人脸，比在检索阶段调阈值有效得多：

- `det_score < MIN_DET_SCORE`（默认 0.5）→ 丢弃
- 人脸框短边 `< MIN_FACE_PX`（默认 40px）→ 丢弃（这是「后排的人搜不到」的根因）
- 极端长宽比（>2.5，通常是误检）→ 丢弃
- 检测器没给出关键点 → 丢弃（无法仿射对齐，未对齐的裁剪送进 ArcFace 会得到偏移的向量）

被丢弃的人脸计入 `photo.faces_discarded` 与 `job_run.stats`，否则「召回低」会无从排查。

## 4. 数据模型要点

完整 DDL 见 [`schema/001_init.sql`](schema/001_init.sql)，约束与查询范式见
[`schema/README.md`](schema/README.md)。几个关键决策：

**`photo` 与 `face` 必须分表。** 一张十人合影产生 1 条 photo + 10 条 face。
把 embedding 挂在 photo 上是从一开始就走不通的。

**`album` 是一个 slug 字符串，不是外键。** 源站的 album 本身就是 URL 里那一段
（`/album/2026-08-10`），没有额外元数据，所以不需要 album 表。`face` 上冗余存一份 ——
这是分区的前提，分区键必须在本表上。

**主键用 UUIDv7 而非 v4。** v4 完全随机，批量入库时 B-tree 页分裂严重；v7 前 48 位是
毫秒时间戳，插入始终落在索引右端。

**缩略图存 `BYTEA` 而非 base64。** base64 有 33% 体积膨胀，而且会被塞进每次搜索响应的
JSON 里。存二进制 + 由 `/api/photos/{id}/thumb` 单独分发的好处：Postgres 的 TOAST 会把
>2KB 的值自动移到行外存储，主堆保持紧凑；浏览器可按 ETag 缓存。
**源站已提供缩略图就直接落它的字节**，省一次本地重编码；没有才本地生成 256px WebP。

**向量索引用 HNSW，不用 IVFFlat。** HNSW 无需预训练、召回率更高、增量插入友好。

**规模估算：** 5 万张照片 × 平均 3 张脸 ≈ 15 万条 512 维向量 ≈ 300MB 原始向量。
这个量级 pgvector HNSW 单次 KNN 在毫秒级。不需要引入独立向量数据库 ——
说实话也不需要分区，分区是为了 album 级检索的精确性。

## 5. 幂等与增量

`jobs` 必须能在任意时刻被杀掉再重跑，不产生重复也不丢数据：

- `photo.photo_url` 唯一约束，写入走 `ON CONFLICT (photo_url) DO UPDATE`。
  静态站点的 URL 稳定且天然唯一，不需要再造一个 source_asset_id。
- 增量策略：**该 URL 成功入库过（`processing_status = 'embedded'`）就跳过**。
  对追加式照片墙够用；同一 URL 内容被替换的情况检测不到，需要时用 `--full`。
- 写新人脸前先按 `(album, photo_id)` 删旧人脸 —— 保证重跑不累积重复向量。
- `photo.processing_status` ∈ `pending|embedded|failed|skipped`，`processing_error`
  存错误摘要。单张失败不阻塞整批，下次运行自动重试，job 结束时汇总报告。
- 静态相册页一次给出完整清单，所以「没见到」即「源站已删除」→ 标记 `deleted_at`
  （软删除，便于排查「照片怎么突然搜不到了」）。
- `album_sync_state` 记录上次同步时间，记的是**本轮开始**的时间而不是结束时间 ——
  否则运行期间新增的照片会落进「已同步」的窗口里被永久跳过。

**内存纪律**：批量推理需要把字节 POST 给 embedding 服务，所以一批的原图字节留在内存中，
处理完立即释放。**不落盘、不做原图缓存** —— 落盘只是多一次往返，还会给「不缓存原图」
这条纪律留缺口。字节预算由 `INGEST_BATCH_MAX_BYTES` 兜住。

## 6. 为什么要有 `libs/gallery_core`

`api` 与 `jobs` 是两个独立镜像，但共享：DB 表模型、embedding 服务的 HTTP 客户端、
向量归一化工具、UUIDv7 生成、配置读取。

复制两份的失效模式非常隐蔽：某天有人只在 `jobs` 里改了归一化或阈值常量，在线检索仍然
「正常返回结果」，只是结果全错。没有任何测试会红。所以这部分强制共享，通过
`pip install -e ./libs` 装进两个镜像。

同理，`jobs/eval.py` 直接 import `api.app.services.search` 里那份**真实的**检索函数 ——
评估必须走线上代码路径，否则测出来的指标和线上行为无关。

## 7. 安全

源站是公开的，所以这里的安全边界和一般图库项目不同：

**不需要的**：原图签名链接。签名保护的是源站访问控制，而源站没有访问控制可绕过。
`/original` 直接 302 到 `photo_url`；保留这一跳只是为了不把源站 URL 结构写进前端。

**仍然需要的**：

- **邀请码。** 人脸检索创造了一个源站本身没有的能力 —— 拿一张某人的照片就能把他在所有
  活动里的照片一次性聚齐。见 [`privacy.md`](privacy.md)。
- **上传接口防护**：文件大小上限、真实 MIME 嗅探（不信 `Content-Type`）、
  Pillow 的 decompression bomb 防护、按 session + IP 双维度限流。
- **EXIF 剥离**：自拍的 EXIF（含 GPS）在进入推理前就丢弃，且本来也不会被持久化。
- **屏蔽名单**：`block_list` 支持按 person 或 photo 屏蔽，在 SQL 层过滤。

## 8. 可观测性

- `/healthz`（存活）与 `/readyz`（DB 可连 + embedding 服务可达）。
- 结构化 JSON 日志，字段：`request_id`、`route`、`latency_ms`、`faces_detected`、
  `results`。**绝不含图片或向量**（`gallery_core.logging` 里有兜底过滤，但那是补救
  措施不是许可）。
- `job_run` 表记录每次离线任务：处理数、跳过数、失败数、丢弃的小脸数、批次数、
  下载字节数、耗时。这张表是排查「召回率变差」的第一现场。
- `/healthz` 上的 `batch_supported` 与 `gpu` 用于确认批量推理真的生效了。
