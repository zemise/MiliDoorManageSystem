"""Button entities for Mili Door."""

from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import MiliDoorEntity, entry_key
from .runtime import MiliDoorRuntime


@dataclass(frozen=True, kw_only=True)
class MiliDoorButtonDescription(ButtonEntityDescription):
    """Describe a gateway command button."""

    command: str


BUTTONS = (
    MiliDoorButtonDescription(
        key="unlock", translation_key="unlock", icon="mdi:door-open", command="unlock"
    ),
    MiliDoorButtonDescription(
        key="answer", translation_key="answer", icon="mdi:phone", command="answer"
    ),
    MiliDoorButtonDescription(
        key="hangup", translation_key="hangup", icon="mdi:phone-hangup", command="hangup"
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    runtime: MiliDoorRuntime = entry.runtime_data
    async_add_entities(
        MiliDoorButton(runtime, door_id, description)
        for door_id in runtime.door_ids
        for description in BUTTONS
    )


class MiliDoorButton(MiliDoorEntity, ButtonEntity):
    """Send a command to one door."""

    entity_description: MiliDoorButtonDescription

    def __init__(
        self,
        runtime: MiliDoorRuntime,
        door_id: str,
        description: MiliDoorButtonDescription,
    ) -> None:
        super().__init__(runtime, door_id)
        self.entity_description = description
        self._attr_unique_id = f"{entry_key(runtime)}_{door_id}_{description.key}"

    async def async_press(self) -> None:
        await self.runtime.async_command(self.door_id, self.entity_description.command)
