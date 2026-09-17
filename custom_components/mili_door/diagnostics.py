"""Diagnostics for Mili Door Gateway."""

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .runtime import MiliDoorRuntime


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return non-secret integration diagnostics."""
    runtime: MiliDoorRuntime = entry.runtime_data
    return {
        "config": dict(entry.data),
        "online": runtime.online,
        "states": runtime.states,
    }
