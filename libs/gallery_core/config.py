"""集中配置。字段与 .env.example 一一对应。"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# 未配置密钥时的占位值。集中定义，便于启动检查识别出「还没配」的字段。
PLACEHOLDER_SECRET = "change-me"  # noqa: S105 — 这是待替换的哨兵值，不是真实密钥


class Settings(BaseSettings):
    # protected_namespaces=() 是必需的：字段名 model_name / model_version 会撞上 pydantic
    # 默认保护的 `model_` 前缀命名空间并触发警告。
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        protected_namespaces=(),
    )

    # --- 数据库 ---
    database_url: str = "postgresql+asyncpg://gallery:change-me@db:5432/photo_gallery"
    db_pool_size: int = 5
    db_max_overflow: int = 5

    # --- embedding 服务 ---
    embedding_service_url: str = "http://embedding:8000"
    # 批量请求可能要跑几十秒，超时要比单张宽松得多
    embedding_timeout_seconds: float = 300.0
    model_name: str = "buffalo_l"
    model_version: str = "1"

    # --- 离线批量 ---
    # 一批多少张照片。实际值会与 embedding 服务 /healthz 报的 max_batch_images 取小。
    ingest_batch_images: int = 16
    # 一批的字节上限。只限张数不限字节，遇到一批高像素原图会直接顶穿内存。
    ingest_batch_max_bytes: int = 128 * 1024 * 1024

    # --- 检索阈值 ---
    # ⚠️ 以下默认值是文献经验值，不是本项目的标定结果。见 docs/evaluation.md。
    face_match_threshold: float = 0.42
    min_det_score: float = 0.50
    min_face_px: int = 40
    max_results: int = 200
    hnsw_ef_search: int = 100
    # 从 HNSW 索引里取多少个最近邻作为候选，再按阈值过滤。
    # 这是召回上限：一个人在库里的照片数超过它就会少返结果。见 api/app/services/search.py。
    search_candidates: int = 500

    # --- 鉴权 ---
    # 这些占位值只为本地开发方便。带着它们上生产等于没有鉴权：
    # 默认 jwt_secret 意味着任何人都能伪造 session。启动时会检查，见 insecure_secrets()。
    invite_code_hash: str = ""
    jwt_secret: str = PLACEHOLDER_SECRET
    session_ttl_hours: int = 12
    # session cookie 的 Secure 属性。默认 true（生产走 HTTPS）；
    # 内网 http 直测阶段设 false —— 否则浏览器直接拒收 Secure cookie，
    # 表现为「登录 200 但下一个请求 401」。上 HTTPS 后必须改回 true。
    session_cookie_secure: bool = True
    audit_hash_salt: str = PLACEHOLDER_SECRET

    # --- 上传防护 ---
    rate_limit_searches_per_hour: int = 30
    max_upload_bytes: int = 10 * 1024 * 1024
    max_selfies_per_search: int = 3

    # --- 源站 photos.zrc.sg（公开、无需鉴权）---
    source_adapter: Literal["local_dir", "static_gallery"] = "static_gallery"
    source_base_url: str = "https://photos.zrc.sg"
    source_local_dir: str = "/data/sample-albums"
    # 抓取纪律：默认保守，别把自己家的图库打挂
    source_concurrency: int = 4
    source_rate_limit_per_second: float = 5.0
    source_user_agent: str = "zrc-face-search/0.1 (+https://faces.zrc.sg)"

    # --- 缩略图（仅在源站未提供时本地生成）---
    thumb_max_edge: int = 256
    thumb_quality: int = 75

    # --- 其他 ---
    log_level: str = "INFO"
    schema_dir: str = "docs/schema"

    def model_tag(self) -> str:
        """写入 face.model_name / model_version 的组合标识，用于溯源与重算。"""
        return f"{self.model_name}:{self.model_version}"

    def insecure_secrets(self) -> list[str]:
        """返回仍是占位值的密钥字段名。

        api 启动时调用它并把结果吵出来。带着 `change-me` 上生产等于没有鉴权 ——
        默认 jwt_secret 可以让任何人伪造 session cookie。
        """
        candidates = {
            "JWT_SECRET": self.jwt_secret,
            "AUDIT_HASH_SALT": self.audit_hash_salt,
        }
        return [name for name, value in candidates.items() if value == PLACEHOLDER_SECRET]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """测试用：修改环境变量后清缓存。"""
    get_settings.cache_clear()
