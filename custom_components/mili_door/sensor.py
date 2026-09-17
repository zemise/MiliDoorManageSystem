"""Sensor entities for Mili Door."""

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import MiliDoorEntity, entry_key
from .runtime import MiliDoorRuntime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    runtime: MiliDoorRuntime = entry.runtime_data
    async_add_entities(MiliDoorCallStateSensor(runtime, door_id) for door_id in runtime.door_ids)


class MiliDoorCallStateSensor(MiliDoorEntity, SensorEntity):
    """Expose the retained call state."""

    _attr_translation_key = "call_state"
    _attr_icon = "mdi:phone-in-talk"

    def __init__(self, runtime: MiliDoorRuntime, door_id: str) -> None:
        super().__init__(runtime, door_id)
        self._attr_unique_id = f"{entry_key(runtime)}_{door_id}_call_state"

    @property
    def native_value(self) -> str:
        return str(self.runtime.states[self.door_id].get("state", "idle"))

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            key: value
            for key, value in self.runtime.states[self.door_id].items()
            if key != "state"
        }
