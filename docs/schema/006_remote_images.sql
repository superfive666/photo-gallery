-- 006_remote_images.sql — 剪辑素材的照片不再落盘
--
-- 照片的分析（关键帧/画质/CLIP 向量）在内存中完成、渲染导出时按 source_url
-- 现下载 —— 本地盘只需要放视频（拆条与 ffmpeg 剪裁必须随机访问文件）。
-- path 因此允许为 NULL：NULL = 远端照片，非 NULL = 本地文件（视频，或手动拷入的素材）。

BEGIN;

ALTER TABLE media_asset ALTER COLUMN path DROP NOT NULL;

INSERT INTO schema_migrations (version) VALUES ('006_remote_images')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
