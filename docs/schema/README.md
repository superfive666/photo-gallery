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
| `photo` | 一张照片/视频一行 | `photo_url` 唯一 → 幂等；`thumbnail BYTEA` 走 TOAST；软删除 |
| `face` | **向量主表**，一张脸一行 | **按 `album` LIST 分区**；PK `(album, id)`；HNSW cosine |
| `person` | 聚类簇 | 不分区（跨相册）；`centroid` 是两段式检索的第一段 |
| `block_list` | opt-out | 按 person 或 photo；检索在 SQL 层过滤 |
| `album_sync_state` | 同步进度 | 按 album slug |
| `job_run` | 离线任务记录 | 排查召回率变差的第一现场 |
| `search_audit` | 检索留痕 | **只有计数与耗时**，无图片无向量 |

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

## `face` 的分区

```sql
CREATE TABLE face (...) PARTITION BY LIST (album);
CREATE TABLE face_default PARTITION OF face DEFAULT;
```

新相册的分区由 `jobs` 在处理该相册的第一张照片之前调用
`SELECT ensure_face_partition('2026-08-10')` 创建，幂等且并发安全。分区名是
「净化后的 slug 前缀 + md5 短哈希」——`album` 允许 200 字符，而 PG 标识符上限 63 字节。

### 三个必须知道的约束

**1. 主键必须包含分区键。** 所以是 `PRIMARY KEY (album, id)` 而不是 `(id)`。
后果：`face.id` 的全局唯一性不由数据库保证（只保证 `(album, id)` 唯一）。
uuidv7 碰撞概率可忽略，且代码里从不用「只给 id 不给 album」查 face。

**2. 所有针对 face 的 DML 都要带上 `album`**，否则会扫描/更新全部分区：

```sql
-- ✅ 裁剪到单分区
DELETE FROM face WHERE album = :album AND photo_id = ANY(...);
UPDATE face SET person_id = :pid WHERE album = :album AND id = ANY(...);

-- ❌ 扫全部分区
DELETE FROM face WHERE photo_id = ANY(...);
```

`pipeline.py` 和 `cluster.py` 里都有对应注释，改动时别把 `album` 条件删掉。

**3. 先建分区、再插数据。** 如果某个 album 的行先落进了 `face_default`，之后再为它
建专属分区，Postgres 会拒绝（它要扫 DEFAULT 分区并确认没有冲突行）。
真的发生了的恢复办法：

```sql
BEGIN;
-- 1. 把该 album 的行搬出 DEFAULT
CREATE TEMP TABLE moved AS
  SELECT * FROM face WHERE album = '2026-08-10';
DELETE FROM face WHERE album = '2026-08-10';
-- 2. 建分区
SELECT ensure_face_partition('2026-08-10');
-- 3. 搬回去
INSERT INTO face SELECT * FROM moved;
COMMIT;
```

### 分区的代价

⚠️ **分区只让「限定相册」的检索变快，让「全库」的检索变慢。**

- `WHERE album = '2026-08-10'` → 裁剪到单分区，精确且快。这是 `/api/albums`
  筛选器存在的原因。
- 不带 album（主流程：上传自拍找所有活动的照片）→ 无法裁剪，Postgres 要对**每个分区各做
  一次 HNSW 索引扫描**再 MergeAppend，还要承担分区数量带来的规划开销。

在本项目的量级（十万级人脸、百级相册）这个开销大约是几十毫秒，可以接受。
但它随相册数量线性增长，**相册数到千级时需要重新评估**。届时的两条退出路径：

1. **去掉分区**：`face` 改回普通表，单个 HNSW 索引。这个数据量级本来就不需要分区，
   全库检索会变快，只有 album 筛选退化成「先 ANN 再按 album 过滤」。
2. **改成 RANGE 按年分区**：`album` slug 是日期，字典序即时间序（列已声明
   `COLLATE "C"`，所以这一点是确定的）。分区数从 N_相册 降到 N_年，全库检索的
   MergeAppend 分支大幅减少；代价是 album 筛选变成分区内的后过滤。

两条路都只需要一次新增迁移 + 一次数据搬迁，不影响应用层代码（除了可以删掉那些
`album =` 条件）。

## 常用查询

**两段式检索的第一段（簇心匹配）** —— `:q` 是归一化后的查询向量：

```sql
SELECT p.id, 1 - (p.centroid <=> CAST(:q AS vector)) AS similarity
FROM person p
WHERE NOT EXISTS (SELECT 1 FROM block_list b WHERE b.person_id = p.id)
  AND 1 - (p.centroid <=> CAST(:q AS vector)) >= :threshold
ORDER BY p.centroid <=> CAST(:q AS vector)
LIMIT 10;
```

`<=>` 是 cosine 距离；因为向量已 L2 归一化，`1 - distance` 即 cosine 相似度。

**取簇内全部照片并按最佳命中打分**（注意 album 条件的写法）：

```sql
SELECT ph.id, ph.album, MAX(1 - (f.embedding <=> CAST(:q AS vector))) AS score
FROM face f
JOIN photo ph ON ph.id = f.photo_id
WHERE f.person_id = ANY(CAST(:person_ids AS uuid[]))
  -- 这个写法能被运行时分区裁剪；写成 f.album = COALESCE(:album, f.album) 就不行
  AND (:album IS NULL OR f.album = :album)
  AND ph.deleted_at IS NULL
GROUP BY ph.id, ph.album
ORDER BY score DESC
LIMIT :limit;
```

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

结论：单机 Postgres 完全够用，不需要引入独立向量数据库。
