"""Base entity for Mili Door."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .runtime import MiliDoorRuntime


class MiliDoorEntity(Entity):
    """Base class shared by non-camera Mili Door entities."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, runtime: MiliDoorRuntime, door_id: str) -> None:
        self.runtime = runtime
        self.door_id = door_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_key(runtime)}:{door_id}")},
            name=f"Mili Door {door_id}",
            manufacturer="Mili",
            model="Door Gateway",
        )

    @property
    def available(self) -> bool:
        return self.runtime.online

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.runtime.add_listener(self._handle_runtime_update))

    def _handle_runtime_update(self) -> None:
        self.async_write_ha_state()


def entry_key(runtime: MiliDoorRuntime) -> str:
    """Return the stable config-entry part of entity unique IDs."""
    return runtime.entry.unique_id or runtime.entry.entry_id
