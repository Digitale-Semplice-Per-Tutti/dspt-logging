"""The six fields a line carries about where it comes from."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import pytest

from dspt_logging import context

Records = Callable[[], list[dict[str, Any]]]


def test_nothing_is_set_by_default() -> None:
    assert context.current() == {}


def test_bound_fields_are_visible_only_inside_the_block() -> None:
    with context.bound(request_id="r1", user_id=7, tenant="tenant-a"):
        assert context.current() == {"request_id": "r1", "user_id": 7, "tenant": "tenant-a"}
    assert context.current() == {}


def test_bind_and_unbind_by_token() -> None:
    tokens = context.bind(job="nightly.cleanup")
    assert context.current() == {"job": "nightly.cleanup"}
    context.unbind(tokens)
    assert context.current() == {}


def test_the_field_set_is_closed() -> None:
    """A typo would bind something nothing ever reads, and a new field is a
    decision for the package: every name is a searchable field in six apps."""
    with pytest.raises(TypeError, match="username"):
        context.bind(username="mario")
    with pytest.raises(TypeError, match="username"):
        with context.bound(username="mario"):
            pass


def test_a_task_does_not_see_another_tasks_context() -> None:
    async def one() -> dict[str, Any]:
        with context.bound(request_id="a"):
            await asyncio.sleep(0)
            return context.current()

    async def two() -> dict[str, Any]:
        await asyncio.sleep(0)
        return context.current()

    async def main() -> tuple[dict[str, Any], dict[str, Any]]:
        first, second = await asyncio.gather(one(), two())
        return first, second

    seen_one, seen_two = asyncio.run(main())
    assert seen_one == {"request_id": "a"}
    assert seen_two == {}


def test_the_context_follows_a_task_and_a_thread() -> None:
    async def main() -> dict[str, Any]:
        with context.bound(turn_id="turn-9"):
            return await asyncio.create_task(asyncio.to_thread(context.current))

    assert asyncio.run(main()) == {"turn_id": "turn-9"}


def test_bound_fields_reach_every_record_and_an_explicit_field_wins(
    json_lines: Records,
) -> None:
    log = logging.getLogger("test.context")
    with context.bound(request_id="req-1", thread_id="t-1"):
        log.info("inside")
        log.info("override", extra={"thread_id": "t-2"})
    log.info("outside")

    inside, override, outside = json_lines()
    assert inside["request_id"] == "req-1"
    assert inside["thread_id"] == "t-1"
    assert override["thread_id"] == "t-2"
    assert override["request_id"] == "req-1"
    assert "request_id" not in outside
    assert "thread_id" not in outside


def test_a_bound_tenant_overrides_the_processs_own(json_lines: Records) -> None:
    """A job walking the tenants names the one it is on; the deployment's own
    slug is what a line says when nothing else does."""
    with context.bound(tenant="tenant-b"):
        logging.getLogger("test.context").info("per tenant")

    assert json_lines()[0]["tenant"] == "tenant-b"
    assert json.dumps(json_lines()).count("tenant-b") == 1
