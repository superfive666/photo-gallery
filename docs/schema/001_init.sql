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
-- 天然按时间单调递增，插入始终落在索引右端 —— 离线建库是「一次灌很多」的场景，
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
-- album 在这张表上只做索引，不分区（分区在 face 上）。
--
-- ⚠️ album 列显式指定 COLLATE "C"：分区键的比较依赖排序规则，而 face.album 是分区键。
-- 两张表用同一个排序规则，才不会在 JOIN/比较时出现「无法确定使用哪个 collation」的错误，
-- 也让分区路由的行为与 locale 无关、可预测。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS photo (
    id                  UUID            PRIMARY KEY DEFAULT uuid_generate_v7(),

    -- URL friendly 的相册标识，形如 '2026-08-10'（对应 photos.zrc.sg/album/2026-08-10）
    album               VARCHAR(200) COLLATE "C" NOT NULL,

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
-- person — 人脸聚类得到的簇。不分区（跨相册），不承载真实身份。
--
-- 两段式检索的第一段就是对 centroid 做 KNN：簇心是同一人多张样本（正脸/侧脸/不同光照）
-- 的均值，比任何单张照片都更接近这个人的「平均长相」，对查询自拍的角度差异更鲁棒。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS person (
    id                  UUID        PRIMARY KEY DEFAULT uuid_generate_v7(),
    label               TEXT,                      -- 可选人工标注，默认永远 NULL
    centroid            VECTOR(512) NOT NULL,
    face_count          INTEGER     NOT NULL DEFAULT 0,
    model_name          TEXT        NOT NULL,
    model_version       TEXT        NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS person_centroid_hnsw
    ON person USING hnsw (centroid vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- ---------------------------------------------------------------------------
-- face — 向量主表。一张脸一行；十人合影 = 1 条 photo + 10 条 face。
--
-- 按 album 做 LIST 分区，使「只在某个相册里找」能被分区裁剪成单分区精确检索。
--
-- ⚠️ 分区表的主键必须包含分区键，所以 PK 是 (album, id) 而不是 (id)。
--    album 放在前面，让 PK 索引同时能服务按相册的前缀查找。
--    代价：id 的全局唯一性不由数据库保证（只保证 (album, id) 唯一）。
--    uuidv7 的碰撞概率可忽略，且我们从不用「只给 id 不给 album」的方式查 face。
--
-- ⚠️ 全库检索（主流程：上传自拍找所有相册）不会被分区裁剪，必须对每个分区各做一次
--    HNSW 索引扫描再 MergeAppend。相册数量上到千级时这个开销会变得明显。
--    规模与退出路径见 docs/schema/README.md「分区的代价」。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS face (
    id                  UUID            NOT NULL DEFAULT uuid_generate_v7(),
    -- 外键指向未分区的 photo，PG12+ 支持分区表引用普通表
    photo_id            UUID            NOT NULL REFERENCES photo(id) ON DELETE CASCADE,
    -- 与 photo.album 同义。冗余存一份是分区的前提 —— 分区键必须在本表上。
    album               VARCHAR(200) COLLATE "C" NOT NULL,

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

    -- 聚类结果。ON DELETE SET NULL：重聚类时先清 person，face 自动置空。
    person_id           UUID            REFERENCES person(id) ON DELETE SET NULL,

    -- 模型溯源。换模型时靠这三列识别存量数据并增量重算，而不是整库作废。
    model_name          TEXT            NOT NULL,
    model_version       TEXT            NOT NULL,
    dim                 INTEGER         NOT NULL DEFAULT 512,

    created_at          TIMESTAMPTZ     NOT NULL DEFAULT now(),

    PRIMARY KEY (album, id)
) PARTITION BY LIST (album);

-- 兜底分区：接收还没有专属分区的相册（以及非日期形式的 album）。
-- ⚠️ 如果某个 album 的行已经落进 DEFAULT，之后再为它建专属分区会失败
--    （PG 要求扫描 DEFAULT 分区且不允许存在冲突行）。所以 pipeline 的顺序是
--    「先建分区、再插数据」。恢复办法见 docs/schema/README.md。
CREATE TABLE IF NOT EXISTS face_default PARTITION OF face DEFAULT;

-- 在父表上建索引，Postgres 会为每个现有分区建本地索引，
-- 之后新建的分区也会自动继承 —— 所以运行时加分区不需要额外建索引。
CREATE INDEX IF NOT EXISTS face_embedding_hnsw
    ON face USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS face_photo_idx  ON face (photo_id);
CREATE INDEX IF NOT EXISTS face_person_idx ON face (person_id);
CREATE INDEX IF NOT EXISTS face_model_idx  ON face (model_name, model_version);

-- ---------------------------------------------------------------------------
-- ensure_face_partition — 为一个 album 建专属分区（幂等、并发安全）
--
-- jobs 在处理某个相册的第一张照片之前调用它。分区名不能直接用 album：
-- album 允许 200 字符，而 PG 标识符上限 63 字节。所以用「净化后的前缀 + md5 短哈希」，
-- 既可读又不会撞名、不会超长。
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ensure_face_partition(p_album TEXT)
RETURNS TEXT
LANGUAGE plpgsql
AS $$
DECLARE
    slug       TEXT;
    part_name  TEXT;
BEGIN
    IF p_album IS NULL OR p_album = '' THEN
        RAISE EXCEPTION 'album 不能为空';
    END IF;

    slug := left(regexp_replace(lower(p_album), '[^a-z0-9]+', '_', 'g'), 38);
    part_name := 'face_p_' || slug || '_' || left(md5(p_album), 8);

    BEGIN
        EXECUTE format(
            'CREATE TABLE %I PARTITION OF face FOR VALUES IN (%L)',
            part_name, p_album
        );
    EXCEPTION
        -- 分区已存在（本进程之前建过，或并发的另一个 ingest 先建了）
        WHEN duplicate_table THEN NULL;
        -- 另一个事务正在建同名分区
        WHEN unique_violation THEN NULL;
    END;

    RETURN part_name;
END;
$$;

-- ---------------------------------------------------------------------------
-- block_list — opt-out。检索时在 SQL 层过滤，不在应用层结果集里过滤
-- （后者容易在新增查询路径时被漏掉，导致 opt-out 静默失效）。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS block_list (
    id                  UUID        PRIMARY KEY DEFAULT uuid_generate_v7(),
    scope               TEXT        NOT NULL CHECK (scope IN ('person', 'photo')),
    person_id           UUID        REFERENCES person(id) ON DELETE CASCADE,
    photo_id            UUID        REFERENCES photo(id)  ON DELETE CASCADE,
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
-- album_sync_state — 按 album 记录同步进度，支撑增量
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS album_sync_state (
    album               VARCHAR(200) COLLATE "C" PRIMARY KEY,
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
    kind                TEXT        NOT NULL CHECK (kind IN ('ingest', 'cluster', 'recompute')),
    album               VARCHAR(200) COLLATE "C",
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
-- 绝不存图片、向量、或任何可还原查询人脸的数据。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS search_audit (
    id                  BIGSERIAL   PRIMARY KEY,
    session_hash        TEXT        NOT NULL,      -- 带盐 hash，不存原值
    ip_hash             TEXT        NOT NULL,
    album_filter        VARCHAR(200) COLLATE "C",  -- 本次是否限定了相册
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
