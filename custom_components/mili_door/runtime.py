"""Shared MQTT runtime for Mili Door entities."""

from __future__ import annotations

from collections.abc import Callable
import json
import logging
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.mqtt.models import ReceiveMessage
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .const import (
    CALL_STATE_ANSWERED,
    CALL_STATE_IDLE,
    CALL_STATE_RINGING,
    CONF_DOOR_IDS,
    CONF_TOPIC_PREFIX,
)

_LOGGER = logging.getLogger(__name__)

UpdateListener = Callable[[], None]
EventListener = Callable[[str, str, dict[str, Any]], None]


class MiliDoorRuntime:
    """Maintain one set of MQTT subscriptions for a gateway config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.topic_prefix: str = entry.data[CONF_TOPIC_PREFIX].rstrip("/")
        self.door_ids: tuple[str, ...] = tuple(entry.data[CONF_DOOR_IDS])
        self.online = False
        self.states: dict[str, dict[str, Any]] = {
            door_id: {"state": CALL_STATE_IDLE} for door_id in self.door_ids
        }
        self._listeners: set[UpdateListener] = set()
        self._event_listeners: set[EventListener] = set()
        self._unsubscribers: list[Callable[[], None]] = []
        self._last_incoming_session: dict[str, str] = {}

    @property
    def availability_topic(self) -> str:
        return f"{self.topic_prefix}/gateway/availability"

    async def async_start(self) -> None:
        """Subscribe after Home Assistant's MQTT integration is ready."""
        self._unsubscribers.extend(
            [
                await mqtt.async_subscribe(
                    self.hass, self.availability_topic, self._async_availability_message, 1
                ),
                await mqtt.async_subscribe(
                    self.hass, f"{self.topic_prefix}/+/state", self._async_state_message, 1
                ),
                await mqtt.async_subscribe(
                    self.hass, f"{self.topic_prefix}/+/event", self._async_event_message, 1
                ),
            ]
        )

    async def async_stop(self) -> None:
        """Remove all MQTT subscriptions."""
        while self._unsubscribers:
            self._unsubscribers.pop()()

    async def async_command(self, door_id: str, command: str) -> None:
        """Send a non-retained command to one door."""
        await mqtt.async_publish(
            self.hass,
            f"{self.topic_prefix}/{door_id}",
            command,
            qos=1,
            retain=False,
        )

    @callback
    def add_listener(self, listener: UpdateListener) -> Callable[[], None]:
        self._listeners.add(listener)

        def remove_listener() -> None:
            self._listeners.discard(listener)

        return remove_listener

    @callback
    def add_event_listener(self, listener: EventListener) -> Callable[[], None]:
        self._event_listeners.add(listener)

        def remove_listener() -> None:
            self._event_listeners.discard(listener)

        return remove_listener

    @callback
    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            listener()

    @callback
    def _door_id_from_topic(self, topic: str, suffix: str) -> str | None:
        base = f"{self.topic_prefix}/"
        if not topic.startswith(base):
            return None
        parts = topic[len(base) :].split("/")
        if len(parts) != 2 or parts[1] != suffix or parts[0] not in self.states:
            return None
        return parts[0]

    @staticmethod
    def _payload_text(message: ReceiveMessage) -> str:
        payload = message.payload
        return payload.decode(errors="replace") if isinstance(payload, bytes) else payload

    @callback
    def _async_availability_message(self, message: ReceiveMessage) -> None:
        self.online = self._payload_text(message).strip().lower() == "online"
        self._notify()

    @callback
    def _async_state_message(self, message: ReceiveMessage) -> None:
        door_id = self._door_id_from_topic(message.topic, "state")
        if door_id is None:
            return
        try:
            payload = json.loads(self._payload_text(message))
        except (TypeError, ValueError):
            _LOGGER.warning("Ignoring invalid state payload on %s", message.topic)
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("state"), str):
            return
        self.states[door_id] = payload
        self._notify()

    @callback
    def _async_event_message(self, message: ReceiveMessage) -> None:
        door_id = self._door_id_from_topic(message.topic, "event")
        if door_id is None:
            return
        try:
            payload = json.loads(self._payload_text(message))
        except (TypeError, ValueError):
            _LOGGER.warning("Ignoring invalid event payload on %s", message.topic)
            return
        if not isinstance(payload, dict):
            return
        event_type = payload.get("type") or payload.get("event_type")
        if not isinstance(event_type, str):
            return

        # Keep useful behavior with older gateway versions that only publish
        # events and have no retained state topic.
        state_by_event = {
            "incoming_call": CALL_STATE_RINGING,
            "answered": CALL_STATE_ANSWERED,
            "ended": CALL_STATE_IDLE,
        }
        if state := state_by_event.get(event_type):
            self.states[door_id] = {**payload, "state": state}
            self._notify()

        # The gateway intentionally repeats incoming_call while ringing so a
        # reconnecting client cannot miss it.  HA event entities must emit it
        # once per call, otherwise a notification automation would run every
        # second.
        if event_type == "incoming_call":
            session_id = str(payload.get("session_id", ""))
            if session_id and self._last_incoming_session.get(door_id) == session_id:
                return
            if session_id:
                self._last_incoming_session[door_id] = session_id

        for listener in tuple(self._event_listeners):
            listener(door_id, event_type, payload)
