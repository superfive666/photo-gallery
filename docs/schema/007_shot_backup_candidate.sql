-- 007_shot_backup_candidate.sql — 评审备选：每个镜头除主选外可再锁一条备选。
--
-- 设计见 docs/plans/0011-review-preview-and-backup.md。
-- 与 005 同一约定：只追加不改写、IF NOT EXISTS、末尾登记 schema_migrations。

BEGIN;

-- 与 locked_candidate_id 语义平行：锁定镜头时可额外指定一条备选候选，
-- 渲染时随主选一同导出（manifest 标 role），后期软件里二选一。
ALTER TABLE shot ADD COLUMN IF NOT EXISTS backup_candidate_id UUID;

ALTER TABLE shot DROP CONSTRAINT IF EXISTS shot_backup_candidate_fk;
ALTER TABLE shot ADD CONSTRAINT shot_backup_candidate_fk
    FOREIGN KEY (backup_candidate_id) REFERENCES shot_candidate(id) ON DELETE SET NULL;

INSERT INTO schema_migrations (version) VALUES ('007_shot_backup_candidate')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
