"""Bounded, transport-agnostic sequential pagination executor."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Page(Generic[T]):
    records: tuple[T, ...]
    next_token: str | None = None
    exhausted: bool = False
    expected_count: int | None = None


@dataclass(frozen=True)
class Limits:
    max_requests: int = 5
    max_records: int = 10000
    max_seconds: float = 60

    def __post_init__(self) -> None:
        if (
            type(self.max_requests) is not int
            or type(self.max_records) is not int
            or self.max_requests < 1
            or self.max_records < 1
            or type(self.max_seconds) not in (int, float)
            or not math.isfinite(self.max_seconds)
            or self.max_seconds <= 0
        ):
            raise ValueError("invalid limits")


@dataclass(frozen=True)
class Cursor(Generic[T]):
    scope_id: str
    next_token: str | None
    records: tuple[T, ...] = ()
    visited_tokens: tuple[str, ...] = ()
    pages_completed: int = 0
    expected_count: int | None = None


@dataclass(frozen=True)
class Result(Generic[T]):
    cursor: Cursor[T]
    complete: bool
    stop_reason: str
    requests_this_run: int


def _valid_count(v: object) -> bool:
    return v is None or (type(v) is int and v >= 0)


def _valid_token(v: object) -> bool:
    return type(v) is str and bool(v) and len(v) <= 4096


def _valid_optional_token(v: object) -> bool:
    return v is None or _valid_token(v)


def _cursor(
    c: Cursor[T],
    token: str | None,
    records: tuple[T, ...],
    visited: tuple[str, ...],
    pages: int,
    count: int | None,
) -> Cursor[T]:
    return Cursor(c.scope_id, token, records, visited, pages, count)


async def collect_pages(
    *,
    scope_id: str,
    first_token: str,
    fetch_page: Callable[[str], Awaitable[Page[T]]],
    record_key: Callable[[T], str],
    limits: Limits = Limits(),  # noqa: B008
    resume: Cursor[T] | None = None,
    checkpoint: Callable[[Cursor[T]], Awaitable[None]] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Result[T]:
    if not _valid_token(scope_id) or not _valid_token(first_token):
        raise ValueError("invalid input")
    if resume and (
        type(resume.scope_id) is not str
        or resume.scope_id != scope_id
        or not _valid_token(resume.next_token)
        or type(resume.records) is not tuple
        or type(resume.visited_tokens) is not tuple
        or any(not _valid_token(token) for token in resume.visited_tokens)
        or len(set(resume.visited_tokens)) != len(resume.visited_tokens)
        or type(resume.pages_completed) is not int
        or resume.pages_completed != len(resume.visited_tokens)
        or not _valid_count(resume.expected_count)
        or len(resume.records) > limits.max_records
        or (resume.expected_count is not None and len(resume.records) > resume.expected_count)
    ):
        raise ValueError("invalid resume")
    c = resume or Cursor(scope_id, first_token)
    token = c.next_token
    rec = list(c.records)
    seen = set()
    visited = list(c.visited_tokens)
    count = c.expected_count
    req = 0
    start = clock()

    def remaining_seconds() -> float:
        return limits.max_seconds - (clock() - start)

    async def save_checkpoint(cursor: Cursor[T]) -> str | None:
        remaining = remaining_seconds()
        if remaining <= 0:
            return "max_seconds"
        if checkpoint is None:
            return None
        deadline = asyncio.timeout(remaining)
        try:
            async with deadline:
                await checkpoint(cursor)
        except TimeoutError:
            if deadline.expired():
                return "checkpoint_timeout"
            raise
        if remaining_seconds() <= 0:
            return "max_seconds"
        return None

    for r in rec:
        try:
            k = record_key(r)
        except Exception as exc:
            raise ValueError("invalid resume") from exc
        if not isinstance(k, str) or not k or len(k) > 4096 or k in seen:
            raise ValueError("invalid resume")
        seen.add(k)
    checkpoint_stop = await save_checkpoint(c)
    if checkpoint_stop is not None:
        return Result(c, False, checkpoint_stop, req)
    while True:
        if req >= limits.max_requests:
            return Result(c, False, "max_requests", req)
        remaining = remaining_seconds()
        if remaining <= 0:
            return Result(c, False, "max_seconds", req)
        if token in visited:
            return Result(c, False, "loop", req)
        if not _valid_token(token):
            return Result(c, False, "invalid_page", req)
        assert isinstance(token, str)
        req += 1
        try:
            page = await asyncio.wait_for(fetch_page(token), remaining)
        except TimeoutError:
            return Result(c, False, "timeout", req)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return Result(c, False, "fetch_error", req)
        if (
            not isinstance(page, Page)
            or type(page.records) is not tuple
            or not _valid_optional_token(page.next_token)
            or type(page.exhausted) is not bool
            or not _valid_count(page.expected_count)
            or (page.exhausted and page.next_token is not None)
        ):
            return Result(c, False, "invalid_page", req)
        next_token = page.next_token
        if next_token is not None:
            assert isinstance(next_token, str)
        if page.expected_count is not None and count is not None and page.expected_count != count:
            return Result(c, False, "invalid_page", req)
        next_count = page.expected_count if page.expected_count is not None else count
        fresh: list[T] = []
        page_seen: set[str] = set()
        for r in page.records:
            try:
                k = record_key(r)
            except Exception:  # noqa: BLE001
                return Result(c, False, "invalid_page", req)
            if not isinstance(k, str) or not k or len(k) > 4096:
                return Result(c, False, "invalid_page", req)
            if k not in seen and k not in page_seen:
                page_seen.add(k)
                fresh.append(r)
        if len(rec) + len(fresh) > limits.max_records:
            return Result(c, False, "max_records", req)
        if next_count is not None and len(rec) + len(fresh) > next_count:
            return Result(c, False, "invalid_page", req)
        if not fresh and next_token is not None:
            return Result(c, False, "no_new_records", req)
        seen.update(page_seen)
        rec += fresh
        visited.append(token)
        count = next_count
        c = _cursor(c, next_token, tuple(rec), tuple(visited), len(visited), next_count)
        checkpoint_stop = await save_checkpoint(c)
        if checkpoint_stop is not None:
            return Result(c, False, checkpoint_stop, req)
        if page.exhausted:
            return Result(
                c,
                count is None or len(rec) == count,
                "complete" if count is None or len(rec) == count else "count_mismatch",
                req,
            )
        if next_token is None:
            return Result(c, False, "missing_next", req)
        token = next_token
