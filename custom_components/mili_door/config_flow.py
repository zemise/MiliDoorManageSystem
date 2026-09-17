"""Config flow for Mili Door Gateway."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_HOST

from .const import (
    CONF_DOOR_IDS,
    CONF_RTSP_VIDEO_MOUNT,
    CONF_RTSP_VIDEO_PORT,
    CONF_TOPIC_PREFIX,
    DEFAULT_DOOR_IDS,
    DEFAULT_RTSP_VIDEO_MOUNT,
    DEFAULT_RTSP_VIDEO_PORT,
    DEFAULT_TOPIC_PREFIX,
    DOMAIN,
)


class MiliDoorConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure a Mili Door gateway through the UI."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            prefix = user_input[CONF_TOPIC_PREFIX].strip().strip("/")
            door_ids = [
                part.strip()
                for part in user_input[CONF_DOOR_IDS].split(",")
                if part.strip()
            ]
            mount = user_input[CONF_RTSP_VIDEO_MOUNT].strip()

            if not host:
                errors[CONF_HOST] = "required"
            elif not prefix:
                errors[CONF_TOPIC_PREFIX] = "required"
            elif not door_ids or any("/" in door_id for door_id in door_ids):
                errors[CONF_DOOR_IDS] = "invalid_door_ids"
            elif not mount.startswith("/"):
                errors[CONF_RTSP_VIDEO_MOUNT] = "invalid_mount"
            else:
                await self.async_set_unique_id(f"{host.lower()}|{prefix.lower()}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Mili Door ({host})",
                    data={
                        CONF_HOST: host,
                        CONF_TOPIC_PREFIX: prefix,
                        CONF_DOOR_IDS: list(dict.fromkeys(door_ids)),
                        CONF_RTSP_VIDEO_PORT: user_input[CONF_RTSP_VIDEO_PORT],
                        CONF_RTSP_VIDEO_MOUNT: mount,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_TOPIC_PREFIX, default=DEFAULT_TOPIC_PREFIX): str,
                vol.Required(CONF_DOOR_IDS, default=",".join(DEFAULT_DOOR_IDS)): str,
                vol.Required(CONF_RTSP_VIDEO_PORT, default=DEFAULT_RTSP_VIDEO_PORT): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=65535)
                ),
                vol.Required(CONF_RTSP_VIDEO_MOUNT, default=DEFAULT_RTSP_VIDEO_MOUNT): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
