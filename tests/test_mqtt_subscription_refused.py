# Copyright 2026 Ori Nexus Systems LTD
# SPDX-License-Identifier: Apache-2.0

"""A subscription the broker refuses is not a subscription.

A broker whose ACL denies a topic answers SUBSCRIBE with a refusal and keeps
the connection up. Taken as success, the link read as up, the breaker stayed
closed and reads said no data was cached yet, for as long as the runtime ran.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from ori.hal.base import AdapterConnectionError, AdapterReadError
from ori.hal.mqtt_base import MqttCachedAdapter, subscription_refusal
from ori.hal.protocol_registry import MQTT_FAMILY_PROTOCOLS, make_adapter
from tests.test_mqtt_adapter import _FakeClient
from tests.test_mqtt_family_hostile_payloads import _LostConnection
from tests.test_mqtt_wildcard_topic import SENSORS

GRANTED = (0,)
NOT_AUTHORISED = (0x87,)


class _Broker:
    """A broker whose answer to each SUBSCRIBE the test decides.

    `answer` is the SUBACK codes, or a callable from the topic to them.
    """

    def __init__(self) -> None:
        self.answer: Any = GRANTED
        self.clients: list[_FakeClient] = []

    def client(self, **kwargs: Any) -> _FakeClient:
        broker = self

        class _Client(_FakeClient):
            async def subscribe(self, topic: str) -> Any:
                await super().subscribe(topic)
                answer = broker.answer
                return answer(topic) if callable(answer) else answer

        built = _Client(**kwargs)
        self.clients.append(built)
        return built


def _patched(broker: _Broker) -> Any:
    return (
        patch("ori.hal.mqtt_base._AIOMQTT_AVAILABLE", True),
        patch("ori.hal.mqtt_base._aiomqtt", SimpleNamespace(Client=broker.client)),
    )


def _config(protocol: str) -> dict[str, Any]:
    sensor_type, key = SENSORS[protocol]
    return {
        "sensor_id": "s1",
        "sensor_type": sensor_type,
        "broker_host": "192.168.1.50",
        key: "w/a",
    }


def _adapter(protocol: str) -> MqttCachedAdapter:
    adapter = make_adapter(protocol)
    assert isinstance(adapter, MqttCachedAdapter)
    return adapter


async def _spin(times: int = 20) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


def test_every_family_protocol_is_covered() -> None:
    assert set(SENSORS) == MQTT_FAMILY_PROTOCOLS


@pytest.mark.parametrize("granted", [(0,), (1,), [2], [0, 1, 2]])
def test_a_granted_qos_is_a_subscription(granted: Any) -> None:
    assert subscription_refusal(granted) is None


@pytest.mark.parametrize(
    ("granted", "named"),
    [
        pytest.param((0x80,), "SUBACK 0x80", id="v3-failure"),
        pytest.param([0x87], "SUBACK 0x87", id="v5-not-authorised"),
        pytest.param((0, 0x80), "SUBACK 0x80", id="one-filter-of-two"),
        pytest.param((3,), "SUBACK 0x03", id="not-a-qos"),
        pytest.param((-1,), "SUBACK -0x1", id="negative"),
        pytest.param((), "no code", id="empty"),
        pytest.param(None, "unreadable SUBACK code None", id="no-answer"),
        pytest.param(("0",), "unreadable SUBACK code '0'", id="text"),
        pytest.param((True,), "unreadable SUBACK code True", id="bool"),
    ],
)
def test_anything_else_is_a_refusal(granted: Any, named: str) -> None:
    refusal = subscription_refusal(granted)
    assert refusal is not None and named in refusal


def test_paho_reason_codes_are_read_as_the_broker_sent_them() -> None:
    """The type aiomqtt hands back from a real SUBACK, not a stand-in for it."""
    reasoncodes = pytest.importorskip("paho.mqtt.reasoncodes")
    packettypes = pytest.importorskip("paho.mqtt.packettypes")
    suback = packettypes.PacketTypes.SUBACK

    granted = reasoncodes.ReasonCode(suback, "Granted QoS 1")
    refused = reasoncodes.ReasonCode(suback, "Not authorized")

    assert subscription_refusal([granted]) is None
    assert subscription_refusal([refused]) == "SUBACK 0x87 Not authorized"
    assert subscription_refusal([granted, refused]) == "SUBACK 0x87 Not authorized"


@pytest.mark.parametrize("protocol", sorted(SENSORS))
async def test_a_refused_subscription_fails_the_connect(protocol: str) -> None:
    """Refusing the last topic an adapter asks for leaves nothing connected."""
    broker = _Broker()
    available, module = _patched(broker)
    with available, module:
        learner = _adapter(protocol)
        await learner.connect(_config(protocol))
        topics = list(broker.clients[-1].subscriptions)
        await learner.close()
        denied = topics[-1]
        broker.answer = lambda topic: NOT_AUTHORISED if topic == denied else GRANTED

        adapter = _adapter(protocol)
        with pytest.raises(AdapterConnectionError) as caught:
            await adapter.connect(_config(protocol))

    message = str(caught.value)
    assert "the broker refused the subscription" in message
    assert f"topic={denied!r}" in message
    assert "SUBACK 0x87" in message and "ACL" in message
    client = broker.clients[-1]
    assert client.subscriptions == topics, "the refusal was not the broker's answer"
    assert client.closed
    assert adapter._client is None and adapter._listener_task is None
    assert not adapter._link_up and not adapter.is_connected


@pytest.mark.parametrize("protocol", sorted(SENSORS))
async def test_a_subscription_refused_on_reconnect_keeps_the_link_down(
    protocol: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A reconnect the broker refuses is retried, named, and never read as up."""
    broker = _Broker()
    available, module = _patched(broker)
    with available, module, patch("ori.hal.mqtt_base.reconnect_delay", lambda _a: 0):
        adapter = _adapter(protocol)
        await adapter.connect(_config(protocol))
        try:
            first = broker.clients[0]
            listener = adapter._listener_task
            assert listener is not None
            broker.answer = NOT_AUTHORISED
            with caplog.at_level(logging.WARNING, logger="ori.hal.mqtt_base"):
                await first._queue.put(_LostConnection())
                await _spin()

            refused = broker.clients[1:]
            assert len(refused) > 1, "a refused reconnect was not retried"
            assert not adapter._link_up
            assert all(client.closed for client in refused[:-1])
            assert "the broker refused the subscription" in caplog.text
            assert "SUBACK 0x87" in caplog.text
            with pytest.raises(AdapterReadError, match="connection is down"):
                await adapter.read("s1")

            broker.answer = GRANTED
            await _spin()
            assert adapter._link_up
            assert adapter._listener_task is listener and not listener.done()
            assert adapter._client is broker.clients[-1]
            assert not broker.clients[-1].closed
        finally:
            await adapter.close()
