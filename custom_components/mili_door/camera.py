"""RTSP camera entity for Mili Door."""

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_RTSP_VIDEO_MOUNT, CONF_RTSP_VIDEO_PORT, DOMAIN
from .entity import entry_key
from .runtime import MiliDoorRuntime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    runtime: MiliDoorRuntime = entry.runtime_data
    async_add_entities(MiliDoorCamera(runtime, door_id) for door_id in runtime.door_ids)


class MiliDoorCamera(Camera):
    """Expose the gateway's on-demand RTSP video stream."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "camera"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, runtime: MiliDoorRuntime, door_id: str) -> None:
        super().__init__()
        self.runtime = runtime
        self.door_id = door_id
        self._attr_unique_id = f"{entry_key(runtime)}_{door_id}_camera"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_key(runtime)}:{door_id}")},
            name=f"Mili Door {door_id}",
            manufacturer="Mili",
            model="Door Gateway",
        )

    @property
    def available(self) -> bool:
        return self.runtime.online

    @property
    def use_stream_for_stills(self) -> bool:
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.runtime.add_listener(self._handle_runtime_update))

    def _handle_runtime_update(self) -> None:
        self.async_write_ha_state()

    async def stream_source(self) -> str | None:
        if not self.runtime.online:
            return None
        entry = self.runtime.entry
        mount = entry.data[CONF_RTSP_VIDEO_MOUNT].rstrip("/")
        path = mount if len(self.runtime.door_ids) == 1 else f"{mount}/{self.door_id}"
        return (
            f"rtsp://{entry.data[CONF_HOST]}:{entry.data[CONF_RTSP_VIDEO_PORT]}"
            f"{path}"
        )
