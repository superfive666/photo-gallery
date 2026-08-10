"""把「上传的自拍不得持久化」从一条纪律变成一个会失败的测试。

这是 CLAUDE.md 约束 #1 的守卫。它做的是源码级检查而非运行时检查 —— 因为「没有写盘」
这件事无法通过跑一次请求来证明，只能通过「代码里根本没有写盘的手段」来保证。

如果某天有人为了 debug 加了一行 `open(path, "wb").write(payload)`，这个测试会红。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from gallery_core.models import SearchAudit

API_ROOT = Path(__file__).resolve().parents[1] / "app"

# 会把字节落到进程外的手段。出现在处理自拍的代码路径上就是事故。
_FORBIDDEN_CALLS = {
    "open",  # 内置文件写入
    "NamedTemporaryFile",
    "TemporaryFile",
    "mkstemp",
    "write_bytes",
    "copyfileobj",
}
_FORBIDDEN_MODULES = {"aiofiles", "shutil", "boto3", "pickle"}

# 处理自拍字节的模块。这些文件里绝不允许出现落盘手段。
_SELFIE_PATH_MODULES = ("uploads.py", "routers/search.py")


def _iter_selfie_modules() -> list[Path]:
    paths = [API_ROOT / rel for rel in _SELFIE_PATH_MODULES]
    missing = [p for p in paths if not p.exists()]
    assert not missing, f"守卫测试指向了不存在的模块，请同步更新: {missing}"
    return paths


@pytest.mark.parametrize("path", _iter_selfie_modules(), ids=lambda p: p.name)
def test_selfie_path_has_no_filesystem_writes(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _called_name(node.func)
            assert name not in _FORBIDDEN_CALLS, (
                f"{path.name} 调用了 {name}() —— 自拍处理路径不得写入文件系统。"
                " 见 CLAUDE.md 约束 #1。"
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in _FORBIDDEN_MODULES, (
                    f"{path.name} 导入了 {alias.name} —— 自拍处理路径不得持久化数据。"
                )
        if isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            assert root not in _FORBIDDEN_MODULES, (
                f"{path.name} 从 {node.module} 导入 —— 自拍处理路径不得持久化数据。"
            )


def _called_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def test_search_audit_stores_no_biometric_data() -> None:
    """留痕表只允许有计数、耗时与 hash 后的标识。"""
    allowed = {
        "id",
        "session_hash",
        "ip_hash",
        "faces_detected",
        "candidate_count",
        "result_count",
        "latency_ms",
        "created_at",
    }
    actual = {c.name for c in SearchAudit.__table__.columns}
    unexpected = actual - allowed
    assert not unexpected, (
        f"search_audit 出现了未预期的列 {unexpected}。"
        " 这张表绝不允许存图片、向量或任何可还原查询人脸的数据。见 docs/privacy.md。"
    )


def test_logging_scrubber_redacts_vectors() -> None:
    """日志兜底过滤必须能拦掉向量与图片字节。"""
    from gallery_core.logging import scrub_processor

    scrubbed = scrub_processor(
        None,
        "info",
        {
            "event": "search",
            "embedding": [0.1] * 512,
            "query_vector": [0.2] * 512,
            "thumb_webp": b"\x00" * 100,
            "selfie_bytes": b"\x01" * 10,
            "latency_ms": 12,
        },
    )
    assert scrubbed["embedding"] == "<redacted>"
    assert scrubbed["query_vector"] == "<redacted>"
    assert scrubbed["thumb_webp"] == "<redacted>"
    assert scrubbed["selfie_bytes"] == "<redacted>"
    # 无害字段原样保留
    assert scrubbed["latency_ms"] == 12
