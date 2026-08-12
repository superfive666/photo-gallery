"""UUIDv7 生成（RFC 9562）。

数据库侧有 `uuid_generate_v7()` 作为列默认值，这份 Python 实现用于**批量入库**：
预先在客户端生成 id，就能把一批 photo + 它们的 face 一次性 executemany 写进去，
不必为了拿到 photo.id 而对每一张照片做一次 RETURNING 往返。

## 为什么要额外维护一个计数器

只把前 48 位填时间戳、其余全填随机（RFC 9562 允许的做法之一），在同一毫秒内生成的
id 之间是**无序**的。离线建库一毫秒能生成上百个 id，那就等于在每个毫秒区间内又变回
随机插入。所以这里按 RFC 9562 的「单调随机计数器」方案：用 rand_a 的 12 位做毫秒内
计数器，新的毫秒重新随机播种，使同一进程内生成的 id 严格递增。

注意：SQL 侧的 `uuid_generate_v7()` 没有这个计数器，只保证毫秒粒度有序 ——
对 B-tree 局部性来说毫秒粒度已经够了，同一毫秒内的乱序只影响索引右端极小的一块。
需要严格单调（例如把 id 当排序键）时，用这个 Python 实现在客户端生成。

Python 3.14 起标准库有 `uuid.uuid7()`；到那时可以直接换掉这个模块。
"""

from __future__ import annotations

import os
import threading
import time
import uuid

# rand_a 是 12 位，所以毫秒内最多 4096 个序号
_COUNTER_BITS = 12
_COUNTER_MAX = (1 << _COUNTER_BITS) - 1
# 新毫秒的播种上限，留出足够的增长空间，避免一毫秒内就把计数器用满
_SEED_MAX = 1 << 10

_lock = threading.Lock()
_last_ms = -1
_counter = 0


def uuid7() -> uuid.UUID:
    """时间有序且同一进程内严格递增的 UUID。

    前 48 位是 Unix 毫秒时间戳，接着 12 位是毫秒内计数器，其余 62 位随机。
    插入 B-tree 时始终落在索引右端，不像 v4 那样到处引发页分裂。
    """
    global _last_ms, _counter

    with _lock:
        ts_ms = int(time.time() * 1000)
        if ts_ms > _last_ms:
            _last_ms = ts_ms
            _counter = int.from_bytes(os.urandom(2), "big") % _SEED_MAX
        elif _counter >= _COUNTER_MAX:
            # 这一毫秒的序号用尽（单进程要 4096 个 id/ms 才会发生）。
            # 借用下一毫秒而不是 sleep：时间戳只需单调，不必与墙钟严格一致。
            _last_ms += 1
            ts_ms = _last_ms
            _counter = 0
        else:
            _counter += 1
            ts_ms = _last_ms
        counter = _counter

    rand_b = bytearray(os.urandom(8))
    # 第 9 字节高 2 位 = 变体 0b10
    rand_b[0] = (rand_b[0] & 0x3F) | 0x80

    # 第 7 字节：高 4 位版本号 7，低 4 位是计数器高 4 位；第 8 字节是计数器低 8 位
    ver_and_counter_hi = 0x70 | ((counter >> 8) & 0x0F)
    counter_lo = counter & 0xFF

    return uuid.UUID(
        bytes=ts_ms.to_bytes(6, "big") + bytes([ver_and_counter_hi, counter_lo]) + bytes(rand_b)
    )
