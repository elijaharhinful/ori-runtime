# Copyright 2026 Ori Nexus Systems LTD
# SPDX-License-Identifier: Apache-2.0

"""Every action-log row the runtime writes carries a unique `record_key` (#859).

The refusal and suppression paths used to log with no key, stored as the empty
string, so their rows could not be named, resolved or deduplicated, and
gateway-api/v2 refuses an empty key on export. Driven through a real
`ActionDispatcher` and `StateStore`.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from ori.network.events import (
    ActionResult,
    ActionTier,
    OriEvent,
    ReasoningResult,
    SensorReading,
)
from ori.reasoning.action_dispatcher import ActionDispatcher
from ori.reasoning.elevator import SkillContext
from ori.state._action_records import _action_record_members
from ori.state.store import StateStore
from tests.waiting import drained


@dataclass
class _Skill:
    name: str = "record-key-skill"
    config: dict = field(default_factory=dict)
    triggers: list = field(default_factory=list)
    actions: dict = field(default_factory=dict)
    first_party: bool = False


def _context(store: StateStore) -> SkillContext:
    reading = SensorReading(
        sensor_id="load-current",
        sensor_type="current_clamp",
        value=5.0,
        unit="ampere",
        timestamp=int(time.time() * 1000),
        quality=1.0,
    )
    return SkillContext(
        skill=_Skill(),
        event=OriEvent.from_reading(reading, "dev-01"),
        state_store=store,
        trigger_name="t",
    )


def _reasoning() -> ReasoningResult:
    return ReasoningResult(
        text="observation", tier="rule", model="none", tokens_used=0, latency_ms=0
    )


async def _unknown_tier(d: ActionDispatcher, ctx: SkillContext) -> None:
    await d.dispatch("alert_sms", "X", ctx, _reasoning())


async def _suppressed(d: ActionDispatcher, ctx: SkillContext) -> None:
    # Logged as suppressed, then dispatched again as the safe default.
    d._relay_b_c_enabled = False
    await d.dispatch("trip_relay", ActionTier.HARD_PHYSICAL, ctx, _reasoning())


async def _tier_d_without_provenance(d: ActionDispatcher, ctx: SkillContext) -> None:
    await d._execute_immediately("trip_relay", ActionTier.SAFETY_CRITICAL, ctx)


async def _below_minimum_tier(d: ActionDispatcher, ctx: SkillContext) -> None:
    await d._execute_immediately("close_gas_valve", ActionTier.INFORMATIONAL, ctx)


PATHS: dict[str, Callable[[ActionDispatcher, SkillContext], Awaitable[None]]] = {
    "refused_unknown_tier": _unknown_tier,
    "suppressed": _suppressed,
    "refused_tier_d_without_provenance": _tier_d_without_provenance,
    "refused_below_minimum_tier": _below_minimum_tier,
}


def _rows(path: Path) -> list[sqlite3.Row]:
    with sqlite3.connect(str(path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT action_taken, record_kind, record_key, contributed_to,"
            " executed FROM action_log ORDER BY id"
        ).fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize("taken", sorted(PATHS))
async def test_each_refusal_path_writes_a_keyed_row(tmp_path: Path, taken: str) -> None:
    db = tmp_path / "s.db"
    store = StateStore(str(db))
    await store.open()
    try:
        dispatcher = ActionDispatcher(state_store=store)
        for _ in range(2):
            await PATHS[taken](dispatcher, _context(store))
        await drained(dispatcher)
    finally:
        await store.close()

    refused = [row for row in _rows(db) if row["action_taken"] == taken]
    assert len(refused) == 2
    keys = [row["record_key"] for row in refused]
    assert all(isinstance(key, str) and key for key in keys), keys
    assert len(set(keys)) == 2, keys
    for row in refused:
        assert _action_record_members(row)["record_key"] == row["record_key"]


@pytest.mark.asyncio
async def test_no_row_the_dispatcher_writes_is_unkeyed(tmp_path: Path) -> None:
    """Across every path at once, including the rows dispatch already keyed."""
    db = tmp_path / "s.db"
    store = StateStore(str(db))
    await store.open()
    try:
        dispatcher = ActionDispatcher(state_store=store)
        for run in PATHS.values():
            await run(dispatcher, _context(store))
        await dispatcher.dispatch("alert_sms", "A", _context(store), _reasoning())
        await drained(dispatcher)
    finally:
        await store.close()

    keys = [row["record_key"] for row in _rows(db)]
    assert len(keys) >= len(PATHS) + 1
    assert "" not in keys
    assert len(set(keys)) == len(keys)


def _result() -> ActionResult:
    return ActionResult(
        action_name="alert_sms",
        tier="A",
        executed=True,
        approved=None,
        action_taken="alert_sms",
        timestamp=1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["", None, 7], ids=["empty", "none", "not-text"])
@pytest.mark.parametrize("writer", ["log_action", "log_action_for_event"])
async def test_the_writer_refuses_a_row_without_a_key(
    tmp_path: Path, writer: str, key: Any
) -> None:
    store = StateStore(str(tmp_path / "s.db"))
    await store.open()
    try:
        write = getattr(store, writer)
        with pytest.raises(ValueError, match="non-empty record_key"):
            if writer == "log_action":
                await write(_result(), "t", record_key=key)
            else:
                await write(_result(), trigger_name="t", record_key=key)
        assert await store.get_action_log() == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_a_row_from_before_keys_is_exported_as_null(tmp_path: Path) -> None:
    """The migration default is `''`; no reader passes it on as a key."""
    db = tmp_path / "s.db"
    store = StateStore(str(db))
    await store.open()
    try:
        await store.log_action(_result(), "t", record_key="k-1")
    finally:
        await store.close()
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE action_log SET record_key = ''")
    (row,) = _rows(db)
    assert row["record_key"] == ""
    assert _action_record_members(row)["record_key"] is None
