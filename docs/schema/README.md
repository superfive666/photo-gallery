# 数据库 Schema

Postgres 16 + [pgvector](https://github.com/pgvector/pgvector) ≥ 0.8。

## 迁移约定

- 文件名 `NNN_<slug>.sql`，三位数字顺序递增。
- **已发布的迁移文件绝不原地修改**，只能新增。改动线上已执行过的文件会让不同环境的
  schema 静默分叉。
- 每个文件包在 `BEGIN; ... COMMIT;` 里，末尾写入 `schema_migrations`。
- DDL 尽量用 `IF NOT EXISTS`，使重复执行无害。
- 执行：`make migrate`。

### 破坏性变更要拆两步

删列、改类型、加 `NOT NULL` 都不能一次做完，因为部署流程是「先迁移、后换代码」：

1. 第一次部署：新增兼容结构，代码双写。
2. 回填数据。
3. 第二次部署：移除旧结构。

## 表清单

| 表 | 作用 | 关键点 |
| --- | --- | --- |
| `schema_migrations` | 迁移版本 | — |
| `photo` | 一张照片/视频一行 | `album` 只属于这里；`photo_url` 唯一 → 幂等；`thumbnail BYTEA` 走 TOAST；软删除 |
| `face` | **向量主表**，一张脸/一个视频时间段一行 | 普通表不分区；`embedding` 上 HNSW cosine 索引；`thumb` 人脸小图（可空）；视频 tracklet 带 `t_start_ms`/`t_end_ms`，照片行为 NULL |
| `block_list` | opt-out | 按 face 或 photo；检索在 SQL 层过滤 |
| `invite_code` | 邀请码 ↔ 相册绑定 | `prefix` 唯一索引定位行 → 单次 argon2 验证；`album` NULL = 全相册；吊销置 `disabled_at` 不删行 |
| `album_sync_state` | 同步进度 | 按 album slug |
| `job_run` | 离线任务记录 + 剪辑域任务队列 | 排查召回率变差的第一现场；005 起 status=queued 由 worker 认领 |
| `search_audit` | 检索留痕 | **只有计数与耗时**，无图片无向量 |
| `invite_code` | 邀请码 ↔ 相册 | 005 起分角色：search 查照片 / edit 剪辑（一码一相册，行 id 即 workspace_id） |
| `media_asset` | 剪辑素材一行 | 原片落盘在 /photo-gallery/media/<album>/；source_url 唯一 → 幂等 |
| `scene` | **剪辑域向量主表** | Chinese-CLIP 图像向量 + 画质列；HNSW cosine；与 face 不同向量空间 |
| `filter_preset` | 滤镜库 | 一切滤镜都是 3D LUT；slug 唯一；软下架不删行 |
| `edit_project` | 一次剪辑任务 | 状态机 + state_version 乐观锁；album 创建时固化 |
| `edit_round` | 反馈闭环留痕 | 每轮用户输入 + shot list 快照 + llm/提示词指纹 |
| `shot` / `shot_candidate` | 镜头与候选 | locked 后绝不重写；rejected 的 scene 后续轮次排除；007 起可带一条备选（backup_candidate_id），随主选一同渲染 |
| `render_output` | 渲染产物 | 精确/含余量两组时码 + 滤镜 slug+checksum，可复现 |
| `project_event` | 事件时间线（聊天窗数据源） | 只追加不改写；无向量无图片字节 |

只有两张主表：

```
photo  ──1:N──▶  face
  id (uuidv7)      id (uuidv7)
  album            photo_id  ──FK──▶ photo.id
  photo_url        embedding vector(512)
  thumbnail
```

**`album` 只放在 `photo` 上。** 一张照片属于哪个相册是照片自己的属性，与它上面有几张脸
无关；`face` 通过 `photo_id` 外键间接得到 album，不冗余存第二份。

**为什么 photo 和 face 必须分表**：一张十人合影产生 1 条 photo + 10 条 face。
把 embedding 挂在 photo 上是从一开始就走不通的。

**没有 person 表，也不做聚类。** 查询完全是实时的：上传自拍 → 取最明显的一张脸 →
对 face 做 KNN。代价是侧脸/背光照片的召回率低于「先聚类再按人取图」的方案，
取舍记录在 [`../plans/0003-drop-partition-and-person.md`](../plans/0003-drop-partition-and-person.md)。

## 主键用 UUIDv7

`gen_random_uuid()`（v4）完全随机，插入位置在 B-tree 里到处跳，批量入库时页分裂严重、
索引膨胀。v7 前 48 位是 Unix 毫秒时间戳，插入始终落在索引右端 —— 离线建库正是
「一次灌很多」的场景。

两个实现，用途不同：

| 实现 | 用途 | 保证 |
| --- | --- | --- |
| SQL `uuid_generate_v7()`（列默认值） | 不指定 id 的插入 | 毫秒粒度有序 |
| Python `gallery_core.uuid7()` | 批量入库时客户端预生成 | **严格单调**（带毫秒内计数器） |

PostgreSQL 18 起有内置 `uuidv7()`。升级后可以新增一个迁移把 DEFAULT 换成原生函数
（不要改 `001_init.sql`）。

## 向量检索的写法

pgvector 的 HNSW 索引**只在 `ORDER BY embedding <=> q LIMIT n` 这个形式下会被用到**。
直接写 `WHERE 1 - (embedding <=> q) >= 阈值` 用不上索引，会退化成全表顺序扫描。

所以检索分两层：内层取候选（走索引），外层过滤。

```sql
WITH candidate AS (
    -- 必须保持 ORDER BY + LIMIT，否则用不上 HNSW
    SELECT f.id AS face_id, f.photo_id,
           1 - (f.embedding <=> CAST(:q AS vector)) AS sim
    FROM face f
    ORDER BY f.embedding <=> CAST(:q AS vector)
    LIMIT :candidates
)
SELECT ph.id, ph.album, ph.photo_url, MAX(c.sim) AS score
FROM candidate c
JOIN photo ph ON ph.id = c.photo_id
WHERE c.sim >= :threshold
  AND ph.deleted_at IS NULL
  AND (:album IS NULL OR ph.album = :album)
  AND NOT EXISTS (SELECT 1 FROM block_list b WHERE b.face_id  = c.face_id)
  AND NOT EXISTS (SELECT 1 FROM block_list b WHERE b.photo_id = ph.id)
GROUP BY ph.id, ph.album, ph.photo_url
ORDER BY score DESC
LIMIT :limit;
```

`<=>` 是 cosine 距离；因为向量已 L2 归一化，`1 - distance` 即 cosine 相似度。

⚠️ **`:candidates`（`SEARCH_CANDIDATES`，默认 500）是召回上限。** 一个人在库里的照片数
超过它就会少返结果。相册过滤发生在取候选之后，所以带 `album` 条件时代码会自动把候选数
放大 4 倍 —— 否则在一个只占全库 5% 的相册里筛选，500 个候选里可能只剩二十几个属于它。

如果日后出现「某人照片特别多，结果被截断」，调 `SEARCH_CANDIDATES` 而不是调阈值。

## 索引调优

HNSW 的查询召回由 `hnsw.ef_search` 控制（默认 40）。召回不足时在事务里调高：

```sql
SET LOCAL hnsw.ef_search = 100;
```

必须用 `SET LOCAL`（事务级）而不是 `SET` —— 后者会污染连接池里的这条连接，
影响之后复用它的所有请求。具体取值由评估集决定，见 [`../evaluation.md`](../evaluation.md)。

## 容量估算

| 项 | 5 万张照片的估算 |
| --- | --- |
| `face` 行数 | ~15 万（平均 3 张脸/图） |
| embedding 原始体积 | 15 万 × 512 × 4B ≈ 300 MB |
| HNSW 索引 | ~1.5× 向量体积 ≈ 450 MB |
| `thumbnail` | 5 万 × ~12KB ≈ 600 MB（TOAST 表） |
| 合计 | ~1.5 GB |

结论：单机 Postgres 完全够用。十万级向量的 KNN 是毫秒级，既不需要独立向量数据库，
也不需要分区。
