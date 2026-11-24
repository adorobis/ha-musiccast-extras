"""Diagnostics for MusicCast Extras: what each device reports and what is kept for it."""

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import browse, patch, url_playback, url_queue
from .coordinator import MusicCastExtrasConfigEntry

# Network details, hardware addresses, serials and the media IDs of what was
# pushed (a media source may put an account or feed in its IDs).
TO_REDACT = {
    "default_gateway",
    "device_id",
    "dns_server_1",
    "dns_server_2",
    "ip_address",
    "ipv6",
    "mac_address",
    "media_id",
    "network_name",
    "ssid",
    "subnet_mask",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: MusicCastExtrasConfigEntry
) -> dict[str, Any]:
    """Return the features, last data and kept state of every device."""
    devices = [
        {
            "model": device.model,
            "device_id": device.device_id,
            "web_path": device.web_path,
            "last_update_success": device.last_update_success,
            "features": device.features.raw,
            "data": device.data,
            "pushed_media": url_playback.resume.get(device.host),
            "media_queue_length": len(url_queue.queues.get(device.host, [])),
            "last_played_list": browse.last_played.get(device.host),
            "click_mode": browse.click_mode.get(device.host, "play"),
        }
        for device in entry.runtime_data.values()
    ]
    return async_redact_data(
        {
            "patch": {"path_inputs": list(patch.PATH_INPUTS)},
            "devices": devices,
        },
        TO_REDACT,
    )
