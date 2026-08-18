-- 004_video_segments.sql — 视频人脸时间段
--
-- 视频抽帧后，逐帧检测被聚合成 tracklet（一段时间内连续出现的一张脸），
-- 每个 tracklet 存为一行 face：embedding 是段内均值（再 L2 归一化），
-- t_start_ms / t_end_ms 是出现区间。照片行这两个字段恒为 NULL。
--
-- 复用 face 表而不建新表：一个 HNSW 索引同时覆盖照片与视频、阈值统一、
-- block_list 与相册 scope 自动生效。见 docs/plans/0008。

BEGIN;

ALTER TABLE face  ADD COLUMN IF NOT EXISTS t_start_ms  INTEGER;
ALTER TABLE face  ADD COLUMN IF NOT EXISTS t_end_ms    INTEGER;
ALTER TABLE photo ADD COLUMN IF NOT EXISTS duration_ms INTEGER;

INSERT INTO schema_migrations (version) VALUES ('004_video_segments')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
