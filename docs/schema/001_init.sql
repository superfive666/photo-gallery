-- 001_init.sql — 初始 schema
--
-- 约定：迁移文件只以追加方式演进，已发布的文件绝不原地修改。
-- 所有 DDL 用 IF NOT EXISTS，保证重复执行安全（迁移执行器也会跳过已应用的版本）。

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_bytes()

-- ---------------------------------------------------------------------------
-- 迁移版本表
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT        PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- UUIDv7 生成器
--
-- 为什么不用 gen_random_uuid()（v4）：v4 完全随机，插入位置在 B-tree 里到处跳，
-- 大批量入库时页分裂严重、缓存命中率低、索引膨胀。v7 前 48 位是 Unix 毫秒时间戳，
-- 天然按时间递增，插入始终落在索引右端 —— 离线建库是「一次灌很多」的场景，
-- 这个差别很实际。
--
-- PostgreSQL 18 起内置 uuidv7()。这里自己实现一份，使 pg16/17 也能用，
-- 且不必因为 Postgres 版本而分叉 schema。升到 18 之后可以把 DEFAULT 换成原生函数
-- （新增一个迁移，不要改本文件）。
--
-- 注意：这个实现只保证**毫秒粒度**有序，同一毫秒内生成的 id 之间是随机序 ——
-- 对 B-tree 局部性来说够了（乱序只发生在索引右端极小的一块）。需要严格单调时
-- 用 Python 侧的 gallery_core.uuid7（它带毫秒内计数器）在客户端生成。
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION uuid_generate_v7()
RETURNS UUID
LANGUAGE plpgsql
VOLATILE PARALLEL SAFE
AS $$
DECLARE
    ts_ms BIGINT := (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::BIGINT;
    raw   BYTEA;
BEGIN
    -- 前 6 字节 = Unix 毫秒时间戳（大端），后 10 字节随机
    raw := substring(int8send(ts_ms) FROM 3 FOR 6) || gen_random_bytes(10);
    -- 第 7 字节高 4 位 = 版本号 7（RFC 9562）
    raw := set_byte(raw, 6, (get_byte(raw, 6) & 15) | 112);
    -- 第 9 字节高 2 位 = 变体 0b10
    raw := set_byte(raw, 8, (get_byte(raw, 8) & 63) | 128);
    RETURN encode(raw, 'hex')::UUID;
END;
$$;

-- ---------------------------------------------------------------------------
-- photo — 源站的一张照片/一个视频
--
-- album 只属于 photo：一张照片属于哪个相册是照片自己的属性，与它上面有几张脸无关。
-- face 通过 photo_id 外键间接得到 album，不再冗余存一份。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS photo (
    id                  UUID            PRIMARY KEY DEFAULT uuid_generate_v7(),

    -- URL friendly 的相册标识，形如 '2026-08-10'（对应 photos.zrc.sg/album/2026-08-10）
    album               VARCHAR(200)    NOT NULL,

    -- 原图（original quality）的完整下载地址。源站公开无鉴权，可直接给前端用。
    -- 同时作为幂等写入的唯一键：ON CONFLICT (photo_url) DO UPDATE。
    photo_url           TEXT            NOT NULL UNIQUE,

    -- 缩略图字节。源站已提供缩略图则直接落其字节；没有则由 jobs/thumbnails.py 生成。
    -- 存 BYTEA 而非 base64：省 33% 体积，且 >2KB 会被 TOAST 自动移到行外存储，
    -- 主堆保持紧凑。由 GET /api/photos/{id}/thumb 单独分发并按 ETag 缓存。
    thumbnail           BYTEA,
    thumbnail_width     INTEGER,
    thumbnail_height    INTEGER,

    -- --- 以下是运维必需的列，不属于「业务字段」，但缺了会导致具体故障 ---

    -- 相册里既有照片也有视频。视频当前只登记不处理，登记是为了统计占比。
    kind                TEXT            NOT NULL DEFAULT 'image'
                                        CHECK (kind IN ('image', 'video')),
    -- 单张失败不能中断整批；失败原因要留下来，下次运行自动重试
    processing_status   TEXT            NOT NULL DEFAULT 'pending'
                                        CHECK (processing_status IN
                                            ('pending', 'embedded', 'failed', 'skipped')),
    processing_error    TEXT,
    -- 入库的人脸数，以及被质量门控丢弃的人脸数。
    -- faces_discarded 是「合影后排的人搜不到」的量化依据 —— 排查召回率时第一个要看的数。
    face_count          INTEGER         NOT NULL DEFAULT 0,
    faces_discarded     INTEGER         NOT NULL DEFAULT 0,
    embedded_at         TIMESTAMPTZ,
    -- 源站已删除的照片软删除，便于排查「照片怎么突然搜不到了」
    deleted_at          TIMESTAMPTZ,

    created_at          TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS photo_album_idx  ON photo (album) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS photo_status_idx ON photo (processing_status) WHERE deleted_at IS NULL;

-- ---------------------------------------------------------------------------
-- face — 向量主表。一张脸一行；十人合影 = 1 条 photo + 10 条 face。
--
-- 普通表，不分区。这个量级（十万级向量）单个 HNSW 索引的 KNN 就是毫秒级，
-- 按 album 分区只会让「查所有相册」这个主流程变成对 N 个分区各扫一次再归并。
-- 取舍过程记录在 docs/plans/0003-drop-partition-and-person.md。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS face (
    id                  UUID            PRIMARY KEY DEFAULT uuid_generate_v7(),
    photo_id            UUID            NOT NULL REFERENCES photo(id) ON DELETE CASCADE,

    -- L2 归一化后的 embedding，索引用 vector_cosine_ops。
    -- 归一化统一在 embedding 服务出口完成，下游不再动。
    embedding           VECTOR(512)     NOT NULL,

    -- --- 质量门控与诊断所需 ---
    bbox_x              INTEGER         NOT NULL,
    bbox_y              INTEGER         NOT NULL,
    bbox_w              INTEGER         NOT NULL,
    bbox_h              INTEGER         NOT NULL,
    face_px             INTEGER         NOT NULL,   -- least(bbox_w, bbox_h)
    det_score           REAL            NOT NULL,

    -- 模型溯源。换模型时靠这三列识别存量数据并增量重算，而不是整库作废。
    model_name          TEXT            NOT NULL,
    model_version       TEXT            NOT NULL,
    dim                 INTEGER         NOT NULL DEFAULT 512,

    created_at          TIMESTAMPTZ     NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS face_photo_idx ON face (photo_id);
CREATE INDEX IF NOT EXISTS face_model_idx ON face (model_name, model_version);

-- HNSW 而非 IVFFlat：无需预训练、召回更高、增量插入友好。
-- IVFFlat 需要有代表性的数据才能建好 list，在边灌边查的场景下不稳。需 pgvector >= 0.5。
CREATE INDEX IF NOT EXISTS face_embedding_hnsw
    ON face USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- ---------------------------------------------------------------------------
-- block_list — opt-out。检索时在 SQL 层过滤，不在应用层结果集里过滤
-- （后者容易在新增查询路径时被漏掉，导致 opt-out 静默失效）。
--
-- 两种粒度：
--   · face  —— 某个人不希望被检索到。管理员用他的自拍跑一次检索，把命中的 face 全部
--              加进来。之后这些 face 不再参与匹配，只有他出现的照片也就不再被搜出。
--   · photo —— 整张照片不希望出现在结果里。
--
-- 没有 person 表，所以「屏蔽某个人」= 屏蔽他的那一批 face。
-- 用 `jobs block --selfie <路径>` 一条命令完成，见 docs/privacy.md。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS block_list (
    id                  UUID        PRIMARY KEY DEFAULT uuid_generate_v7(),
    scope               TEXT        NOT NULL CHECK (scope IN ('face', 'photo')),
    face_id             UUID        REFERENCES face(id)  ON DELETE CASCADE,
    photo_id            UUID        REFERENCES photo(id) ON DELETE CASCADE,
    reason              TEXT,
    created_by          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT block_list_target_ck CHECK (
        (scope = 'face'  AND face_id  IS NOT NULL AND photo_id IS NULL) OR
        (scope = 'photo' AND photo_id IS NOT NULL AND face_id  IS NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS block_list_face_uq  ON block_list (face_id)
    WHERE face_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS block_list_photo_uq ON block_list (photo_id)
    WHERE photo_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- album_sync_state — 按 album 记录同步进度，支撑增量
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS album_sync_state (
    album               VARCHAR(200) PRIMARY KEY,
    last_synced_at      TIMESTAMPTZ,
    last_full_sync_at   TIMESTAMPTZ,
    photo_count         INTEGER     NOT NULL DEFAULT 0,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- job_run — 离线任务执行记录。排查「召回率变差」的第一现场。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_run (
    id                  UUID        PRIMARY KEY DEFAULT uuid_generate_v7(),
    kind                TEXT        NOT NULL CHECK (kind IN ('ingest', 'recompute')),
    album               VARCHAR(200),
    status              TEXT        NOT NULL DEFAULT 'running'
                                    CHECK (status IN ('running', 'succeeded', 'failed')),
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ,
    stats               JSONB       NOT NULL DEFAULT '{}'::jsonb,
    error               TEXT
);

CREATE INDEX IF NOT EXISTS job_run_kind_started_idx ON job_run (kind, started_at DESC);

-- ---------------------------------------------------------------------------
-- search_audit — 检索留痕。只有计数与耗时。
--
-- 用户上传的自拍在请求结束时即从内存中销毁，不落盘不落库 —— 这张表里
-- 绝不允许出现图片、向量、或任何可还原查询人脸的数据。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS search_audit (
    id                  BIGSERIAL   PRIMARY KEY,
    session_hash        TEXT        NOT NULL,      -- 带盐 hash，不存原值
    ip_hash             TEXT        NOT NULL,
    album_filter        VARCHAR(200),              -- 本次是否限定了相册
    faces_detected      INTEGER     NOT NULL DEFAULT 0,
    candidate_count     INTEGER     NOT NULL DEFAULT 0,
    result_count        INTEGER     NOT NULL DEFAULT 0,
    latency_ms          INTEGER     NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS search_audit_created_idx ON search_audit (created_at DESC);
CREATE INDEX IF NOT EXISTS search_audit_session_idx ON search_audit (session_hash, created_at DESC);

INSERT INTO schema_migrations (version) VALUES ('001_init')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
