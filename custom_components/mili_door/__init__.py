"""Mili Door Gateway integration."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS
from .runtime import MiliDoorRuntime


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Mili Door from a config entry."""
    runtime = MiliDoorRuntime(hass, entry)
    await runtime.async_start()
    entry.runtime_data = runtime
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Mili Door config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    runtime: MiliDoorRuntime = entry.runtime_data
    await runtime.async_stop()
    hass.data[DOMAIN].pop(entry.entry_id, None)
    return True
