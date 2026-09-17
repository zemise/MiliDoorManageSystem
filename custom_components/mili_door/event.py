"""Event entity for Mili Door calls."""

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import EVENT_TYPES
from .entity import MiliDoorEntity, entry_key
from .runtime import MiliDoorRuntime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    runtime: MiliDoorRuntime = entry.runtime_data
    async_add_entities(MiliDoorCallEvent(runtime, door_id) for door_id in runtime.door_ids)


class MiliDoorCallEvent(MiliDoorEntity, EventEntity):
    """Expose stateless gateway events."""

    _attr_translation_key = "call_event"
    _attr_icon = "mdi:doorbell"
    _attr_event_types = list(EVENT_TYPES)

    def __init__(self, runtime: MiliDoorRuntime, door_id: str) -> None:
        super().__init__(runtime, door_id)
        self._attr_unique_id = f"{entry_key(runtime)}_{door_id}_call_event"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.runtime.add_event_listener(self._handle_event))

    def _handle_event(self, door_id: str, event_type: str, payload: dict[str, Any]) -> None:
        if door_id != self.door_id or event_type not in EVENT_TYPES:
            return
        attributes = {
            key: value for key, value in payload.items() if key not in {"type", "event_type"}
        }
        self._trigger_event(event_type, attributes)
        self.async_write_ha_state()
