-- 005_media_edit.sql — 剪辑域：剧本驱动的选片+剪裁服务（media-clip）
--
-- 设计见 docs/plans/0005-script-driven-clip-service.md（第 9 版）。
-- 与 001 同一约定：只追加不改写、IF NOT EXISTS、末尾登记 schema_migrations。

BEGIN;

-- ---------------------------------------------------------------------------
-- job_run 扩展：剪辑域的离线任务也走这张表（worker 按 queued 拾取）。
-- 001 的 CHECK 是列内联约束，Postgres 自动命名为 <table>_<column>_check。
-- ---------------------------------------------------------------------------
ALTER TABLE job_run DROP CONSTRAINT IF EXISTS job_run_kind_check;
ALTER TABLE job_run ADD CONSTRAINT job_run_kind_check
    CHECK (kind IN ('ingest', 'recompute', 'media_ingest', 'project_flow', 'render',
                    'filters_import'));

ALTER TABLE job_run DROP CONSTRAINT IF EXISTS job_run_status_check;
ALTER TABLE job_run ADD CONSTRAINT job_run_status_check
    CHECK (status IN ('queued', 'running', 'succeeded', 'failed'));

-- worker 任务的入参（如 project_id）。老 kind 不用它。
ALTER TABLE job_run ADD COLUMN IF NOT EXISTS params JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS job_run_queued_idx
    ON job_run (started_at) WHERE status = 'queued';

-- ---------------------------------------------------------------------------
-- invite_code 扩展：邀请码分角色。
--
-- search（默认）= 查照片（现状不变）；edit = 剪辑聊天窗，一码一相册，
-- 行 id 即 workspace_id（会话身份、项目归属、输出目录三处用同一个值）。
-- 复用 002 的 prefix.secret 形态与吊销机制，不另建表。
-- ---------------------------------------------------------------------------
ALTER TABLE invite_code ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'search';

ALTER TABLE invite_code DROP CONSTRAINT IF EXISTS invite_code_role_check;
ALTER TABLE invite_code ADD CONSTRAINT invite_code_role_check
    CHECK (role IN ('search', 'edit'));

-- 剪辑码必须绑定相册（一码一相册的硬语义）；查找码的 album 仍可为 NULL（管理码）
ALTER TABLE invite_code DROP CONSTRAINT IF EXISTS invite_code_edit_album_check;
ALTER TABLE invite_code ADD CONSTRAINT invite_code_edit_album_check
    CHECK (role <> 'edit' OR album IS NOT NULL);

