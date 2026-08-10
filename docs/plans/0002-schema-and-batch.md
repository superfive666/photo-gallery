# 0002 — 数据模型定稿与批量 embedding

- **状态**：已实现，待用真实数据验证
- **日期**：2026-08-10
- **前置**：[0001-mvp](0001-mvp.md)

## 触发

拿到了源站的确切信息与数据模型要求：

1. photos.zrc.sg 是**公开、无需鉴权**的跑团照片墙，相册地址
   `photos.zrc.sg/album/<slug>`（如 `/album/2026-08-10`）。
2. 主键要 UUIDv7、自动生成。
3. `album` 是 ≤200 字符的 URL friendly 字段；在 photo 表上做索引，
   在向量表上**做分区**，用于把向量检索收窄到单个相册。
4. 缩略图：源站已提供就直接转字节入库，没有才本地生成。
5. 离线向量化要支持**批量**，以更好利用 GPU。

## 改了什么

### 数据模型

- `album` 表**删除**。album 就是 URL 里的 slug，没有额外元数据，
  也不再需要 `visibility`（源站公开，没有 private 相册）。
- `photo`：`id`（uuidv7）/ `album` / `photo_url` / `thumbnail` 四个业务字段，
  加上运维必需的 `kind` / `processing_status` / `face_count` / `faces_discarded` /
  `deleted_at` 等。幂等键从 `source_asset_id` 改为 `photo_url`。
- `face`（向量主表）：`id` / `photo_id` / `album` / `embedding`，
  加上质量门控与诊断所需的 bbox、`det_score`、`face_px`，以及模型溯源三列。
  **按 `album` 做 LIST 分区**，PK 因此是 `(album, id)`。
- 新增 SQL 函数 `uuid_generate_v7()`（pg16/17 可用；pg18 有原生 `uuidv7()`）
  与 `ensure_face_partition(album)`（幂等、并发安全地建分区）。
- 新增 Python 侧 `gallery_core.uuid7()`，带毫秒内计数器，**严格单调** ——
  批量入库时在客户端预生成 id，省掉逐张 RETURNING 往返。
- `album` 列显式 `COLLATE "C"`，让分区路由与 locale 无关、可预测。

`001_init.sql` 是**原地重写**而非新增 `002`。这是「DDL 只追加」规则的一次性例外：
该迁移从未在任何数据库上执行过，写一个 002 去改一个不存在的 schema 只会留下噪音。
一旦首次部署完成，这条例外即失效。

### 批量 embedding

- `embedding` 服务新增 `POST /extract/batch`。
- **识别做批量、检测不做批量**：ArcFace 对每张脸各跑一次前向，合影里人脸多，
  识别是计算量主体；把整批照片的对齐人脸拼成一次前向是 GPU 收益最大的一处。
  SCRFD 的前后处理在 insightface 内部按单图实现，重写它风险大于收益，改为多线程并发。
- **批量能力靠探测而非假设**：启动时检查识别模型 ONNX 输入的 batch 维，
  被固定成 1 时退化为逐张并在 `/healthz` 报 `batch_supported: false`。
- GPU 通过构建参数 `ORT_PACKAGE=onnxruntime-gpu` + `EMBEDDING_USE_GPU=true` 启用。
- `jobs` 改为批量流水线，批大小同时受张数与字节数约束。
  原图字节留在内存中、一批处理完即释放，**不落盘**（原先的 tmpfs 临时文件方案去掉了）。

### 其他

- 新增 `POST /api/search` 的可选 `album` 参数与 `GET /api/albums`，
  前端加相册筛选器 —— 选定相册的检索会走分区裁剪。
- 新增 `jobs probe`：对着真站跑一次解析并打印看到了什么，不写库。
- 缩略图优先用源站提供的字节（`SourceAsset.thumbnail_url`）。
- 去掉签名链接：源站公开，签名保护的东西不存在。`/original` 直接 302。

## 非范围

- **视频**仍然不处理（只登记，用于统计占比）。
- **相册页解析仍是通用实现**，等 `make probe` 的真实输出后再收敛成精确选择器。
- 阈值仍是文献占位值，未标定。

## 验收标准

| # | 内容 | 怎么验 |
| --- | --- | --- |
| 1 | 迁移能跑 | `make migrate` 成功，`\d+ face` 显示 LIST 分区与 `face_default` |
| 2 | 分区自动创建 | ingest 一个新 album 后，`\dt face_p_*` 出现对应分区 |
| 3 | 批量真的生效 | `/healthz` 的 `batch_supported: true`；`extract_batch_done` 日志里 `per_image_ms` 明显低于单张 |
| 4 | 幂等 | 同一 album 连跑两次 ingest，`face` 行数不变 |
| 5 | album 裁剪 | `EXPLAIN` 带 `album =` 的检索，只出现一个分区的扫描 |
| 6 | uuidv7 单调 | `test_uuid7.py` 通过（含 5000 个同毫秒内有序） |
| 7 | 源站可解析 | `make probe ALBUM=2026-08-10` 返回非零 assets |

## 风险

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| **分区让全库检索变慢** | 主流程延迟上升 | 已量化并记录退出路径，见 `docs/schema/README.md`「分区的代价」。相册数到千级时重新评估 |
| 识别模型 batch 维固定为 1 | 批量无收益 | 启动探测 + `/healthz` 上报 + ingest 主动提示 |
| 行先落进 `face_default` | 之后无法为该 album 建分区 | pipeline 强制「先建分区、再插数据」；恢复 SQL 已写进 schema README |
| 通用 HTML 解析对不上真站 | ingest 拿到 0 条 | `jobs probe` 一条命令定位，不需要改代码试错 |
| 批量把内存顶穿 | 容器 OOM | 张数 + 字节双上限；compose 里 embedding 限 8g |

## 下一步

1. `make probe ALBUM=2026-08-10` → 收敛相册页解析。
2. 真实数据跑一次 `make migrate && make ingest && make cluster`，验证上面 7 条。
3. 建评估集，标定阈值（M7）。
