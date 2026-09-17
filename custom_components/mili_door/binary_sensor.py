"""Binary sensor entities for Mili Door."""

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CALL_STATE_RINGING
from .entity import MiliDoorEntity, entry_key
from .runtime import MiliDoorRuntime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    runtime: MiliDoorRuntime = entry.runtime_data
    async_add_entities(MiliDoorRingingSensor(runtime, door_id) for door_id in runtime.door_ids)


class MiliDoorRingingSensor(MiliDoorEntity, BinarySensorEntity):
    """Indicate whether a door is currently ringing."""

    _attr_translation_key = "ringing"
    _attr_icon = "mdi:doorbell-video"

    def __init__(self, runtime: MiliDoorRuntime, door_id: str) -> None:
        super().__init__(runtime, door_id)
        self._attr_unique_id = f"{entry_key(runtime)}_{door_id}_ringing"

    @property
    def is_on(self) -> bool:
        return self.runtime.states[self.door_id].get("state") == CALL_STATE_RINGING
