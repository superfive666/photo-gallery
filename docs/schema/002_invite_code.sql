-- 002_invite_code.sql — 邀请码与相册绑定
--
-- 一张码解锁一个相册（album 为 NULL 则是全相册的管理码）。
-- 码的形态是 `<prefix>.<secret>`：prefix 公开、用于定位行，secret 只存 argon2 hash。
-- 这样登录只做一次 argon2 验证 —— 否则要把全表的 hash 挨个验一遍，
-- 码一多登录就被 argon2 的耗时拖垮，还给爆破者送了放大器。
--
-- 吊销 = 置 disabled_at，不删行：谁的码、什么时候停用，留痕。
-- 见 docs/plans/0006-invite-scope-and-abuse-controls.md。

BEGIN;

CREATE TABLE IF NOT EXISTS invite_code (
    id          UUID         PRIMARY KEY DEFAULT uuid_generate_v7(),
    prefix      VARCHAR(16)  NOT NULL,
    code_hash   TEXT         NOT NULL,          -- argon2(secret)，绝不存明文
    album       VARCHAR(200),                   -- NULL = 全相册（管理码）
    label       TEXT,                           -- 发给谁 / 用途备注
    disabled_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- prefix 是登录时的查找键，必须唯一
CREATE UNIQUE INDEX IF NOT EXISTS invite_code_prefix_idx ON invite_code (prefix);

INSERT INTO schema_migrations (version) VALUES ('002_invite_code')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
