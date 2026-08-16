-- 003_face_thumb.sql — 人脸小图
--
-- 浏览模式（plans/0007）需要展示每张照片上检测到的人脸。小图在入库时从原图
-- 按 bbox 裁出（160px WebP），存 BYTEA 走 TOAST，与 photo.thumbnail 同一策略。
--
-- 可空：003 之前入库的存量脸没有小图，用 `python -m jobs face-thumbs` 回填
-- （bbox 已在库里，只需重新下载原图裁剪，不经过 embedding 服务）。

BEGIN;

ALTER TABLE face ADD COLUMN IF NOT EXISTS thumb BYTEA;

INSERT INTO schema_migrations (version) VALUES ('003_face_thumb')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
