-- 001_init.sql — 初始 schema
--
-- 约定：迁移文件只以追加方式演进，已发布的文件绝不原地修改。
-- 所有 DDL 用 IF NOT EXISTS，保证重复执行安全（迁移执行器也会跳过已应用的版本）。

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()

-- ---------------------------------------------------------------------------
-- 迁移版本表
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT        PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- album — 对应源站的一次活动相册
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS album (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    source_album_id     TEXT        NOT NULL UNIQUE,
    name                TEXT        NOT NULL,
    source_url          TEXT,
    -- 只有 public 相册进检索库。源站转私密后要能同步改这里并从检索中排除。
    visibility          TEXT        NOT NULL DEFAULT 'public'
                                    CHECK (visibility IN ('public', 'private', 'unknown')),
    event_date          DATE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- photo — 一张源站资产一行（含视频，视频当前只登记不处理）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS photo (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    album_id            UUID        NOT NULL REFERENCES album(id) ON DELETE CASCADE,

    -- 源站稳定唯一标识。幂等写入的依据：ON CONFLICT (source_asset_id) DO UPDATE
    source_asset_id     TEXT        NOT NULL UNIQUE,
    kind                TEXT        NOT NULL DEFAULT 'image'
                                    CHECK (kind IN ('image', 'video')),
    filename            TEXT        NOT NULL,
    source_url          TEXT        NOT NULL,
    -- etag / md5；源站没有则退化为 "<size>:<mtime>" 弱校验。未变则跳过重新下载与推理。
    source_checksum     TEXT,
    size_bytes          BIGINT,
    width               INTEGER,
    height              INTEGER,
    taken_at            TIMESTAMPTZ,

    -- 缩略图存二进制而非 base64：省 33% 体积，且 >2KB 会被 TOAST 移到行外存储，
    -- 主堆保持紧凑。由 GET /api/photos/{id}/thumb 单独分发，搜索响应只带 URL。
    thumb_webp          BYTEA,
    thumb_width         INTEGER,
    thumb_height        INTEGER,

    processing_status   TEXT        NOT NULL DEFAULT 'pending'
                                    CHECK (processing_status IN
                                        ('pending', 'embedded', 'failed', 'skipped')),
    processing_error    TEXT,
    face_count          INTEGER     NOT NULL DEFAULT 0,
    -- 被质量门控丢弃的人脸数。排查「后排的人搜不到」时的第一手数据。
    faces_discarded     INTEGER     NOT NULL DEFAULT 0,
    embedded_at         TIMESTAMPTZ,

    -- 源站已删除的资产软删除，便于排查「照片怎么突然搜不到了」
    deleted_at          TIMESTAMPTZ,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS photo_album_idx   ON photo (album_id);
CREATE INDEX IF NOT EXISTS photo_status_idx  ON photo (processing_status)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS photo_taken_idx   ON photo (taken_at DESC NULLS LAST)
    WHERE deleted_at IS NULL;

-- ---------------------------------------------------------------------------
-- person — 人脸聚类得到的簇。不含真实身份，除非有人主动命名。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS person (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    -- 可选的人工标注名，仅用于运维排查与 opt-out 处理；默认永远为 NULL。
    label               TEXT,
    -- 簇内成员向量的均值再做 L2 归一化。比任何单张照片都更接近这个人的「平均长相」，
    -- 对查询自拍的角度差异更鲁棒 —— 这是两段式检索的核心。
    centroid            VECTOR(512) NOT NULL,
    face_count          INTEGER     NOT NULL DEFAULT 0,
    model_name          TEXT        NOT NULL,
    model_version       TEXT        NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- face — 一张脸一行。十人合影 = 1 条 photo + 10 条 face。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS face (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    photo_id            UUID        NOT NULL REFERENCES photo(id) ON DELETE CASCADE,
    person_id           UUID        REFERENCES person(id) ON DELETE SET NULL,

    -- 人脸框（像素，原图坐标系）
    bbox_x              INTEGER     NOT NULL,
    bbox_y              INTEGER     NOT NULL,
    bbox_w              INTEGER     NOT NULL,
    bbox_h              INTEGER     NOT NULL,
    -- 短边像素数，= least(bbox_w, bbox_h)。质量门控与漏检归因都要用。
    face_px             INTEGER     NOT NULL,
    det_score           REAL        NOT NULL,
    landmarks           JSONB,

    -- L2 归一化后的 embedding。索引用 vector_cosine_ops。
    -- 归一化统一在 embedding 服务出口完成，下游不再动。
    embedding           VECTOR(512) NOT NULL,

    -- 模型溯源。换模型时靠这三列识别存量数据并重算，而不是整库作废。
    model_name          TEXT        NOT NULL,
    model_version       TEXT        NOT NULL,
    dim                 INTEGER     NOT NULL DEFAULT 512,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS face_photo_idx  ON face (photo_id);
CREATE INDEX IF NOT EXISTS face_person_idx ON face (person_id);
CREATE INDEX IF NOT EXISTS face_model_idx  ON face (model_name, model_version);

-- HNSW 而非 IVFFlat：无需预训练、召回更高、增量插入友好。
-- IVFFlat 需要有代表性的数据才能建好 list，在边灌边查的场景下不稳。需 pgvector >= 0.5。
CREATE INDEX IF NOT EXISTS face_embedding_hnsw
    ON face USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS person_centroid_hnsw
    ON person USING hnsw (centroid vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- ---------------------------------------------------------------------------
-- block_list — opt-out。检索时在 SQL 层过滤，不在应用层结果集里过滤
-- （后者容易在新增查询路径时被漏掉）。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS block_list (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    scope               TEXT        NOT NULL CHECK (scope IN ('person', 'photo')),
    person_id           UUID        REFERENCES person(id) ON DELETE CASCADE,
    photo_id            UUID        REFERENCES photo(id) ON DELETE CASCADE,
    reason              TEXT,
    created_by          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT block_list_target_ck CHECK (
        (scope = 'person' AND person_id IS NOT NULL AND photo_id IS NULL) OR
        (scope = 'photo'  AND photo_id  IS NOT NULL AND person_id IS NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS block_list_person_uq ON block_list (person_id)
    WHERE person_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS block_list_photo_uq  ON block_list (photo_id)
    WHERE photo_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- source_sync_state — 按 album 记录同步游标，支撑增量
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS source_sync_state (
    album_id            UUID        PRIMARY KEY REFERENCES album(id) ON DELETE CASCADE,
    last_synced_at      TIMESTAMPTZ,
    last_full_sync_at   TIMESTAMPTZ,
    last_cursor         TEXT,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- job_run — 每次离线任务的执行记录。排查「召回率变差」的第一现场。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_run (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    kind                TEXT        NOT NULL CHECK (kind IN ('ingest', 'cluster', 'recompute')),
    album_id            UUID        REFERENCES album(id) ON DELETE SET NULL,
    status              TEXT        NOT NULL DEFAULT 'running'
                                    CHECK (status IN ('running', 'succeeded', 'failed')),
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ,
    -- {processed, skipped, failed, faces_added, faces_discarded, bytes_downloaded, ...}
    stats               JSONB       NOT NULL DEFAULT '{}'::jsonb,
    error               TEXT
);

CREATE INDEX IF NOT EXISTS job_run_kind_started_idx ON job_run (kind, started_at DESC);

-- ---------------------------------------------------------------------------
-- search_audit — 检索留痕。只有计数与耗时。
-- 绝不存图片、向量、或任何可还原查询人脸的数据。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS search_audit (
    id                  BIGSERIAL   PRIMARY KEY,
    -- 带盐 hash，不存原值
    session_hash        TEXT        NOT NULL,
    ip_hash             TEXT        NOT NULL,
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
