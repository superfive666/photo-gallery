# 数据库 Schema

Postgres 16 + [pgvector](https://github.com/pgvector/pgvector) ≥ 0.8。

## 迁移约定

- 文件名 `NNN_<slug>.sql`，三位数字顺序递增。
- **已发布的迁移文件绝不原地修改**，只能新增。改动线上已执行过的文件会让不同环境的
  schema 静默分叉。
- 每个文件包在 `BEGIN; ... COMMIT;` 里，末尾写入 `schema_migrations`。
- DDL 尽量用 `IF NOT EXISTS`，使重复执行无害。
- 执行：`make migrate`（`jobs/migrate.py` 按文件名排序，跳过 `schema_migrations` 中已有的版本）。

### 破坏性变更要拆两步

删列、改类型、加 `NOT NULL` 都不能一次做完，因为部署流程是「先迁移、后换代码」：

1. 第一次部署：新增兼容结构，代码双写。
2. 回填数据。
3. 第二次部署：移除旧结构。

## 表清单

| 表 | 作用 | 关键点 |
| --- | --- | --- |
| `schema_migrations` | 迁移版本 | — |
| `album` | 源站相册 | `visibility != 'public'` 不进检索库 |
| `photo` | 源站资产，一张一行 | `source_asset_id` 唯一 → 幂等；`thumb_webp BYTEA` 走 TOAST；软删除 |
| `face` | 一张脸一行 | `VECTOR(512)` + HNSW cosine；带 `model_name/version/dim` 溯源 |
| `person` | 聚类簇 | `centroid` 为簇内均值再归一化；两段式检索的第一段 |
| `block_list` | opt-out | 按 person 或 photo；检索在 SQL 层过滤 |
| `source_sync_state` | 同步游标 | 支撑增量 |
| `job_run` | 离线任务记录 | 排查召回率变差的第一现场 |
| `search_audit` | 检索留痕 | **只有计数与耗时**，无图片无向量 |

## 常用查询

**两段式检索的第一段（簇心匹配）** —— `:q` 是归一化后的查询向量：

```sql
SELECT p.id, 1 - (p.centroid <=> :q) AS similarity, p.face_count
FROM person p
WHERE NOT EXISTS (SELECT 1 FROM block_list b WHERE b.person_id = p.id)
  AND 1 - (p.centroid <=> :q) >= :person_threshold
ORDER BY p.centroid <=> :q
LIMIT 10;
```

`<=>` 是 cosine 距离；因为向量已 L2 归一化，`1 - distance` 即 cosine 相似度。

**取簇内全部照片并按最佳命中打分**：

```sql
SELECT ph.id, ph.album_id, ph.taken_at,
       MAX(1 - (f.embedding <=> :q)) AS score
FROM face f
JOIN photo ph ON ph.id = f.photo_id
WHERE f.person_id = ANY(:person_ids)
  AND ph.deleted_at IS NULL
  AND NOT EXISTS (SELECT 1 FROM block_list b WHERE b.photo_id = ph.id)
GROUP BY ph.id, ph.album_id, ph.taken_at
ORDER BY score DESC
LIMIT :limit;
```

## 索引调优

HNSW 的查询召回由 `hnsw.ef_search` 控制（默认 40）。召回不足时在 session 里调高：

```sql
SET LOCAL hnsw.ef_search = 100;
```

代价是延迟上升。在本项目的数据量级（十万级向量）下，设到 100~200 仍是毫秒级，
值得为召回率买单。具体取值由评估集决定，见 [`../evaluation.md`](../evaluation.md)。

## 容量估算

| 项 | 5 万张照片的估算 |
| --- | --- |
| `face` 行数 | ~15 万（平均 3 张脸/图） |
| embedding 原始体积 | 15 万 × 512 × 4B ≈ 300 MB |
| HNSW 索引 | ~1.5× 向量体积 ≈ 450 MB |
| `thumb_webp` | 5 万 × ~12KB ≈ 600 MB（TOAST 表） |
| 合计 | ~1.5 GB |

结论：单机 Postgres 完全够用，不需要引入独立向量数据库。
