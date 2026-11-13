"""Restart buttons for MusicCast devices.

The Yamaha Extended Control API (Basic spec rev. 2.00, 4.37 and 4.38) has
two GET requests, offered only when listed in system/getFeatures func_list:
- requestSystemReboot ("system_reboot"): restarts the whole device;
- requestNetworkReboot ("network_reboot"): restarts the network module
  (streaming, MusicCast, DLNA), e.g. when it hangs while the amp still works.

The buttons are attached to the devices of the core Yamaha MusicCast
integration and only created for what a device reports it supports.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

_LOGGER = logging.getLogger(__name__)

MUSICCAST_DOMAIN = "yamaha_musiccast"
API = "http://{host}/YamahaExtendedControl/v1/system/"


@dataclass(frozen=True, kw_only=True)
class RebootDescription(ButtonEntityDescription):
    """A reboot request and the func_list entry that enables it."""

    feature: str
    request: str


REBOOTS = (
    RebootDescription(
        key="restart",
        translation_key="restart",
        device_class=ButtonDeviceClass.RESTART,
        entity_category=EntityCategory.CONFIG,
        feature="system_reboot",
        request="requestSystemReboot",
    ),
    RebootDescription(
        key="restart_network",
        translation_key="restart_network",
        device_class=ButtonDeviceClass.RESTART,
        entity_category=EntityCategory.CONFIG,
        feature="network_reboot",
        request="requestNetworkReboot",
    ),
)


async def _func_list(coordinator: Any) -> list[str]:
    """Return the device's system func_list (cached by aiomusiccast if possible)."""
    musiccast = coordinator.musiccast
    features = getattr(musiccast, "_features", None) or {}
    func_list = features.get("system", {}).get("func_list")
    if func_list is None:
        features = await musiccast.device.request_json(API + "getFeatures")
        func_list = features.get("system", {}).get("func_list", [])
    return list(func_list)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add restart buttons to every loaded MusicCast device."""
    entities: list[MusicCastRebootButton] = []
    for mc_entry in hass.config_entries.async_loaded_entries(MUSICCAST_DOMAIN):
        coordinator = mc_entry.runtime_data
        try:
            func_list = await _func_list(coordinator)
        except Exception as err:
            _LOGGER.debug("No features from %s: %s", mc_entry.title, err)
            continue
        entities.extend(
            MusicCastRebootButton(coordinator, description)
            for description in REBOOTS
            if description.feature in func_list
        )
    async_add_entities(entities)


class MusicCastRebootButton(ButtonEntity):
    """Restart a MusicCast device or its network module."""

    _attr_has_entity_name = True
    entity_description: RebootDescription

    def __init__(self, coordinator: Any, description: RebootDescription) -> None:
        """Initialize the button."""
        self.coordinator = coordinator
        self.entity_description = description
        device_id = coordinator.data.device_id
        self._attr_unique_id = f"{device_id}_{description.key}"
        # Same identifiers as the core integration: the button shows up on
        # the existing MusicCast device.
        self._attr_device_info = DeviceInfo(identifiers={(MUSICCAST_DOMAIN, device_id)})

    async def async_press(self) -> None:
        """Send the reboot request."""
        musiccast = self.coordinator.musiccast
        try:
            result = await musiccast.device.request_json(API + self.entity_description.request)
        except Exception as err:
            raise HomeAssistantError(f"Restart request failed: {err}") from err
        code = result.get("response_code") if isinstance(result, dict) else None
        if code not in (0, None):
            raise HomeAssistantError(f"The device refused the restart (response code {code})")
        _LOGGER.info(
            "Requested %s on %s (%s)",
            self.entity_description.request,
            self.coordinator.data.model_name,
            musiccast.device.ip,
        )
