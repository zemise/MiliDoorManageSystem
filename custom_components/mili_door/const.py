"""Constants for the Mili Door integration."""

from homeassistant.const import Platform

DOMAIN = "mili_door"

CONF_TOPIC_PREFIX = "topic_prefix"
CONF_DOOR_IDS = "door_ids"
CONF_RTSP_VIDEO_PORT = "rtsp_video_port"
CONF_RTSP_VIDEO_MOUNT = "rtsp_video_mount"

DEFAULT_TOPIC_PREFIX = "mili/door"
DEFAULT_DOOR_IDS = ["1"]
DEFAULT_RTSP_VIDEO_PORT = 8554
DEFAULT_RTSP_VIDEO_MOUNT = "/video"

CALL_STATE_IDLE = "idle"
CALL_STATE_RINGING = "ringing"
CALL_STATE_ANSWERED = "answered"

EVENT_TYPES = (
    "incoming_call",
    "incoming_call_ignored",
    "answered",
    "ended",
    "auto_unlock",
)

PLATFORMS = (
    Platform.BUTTON,
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.EVENT,
    Platform.CAMERA,
)
