-- 008_shot_render_queue.sql — 预渲染任务：镜头锁定即入队剪该镜头的片段。
--
-- 设计见 docs/plans/0012-prerender-on-lock.md。只改 job_run 的 kind 白名单，
-- 无新表新列。与 005 同一约定：只追加不改写、末尾登记 schema_migrations。

BEGIN;

ALTER TABLE job_run DROP CONSTRAINT IF EXISTS job_run_kind_check;
ALTER TABLE job_run ADD CONSTRAINT job_run_kind_check
    CHECK (kind IN ('ingest', 'recompute', 'media_ingest', 'project_flow', 'render',
                    'filters_import', 'shot_render'));

INSERT INTO schema_migrations (version) VALUES ('008_shot_render_queue')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
