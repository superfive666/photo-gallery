"""按 session + IP 双维度的滑动窗口限流。

单看 IP 会误伤共用出口的成员（同一栋楼、同一运营商 NAT）；单看 session 则换个 cookie 就绕过。
两者都限才有意义。

进程内实现，够单实例 api 用。若日后横向扩容多个 api 副本，需要换成 Redis ——
届时这个模块的接口不变，只换实现。
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException


class SlidingWindowLimiter:
    def __init__(self, max_events: int, window_seconds: int = 3600) -> None:
        self._max = max_events
        self._window = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        bucket = self._events[key]
        cutoff = now - self._window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= self._max:
            retry_after = int(bucket[0] + self._window - now) + 1
            raise HTTPException(
                status_code=429,
                detail="检索次数过于频繁，请稍后再试",
                headers={"Retry-After": str(retry_after)},
            )
        bucket.append(now)

    def prune(self) -> None:
        """清掉空桶，避免长期运行后字典无限增长。"""
        cutoff = time.monotonic() - self._window
        for key in list(self._events):
            bucket = self._events[key]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if not bucket:
                del self._events[key]
