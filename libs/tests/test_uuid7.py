"""UUIDv7 的两条不变量：版本位正确、按时间单调递增。

单调性是选它的全部理由 —— 离线建库一次灌很多行，时间有序的主键让插入始终落在
B-tree 右端，不像 v4 那样到处引发页分裂。这条要是坏了，换 v7 就没有意义了。
"""

from __future__ import annotations

import time

from gallery_core.uuid7 import uuid7


def test_version_and_variant_bits() -> None:
    value = uuid7()
    assert value.version == 7
    # RFC 9562 变体位是 0b10，Python 把它表示成 variant == 'specified in RFC 4122'
    assert (value.bytes[8] & 0xC0) == 0x80


def test_monotonic_within_same_process() -> None:
    # 5000 个在现代机器上会落在同一个或少数几个毫秒里，所以这条同时覆盖了
    # 「同一毫秒内也必须有序」—— 那正是需要毫秒内计数器的原因。
    values = [uuid7() for _ in range(5000)]
    assert values == sorted(values), "同一进程内连续生成的 uuid7 必须严格递增"


def test_timestamp_prefix_tracks_wall_clock() -> None:
    before = int(time.time() * 1000)
    value = uuid7()
    after = int(time.time() * 1000)

    ts_ms = int.from_bytes(value.bytes[:6], "big")
    # 允许 1 秒余量，避免慢机器上偶发抖动
    assert before - 1000 <= ts_ms <= after + 1000


def test_ids_are_unique() -> None:
    assert len({uuid7() for _ in range(10_000)}) == 10_000
