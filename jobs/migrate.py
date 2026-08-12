"""迁移执行器：按文件名顺序执行 docs/schema/*.sql，跳过已应用的版本。

约定见 docs/schema/README.md。要点：
  · 已发布的迁移文件绝不原地修改 —— 那会让不同环境的 schema 静默分叉；
  · 每个文件自己负责写入 schema_migrations；
  · 部署顺序是「先迁移、后换代码」，所以 DDL 只能做向后兼容的新增。
"""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from gallery_core.logging import get_logger

log = get_logger(__name__)

_FILENAME_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


def discover(schema_dir: Path) -> list[Path]:
    if not schema_dir.is_dir():
        raise FileNotFoundError(f"找不到 schema 目录: {schema_dir}")

    files = []
    for path in sorted(schema_dir.glob("*.sql")):
        if not _FILENAME_RE.match(path.name):
            raise ValueError(
                f"迁移文件名不符合约定 NNN_slug.sql: {path.name}。 命名决定执行顺序，不能随意取名。"
            )
        files.append(path)

    versions = [p.stem for p in files]
    duplicates = {v for v in versions if versions.count(v) > 1}
    if duplicates:
        raise ValueError(f"迁移版本重复: {duplicates}")
    return files


async def applied_versions(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        exists = (
            await conn.execute(text("SELECT to_regclass('public.schema_migrations') IS NOT NULL"))
        ).scalar()
        if not exists:
            return set()
        rows = (await conn.execute(text("SELECT version FROM schema_migrations"))).scalars().all()
    return set(rows)


async def run(engine: AsyncEngine, schema_dir: Path) -> list[str]:
    """执行未应用的迁移，返回本次执行的版本列表。"""
    files = discover(schema_dir)
    done = await applied_versions(engine)

    executed: list[str] = []
    for path in files:
        version = path.stem
        if version in done:
            continue
        sql = path.read_text(encoding="utf-8")
        log.info("migration_applying", version=version)
        # 迁移文件是「多条语句 + 自带 BEGIN/COMMIT」的整体脚本，必须绕过 SQLAlchemy
        # 直接用底层 asyncpg 连接执行：SQLAlchemy 的 execute/exec_driver_sql 都走
        # asyncpg 的预处理语句协议，一次只接受一条命令（报 "cannot insert multiple
        # commands into a prepared statement"）。asyncpg 自己的 execute() 在无参数
        # 调用时走 simple query protocol —— 允许多条命令，事务也归脚本自己控制。
        async with engine.connect() as conn:
            raw = await conn.get_raw_connection()
            driver = raw.driver_connection
            assert driver is not None  # 刚从池里拿出的连接必有底层驱动连接
            await driver.execute(sql)
        executed.append(version)
        log.info("migration_applied", version=version)

    if not executed:
        log.info("migration_up_to_date", total=len(files))
    return executed
