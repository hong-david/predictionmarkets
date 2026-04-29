"""Small fixed-window API rate limiter.

The production path uses Redis so limits are shared across app workers. If
Redis is unavailable, the limiter falls back to a process-local counter so dev
and smoke deployments keep running.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import threading
import time
from typing import Protocol

import redis
from fastapi import Request

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimitRule:
    name: str
    requests: int
    window_seconds: int


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    rule: RateLimitRule
    remaining: int
    reset_seconds: int


class FixedWindowCounter(Protocol):
    def increment(self, key: str, ttl_seconds: int) -> int:
        """Increment a key and return the count for the current window."""


class RedisFixedWindowCounter:
    def __init__(self, redis_url: str, *, socket_timeout: float = 0.2) -> None:
        self._client = redis.Redis.from_url(redis_url, socket_timeout=socket_timeout)
        self._client.ping()

    def increment(self, key: str, ttl_seconds: int) -> int:
        count = int(self._client.incr(key))
        if count == 1:
            self._client.expire(key, ttl_seconds)
        return count


class InMemoryFixedWindowCounter:
    def __init__(self, *, time_func=time.time) -> None:
        self._time_func = time_func
        self._lock = threading.Lock()
        self._counts: dict[str, tuple[int, int]] = {}

    def increment(self, key: str, ttl_seconds: int) -> int:
        now = int(self._time_func())
        with self._lock:
            expires_at, count = self._counts.get(key, (now + ttl_seconds, 0))
            if expires_at <= now:
                expires_at = now + ttl_seconds
                count = 0
            count += 1
            self._counts[key] = (expires_at, count)
            if len(self._counts) > 10_000:
                self._counts = {
                    k: v for k, v in self._counts.items() if v[0] > now
                }
            return count


class RateLimiter:
    def __init__(
        self,
        *,
        enabled: bool,
        redis_url: str,
        default_limit_per_minute: int,
        expensive_limit_per_minute: int,
        health_limit_per_minute: int,
        window_seconds: int = 60,
        counter: FixedWindowCounter | None = None,
        time_func=time.time,
    ) -> None:
        self.enabled = enabled
        self.redis_url = redis_url
        self.default_rule = RateLimitRule(
            "api_default", default_limit_per_minute, window_seconds
        )
        self.expensive_rule = RateLimitRule(
            "api_expensive", expensive_limit_per_minute, window_seconds
        )
        self.health_rule = RateLimitRule(
            "api_health", health_limit_per_minute, window_seconds
        )
        self._counter = counter
        self._memory_counter = InMemoryFixedWindowCounter(time_func=time_func)
        self._time_func = time_func
        self._redis_failed = False

    @classmethod
    def from_settings(cls, settings) -> "RateLimiter":
        return cls(
            enabled=bool(settings.rate_limit_enabled),
            redis_url=settings.redis_url,
            default_limit_per_minute=int(settings.rate_limit_default_per_minute),
            expensive_limit_per_minute=int(settings.rate_limit_expensive_per_minute),
            health_limit_per_minute=int(settings.rate_limit_health_per_minute),
            window_seconds=int(settings.rate_limit_window_seconds),
        )

    def check_request(self, request: Request) -> RateLimitDecision | None:
        rule = self._rule_for_path(request.url.path)
        if rule is None or rule.requests <= 0:
            return None

        now = int(self._time_func())
        bucket = now // rule.window_seconds
        reset_seconds = int(((bucket + 1) * rule.window_seconds) - now)
        key = self._cache_key(
            rule=rule,
            client_id=_client_identifier(request),
            bucket=bucket,
        )
        count = self._increment(key, rule.window_seconds)
        remaining = max(0, rule.requests - count)
        return RateLimitDecision(
            allowed=count <= rule.requests,
            rule=rule,
            remaining=remaining,
            reset_seconds=max(1, reset_seconds),
        )

    def _rule_for_path(self, path: str) -> RateLimitRule | None:
        if not self.enabled or not path.startswith("/api"):
            return None
        if path == "/api/health":
            return self.health_rule
        if (
            path.startswith("/api/dashboard/search")
            or path.startswith("/api/dashboard/news")
            or path.endswith("/news")
        ):
            return self.expensive_rule
        return self.default_rule

    def _counter_for_request(self) -> FixedWindowCounter:
        if self._counter is not None:
            return self._counter
        if self._redis_failed:
            return self._memory_counter
        try:
            self._counter = RedisFixedWindowCounter(self.redis_url)
            return self._counter
        except redis.RedisError as exc:
            logger.warning(
                "Redis rate-limit counter unavailable; using in-memory fallback: %s",
                exc,
            )
            self._redis_failed = True
            return self._memory_counter

    def _increment(self, key: str, ttl_seconds: int) -> int:
        counter = self._counter_for_request()
        try:
            return counter.increment(key, ttl_seconds)
        except redis.RedisError as exc:
            logger.warning(
                "Redis rate-limit increment failed; using in-memory fallback: %s",
                exc,
            )
            self._counter = self._memory_counter
            self._redis_failed = True
            return self._memory_counter.increment(key, ttl_seconds)

    @staticmethod
    def _cache_key(*, rule: RateLimitRule, client_id: str, bucket: int) -> str:
        digest = hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:24]
        return f"rl:{rule.name}:{digest}:{bucket}"


def _client_identifier(request: Request) -> str:
    for header in ("cf-connecting-ip", "x-real-ip", "x-forwarded-for"):
        raw = request.headers.get(header)
        if raw:
            return raw.split(",", 1)[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"
