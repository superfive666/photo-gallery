from __future__ import annotations

from pathlib import Path

import pytest

from jobs.migrate import discover


def test_discover_orders_by_filename(tmp_path: Path) -> None:
    for name in ("003_c.sql", "001_a.sql", "002_b.sql"):
        (tmp_path / name).write_text("SELECT 1;", encoding="utf-8")
    assert [p.stem for p in discover(tmp_path)] == ["001_a", "002_b", "003_c"]


def test_discover_rejects_bad_names(tmp_path: Path) -> None:
    """文件名决定执行顺序，不能随意取名。"""
    (tmp_path / "add-thumbnails.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(ValueError, match="不符合约定"):
        discover(tmp_path)


def test_real_schema_dir_is_valid() -> None:
    """仓库里的 docs/schema 必须始终满足命名约定。"""
    schema_dir = Path(__file__).resolve().parents[2] / "docs" / "schema"
    files = discover(schema_dir)
    assert files, "docs/schema 下应至少有一个迁移文件"
    assert files[0].stem == "001_init"