-- ---------------------------------------------------------------------------
-- media_asset — 剪辑素材库，全局共享、按相册组织。
--
-- 与 photo 表是两个业务域：photo 服务在线检索产品（缩略图入库、无本地文件）；
-- media_asset 的原片落在 /photo-gallery/media/<album>/ 下，渲染时直接读文件。
-- source_url 是幂等键（与 photo.photo_url 同思想）。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS media_asset (
    id                  UUID          PRIMARY KEY DEFAULT uuid_generate_v7(),
    album               VARCHAR(200)  NOT NULL,
    source_url          TEXT          NOT NULL UNIQUE,
    path                TEXT          NOT NULL,
    kind                TEXT          NOT NULL DEFAULT 'image'
                                      CHECK (kind IN ('image', 'video')),

    duration_ms         INTEGER,
    width               INTEGER,
    height              INTEGER,
    fps                 REAL,
    codec               TEXT,
    size_bytes          BIGINT,
    checksum            TEXT,
    -- 分辨率档位（画质分的一项）：4K→1.0 / 2K→0.85 / 1080p→0.7 / 720p→0.4 / 更低→0.2
    resolution_tier     REAL          NOT NULL DEFAULT 0,

    processing_status   TEXT          NOT NULL DEFAULT 'pending'
                                      CHECK (processing_status IN
                                          ('pending', 'embedded', 'failed', 'skipped')),
    processing_error    TEXT,
    scene_count         INTEGER       NOT NULL DEFAULT 0,

    created_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS media_asset_album_idx  ON media_asset (album);
CREATE INDEX IF NOT EXISTS media_asset_status_idx ON media_asset (processing_status);

-- ---------------------------------------------------------------------------
-- scene — 剪辑检索的基本单元。视频一个镜头一行；照片整张即一个 scene。
--
-- embedding 是 Chinese-CLIP 图像塔的输出（出口 L2 归一化，约束 4），
-- 与 face 表的人脸向量是**不同的向量空间**，绝不混用。
-- album 冗余自 media_asset：检索外层过滤直接用，免 join。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scene (
    id                  UUID          PRIMARY KEY DEFAULT uuid_generate_v7(),
    asset_id            UUID          NOT NULL REFERENCES media_asset(id) ON DELETE CASCADE,
    album               VARCHAR(200)  NOT NULL,

    start_ms            INTEGER       NOT NULL DEFAULT 0,
    end_ms              INTEGER       NOT NULL DEFAULT 0,

    keyframe            BYTEA,
    keyframe_width      INTEGER,
    keyframe_height     INTEGER,

    embedding           VECTOR(512)   NOT NULL,
    model_name          TEXT          NOT NULL,
    model_version       TEXT          NOT NULL,
    dim                 INTEGER       NOT NULL DEFAULT 512,

    -- 画质列，0~1。照片没有稳定性概念，恒为 1。
    stability           REAL          NOT NULL DEFAULT 1,
    sharpness           REAL          NOT NULL DEFAULT 0,
    exposure            REAL          NOT NULL DEFAULT 0,
    quality_score       REAL          NOT NULL DEFAULT 0,
    face_count          INTEGER       NOT NULL DEFAULT 0,

    created_at          TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS scene_asset_idx ON scene (asset_id);
CREATE INDEX IF NOT EXISTS scene_album_idx ON scene (album);

CREATE INDEX IF NOT EXISTS scene_embedding_hnsw
    ON scene USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- ---------------------------------------------------------------------------
-- filter_preset — 滤镜库。一切滤镜都是 3D LUT（内置预设在导入时由代码生成），
-- 预览与渲染共用同一份字节，保证所见即所得。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS filter_preset (
    id              UUID          PRIMARY KEY DEFAULT uuid_generate_v7(),
    slug            VARCHAR(100)  NOT NULL UNIQUE,
    display_name    TEXT          NOT NULL,
    -- .cube 文本字节。渲染时写临时文件进 ffmpeg lut3d。
    lut             BYTEA         NOT NULL,
    -- 对标准测试图套用后的预览缩略图（JPEG）
    preview         BYTEA,
    checksum        TEXT          NOT NULL,
    builtin         BOOLEAN       NOT NULL DEFAULT FALSE,
    enabled         BOOLEAN       NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ   NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- edit_project — 一次剪辑任务（聊天窗里的一个会话）。
-- 用户提交剧本时隐式创建；album 冗余自 invite_code，创建时固化。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS edit_project (
    id                  UUID          PRIMARY KEY DEFAULT uuid_generate_v7(),
    -- 剪辑码（invite_code, role=edit）的行 id，跨设备断点恢复的身份锚点
    workspace_id        UUID          NOT NULL REFERENCES invite_code(id),
    album               VARCHAR(200)  NOT NULL,
    title               TEXT          NOT NULL DEFAULT '',
    script              TEXT          NOT NULL,

    status              TEXT          NOT NULL DEFAULT 'ingesting'
                                      CHECK (status IN
                                          ('ingesting', 'parsing', 'matching', 'reviewing',
                                           'refining', 'rendering', 'done', 'failed')),
    error               TEXT,
    current_round       INTEGER       NOT NULL DEFAULT 1,
    -- 乐观并发控制：写接口带上它，过期返回 409（双设备同时操作的保护）
    state_version       INTEGER       NOT NULL DEFAULT 0,
    default_filter_slug VARCHAR(100),

    created_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS edit_project_workspace_idx
    ON edit_project (workspace_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- edit_round — 反馈闭环留痕。第 N+1 轮的 LLM 上下文按轮次全量组装自这张表。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS edit_round (
    id                  UUID          PRIMARY KEY DEFAULT uuid_generate_v7(),
    project_id          UUID          NOT NULL REFERENCES edit_project(id) ON DELETE CASCADE,
    round_no            INTEGER       NOT NULL,
    -- 项目级补充意见（本轮）
    user_note           TEXT,
    -- 逐镜头反馈：{shot_idx: 反馈原文}
    shot_feedback       JSONB         NOT NULL DEFAULT '{}'::jsonb,
    -- 当轮 shot list 快照（LLM 输出或回退解析的结果）
    shot_list           JSONB         NOT NULL DEFAULT '[]'::jsonb,
    -- 溯源：换模型/改提示词后，旧轮次产出仍能说清是谁生成的
    llm_model           TEXT,
    prompt_fingerprint  TEXT,
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT edit_round_project_round_uq UNIQUE (project_id, round_no)
);

-- ---------------------------------------------------------------------------
-- shot — 镜头一行。locked 后后续轮次绝不重写（评审反馈闭环的硬约定）。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shot (
    id                  UUID          PRIMARY KEY DEFAULT uuid_generate_v7(),
    project_id          UUID          NOT NULL REFERENCES edit_project(id) ON DELETE CASCADE,
    idx                 INTEGER       NOT NULL,
    source_text         TEXT          NOT NULL DEFAULT '',
    description         TEXT          NOT NULL DEFAULT '',
    -- 具象化检索 query，1~3 条，RRF 融合
    queries             JSONB         NOT NULL DEFAULT '[]'::jsonb,
    media_kind          TEXT          NOT NULL DEFAULT 'any'
                                      CHECK (media_kind IN ('any', 'image', 'video')),
    min_ms              INTEGER,
    max_ms              INTEGER,
    filter_slug         VARCHAR(100),

    locked              BOOLEAN       NOT NULL DEFAULT FALSE,
    locked_candidate_id UUID,
    feedback            TEXT,
    round_no            INTEGER       NOT NULL DEFAULT 1,

    created_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT shot_project_idx_uq UNIQUE (project_id, idx)
);

-- ---------------------------------------------------------------------------
-- shot_candidate — 镜头×scene 候选。rejected 的 scene 在该镜头后续轮次的
-- 检索里排除（负反馈：换血而不是复读）。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shot_candidate (
    id              UUID          PRIMARY KEY DEFAULT uuid_generate_v7(),
    shot_id         UUID          NOT NULL REFERENCES shot(id) ON DELETE CASCADE,
    scene_id        UUID          NOT NULL REFERENCES scene(id) ON DELETE CASCADE,
    round_no        INTEGER       NOT NULL DEFAULT 1,
    rank            INTEGER       NOT NULL DEFAULT 0,
    similarity      REAL          NOT NULL DEFAULT 0,
    quality         REAL          NOT NULL DEFAULT 0,
    final_score     REAL          NOT NULL DEFAULT 0,
    status          TEXT          NOT NULL DEFAULT 'pending'
                                  CHECK (status IN ('pending', 'approved', 'rejected')),
    -- 用户微调后的 in/out 点（未微调时为 NULL，用 scene 的边界）
    in_ms           INTEGER,
    out_ms          INTEGER,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT shot_candidate_uq UNIQUE (shot_id, scene_id)
);

CREATE INDEX IF NOT EXISTS shot_candidate_shot_idx ON shot_candidate (shot_id);

-- shot.locked_candidate_id 的外键放在两张表都建好之后
ALTER TABLE shot DROP CONSTRAINT IF EXISTS shot_locked_candidate_fk;
ALTER TABLE shot ADD CONSTRAINT shot_locked_candidate_fk
    FOREIGN KEY (locked_candidate_id) REFERENCES shot_candidate(id) ON DELETE SET NULL;

-- ---------------------------------------------------------------------------
-- render_output — 渲染产物。滤镜记 slug+checksum，之后滤镜文件被更新也能说清
-- 「这段片子当时是用哪个版本调的」。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS render_output (
    id              UUID          PRIMARY KEY DEFAULT uuid_generate_v7(),
    project_id      UUID          NOT NULL REFERENCES edit_project(id) ON DELETE CASCADE,
    shot_id         UUID          NOT NULL REFERENCES shot(id) ON DELETE CASCADE,
    path            TEXT          NOT NULL,
    kind            TEXT          NOT NULL CHECK (kind IN ('image', 'video')),
    -- 精确点（评审确认的）与含余量点（实际导出的，前后各 +1s）两组时码
    precise_in_ms   INTEGER,
    precise_out_ms  INTEGER,
    padded_in_ms    INTEGER,
    padded_out_ms   INTEGER,
    filter_slug     VARCHAR(100),
    filter_checksum TEXT,
    tier            TEXT          NOT NULL DEFAULT 'crf16',
    ffmpeg_args     TEXT,
    size_bytes      BIGINT,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS render_output_project_idx ON render_output (project_id);

-- ---------------------------------------------------------------------------
-- project_event — 追加式事件时间线，聊天窗的数据源。
--
-- 事件流是展示层，状态表（shot / shot_candidate / edit_round）才是事实层：
-- 事件只追加、绝不改写，payload 只存小快照 + id 引用，不内嵌大对象。
-- 绝不允许出现 embedding 或任何图片字节（约束 2 延伸适用）。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS project_event (
    id          BIGSERIAL     PRIMARY KEY,
    project_id  UUID          NOT NULL REFERENCES edit_project(id) ON DELETE CASCADE,
    seq         INTEGER       NOT NULL,
    actor       TEXT          NOT NULL CHECK (actor IN ('user', 'assistant', 'system')),
    kind        TEXT          NOT NULL,
    payload     JSONB         NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT project_event_seq_uq UNIQUE (project_id, seq)
);

INSERT INTO schema_migrations (version) VALUES ('005_media_edit')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
