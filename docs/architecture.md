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
   │   photo (id/album/photo_url/thumbnail)  ──1:N──▶  face          │
   │   block_list   album_sync_state   job_run   search_audit       │
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
| `POST /extract` | 在线检索的单张自拍（`primary_only` 只取最明显的一张脸） | 延迟 |
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
- `POST /api/search` — multipart 上传 1~3 张自拍 + 可选 `album`，同步返回匹配结果，
  响应里带 `selfie_discarded: true` 供前端向用户确认。
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
python -m jobs eval   --sweep                   # 阈值标定
python -m jobs block  --selfie me.jpg           # opt-out：屏蔽此人的全部人脸
```

### `web` — 前端

Vite 构建为纯静态产物，nginx 托管并反代 `/api` 到 `api` 容器。移动端优先。

## 3. 检索算法

单段式，全部实时完成：

```
① 上传 1~3 张自拍
② 每张只取**最明显的一张脸**（面积最大者），筛选在 embedding 服务端完成
③ 多张则取均值后重新归一化 → 单一查询向量
④ 对 face.embedding 做 KNN 取候选（走 HNSW 索引），按阈值过滤
⑤ 命中的人脸映射回照片，每张照片取其上所有命中人脸的最高相似度作为得分
⑥ 按得分降序返回，附 album 与缩略图 URL
⑦ 自拍字节在请求返回时失去引用，响应里显式告知用户「已删除」
```

**为什么每张自拍只取一张脸**：用户要找的是自己。背景里的路人如果也被向量化并参与
匹配，会把别人的照片混进结果里。筛选放在 embedding 服务内、识别前向**之前** ——
其余人脸根本不会被向量化，既省算力，也让离开该服务的人脸数据最少化。

「最明显」的判据是人脸框面积，`det_score` 作为并列时的次序。刻意不引入更复杂的打分
（清晰度、居中程度）：这个选择直接决定查询结果，规则越简单越容易在出问题时说清原因。

**多张自拍取均值**是目前唯一的召回率提升手段（库里不做聚类），多角度的均值是一个更
中性、对侧脸更宽容的查询点。所以 UI 会主动鼓励用户多传一张，但绝不强制。

### 不做 person 聚类的代价

一个人的侧脸/背光照片与其正脸自拍的 cosine 距离常常超过阈值。先聚类再按人取图能让
侧脸经由同簇的正脸被间接命中；单段式没有这条路径，**这类照片的召回率会更低**。

这是有意的取舍：不落任何长期的人物身份数据，查询完全实时。已写进 README 的已知局限，
不要当成 bug 排查。

### 候选数是召回上限

pgvector 的 HNSW 只在 `ORDER BY ... LIMIT n` 形式下会被用到，所以检索先取 N 个最近邻
候选、再按阈值过滤。`SEARCH_CANDIDATES`（默认 500）因此是硬上限：某人照片数超过它就会
被截断。带相册过滤时代码会自动放大 4 倍（过滤发生在取候选之后）。
细节见 [`schema/README.md`](schema/README.md#向量检索的写法)。

### album 过滤

`photo.album` 上有索引，检索 SQL 里带 `ph.album = :album` 即可。这是普通过滤，
不是分区裁剪 —— 好处是全库检索（主流程）不受任何分区归并开销影响。

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

**`album` 是一个 slug 字符串，不是外键，只放在 `photo` 上。** 源站的 album 本身就是
URL 里那一段（`/album/2026-08-10`），没有额外元数据，所以不需要 album 表。
`face` 通过 `photo_id` 外键间接得到 album，不冗余存第二份。

**主键用 UUIDv7 而非 v4。** v4 完全随机，批量入库时 B-tree 页分裂严重；v7 前 48 位是
毫秒时间戳，插入始终落在索引右端。

**缩略图存 `BYTEA` 而非 base64。** base64 有 33% 体积膨胀，而且会被塞进每次搜索响应的
JSON 里。存二进制 + 由 `/api/photos/{id}/thumb` 单独分发的好处：Postgres 的 TOAST 会把
>2KB 的值自动移到行外存储，主堆保持紧凑；浏览器可按 ETag 缓存。
**源站已提供缩略图就直接落它的字节**，省一次本地重编码；没有才本地生成 256px WebP。

**向量索引用 HNSW，不用 IVFFlat。** HNSW 无需预训练、召回率更高、增量插入友好。

**`face` 是普通表，不分区。** 5 万张照片 × 平均 3 张脸 ≈ 15 万条 512 维向量。
这个量级 pgvector HNSW 单次 KNN 在毫秒级；按 album 分区只会让全库检索（主流程）
变成对 N 个分区各扫一次再归并。取舍过程见
[`plans/0003`](plans/0003-drop-partition-and-person.md)。

## 5. 幂等与增量

`jobs` 必须能在任意时刻被杀掉再重跑，不产生重复也不丢数据：

- `photo.photo_url` 唯一约束，写入走 `ON CONFLICT (photo_url) DO UPDATE`。
  静态站点的 URL 稳定且天然唯一，不需要再造一个 source_asset_id。
- 增量策略：**该 URL 成功入库过（`processing_status = 'embedded'`）就跳过**。
  对追加式照片墙够用；同一 URL 内容被替换的情况检测不到，需要时用 `--full`。
- 写新人脸前先按 `photo_id` 删旧人脸 —— 保证重跑不累积重复向量。
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
「正常返回结果」，只是结果全错。没有任何测试会红。所以这部分强制共享。

依赖用 **uv workspace** 管理：根 `pyproject.toml` 是 workspace 根，`libs`/`api`/`jobs`/
`embedding` 四个成员共用**一份 `uv.lock`**。这一份锁文件是关键 —— 三个服务共享
sqlalchemy / pydantic / numpy 等一大堆依赖，共用一份锁才能保证它们在所有镜像里解析到
完全相同的版本。否则 api 与 jobs 各自解析出不同的 numpy，向量的行为差异同样会以
「检索结果不对」的形式出现。

`gallery-core` 是四个成员里唯一会被构建安装的包；另外三个是 `package = false` 的虚拟
成员（uv 只锁定并安装它们的依赖，代码按源码目录导入）。DB 栈放在 `gallery-core[db]`
extra 里，所以 embedding 镜像里没有 sqlalchemy / asyncpg / pgvector。

同理，`jobs/eval.py` 直接 import `api.app.services.search` 里那份**真实的**检索函数 ——
评估必须走线上代码路径，否则测出来的指标和线上行为无关。

## 7. 安全

源站是公开的，所以这里的安全边界和一般图库项目不同：

**不需要的**：原图签名链接。签名保护的是源站访问控制，而源站没有访问控制可绕过。
`/original` 直接 302 到 `photo_url`；保留这一跳只是为了不把源站 URL 结构写进前端。

**仍然需要的**：

- **邀请码。** 人脸检索创造了一个源站本身没有的能力 —— 拿一张某人的照片就能把他在所有
  活动里的照片一次性聚齐。见 [`privacy.md`](privacy.md)。
- **查询自拍零留存，并且让用户看见。** 字节只在请求生命周期内存在，响应里的
  `selfie_discarded` 让这个承诺在界面上可验证，而不只是文档里的一句话。
- **上传接口防护**：文件大小上限、真实 MIME 嗅探（不信 `Content-Type`）、
  Pillow 的 decompression bomb 防护、按 session + IP 双维度限流。
- **EXIF 剥离**：自拍的 EXIF（含 GPS）在进入推理前就丢弃，且本来也不会被持久化。
- **屏蔽名单**：`block_list` 支持按 face 或 photo 屏蔽，在 SQL 层过滤。
  没有 person 表，所以「屏蔽某个人」= 屏蔽他的那一批 face，用
  `jobs block --selfie <路径>` 一条命令完成。

## 8. 可观测性

- `/healthz`（存活）与 `/readyz`（DB 可连 + embedding 服务可达）。
- 结构化 JSON 日志，字段：`request_id`、`route`、`latency_ms`、`faces_detected`、
  `results`。**绝不含图片或向量**（`gallery_core.logging` 里有兜底过滤，但那是补救
  措施不是许可）。
- `job_run` 表记录每次离线任务：处理数、跳过数、失败数、丢弃的小脸数、批次数、
  下载字节数、耗时。这张表是排查「召回率变差」的第一现场。
- `search_audit` 的 `candidate_count` 记录本次取了多少候选 —— 结果数逼近它时，
  说明是候选数在截断召回，该调 `SEARCH_CANDIDATES` 而不是阈值。
- `/healthz` 上的 `batch_supported` 与 `gpu` 用于确认批量推理真的生效了。
