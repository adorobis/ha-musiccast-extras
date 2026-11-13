"""Buttons: restart, scenes and the CD tray.

Restart uses the Yamaha Extended Control API (Basic spec rev. 2.00, 4.37
and 4.38) GET requests, offered only when listed in system func_list:
- requestSystemReboot ("system_reboot"): restarts the whole device;
- requestNetworkReboot ("network_reboot"): restarts the network module
  (streaming, MusicCast, DLNA), e.g. when it hangs while the amp still works.
Scenes (recallScene) are created per zone from "scene" and "scene_num".
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .device import MusicCastExtrasDevice
from .entity import MusicCastExtrasEntity

_LOGGER = logging.getLogger(__name__)

REBOOTS = (
    ("restart", "system_reboot", "system/requestSystemReboot"),
    ("restart_network", "network_reboot", "system/requestNetworkReboot"),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Create the buttons each device supports."""
    entities: list[ButtonEntity] = []
    for device in entry.runtime_data.values():
        f = device.features
        entities += [
            RebootButton(device, key, path) for key, feature, path in REBOOTS if feature in f.system
        ]
        for zone_id, funcs in f.zones.items():
            count = f.zone(zone_id).get("scene_num") or 0
            if "scene" in funcs:
                entities += [SceneButton(device, zone_id, n) for n in range(1, int(count) + 1)]
        if "cd" in f.inputs:
            entities.append(CommandButton(device, "cd_eject", "cd/toggleTray", "mdi:eject"))
    async_add_entities(entities)


class CommandButton(MusicCastExtrasEntity, ButtonEntity):
    """A button sending one GET request."""

    def __init__(self, device: MusicCastExtrasDevice, key: str, path: str, icon: str) -> None:
        """Initialize."""
        super().__init__(device, key)
        self._path = path
        self._attr_icon = icon

    async def async_press(self) -> None:
        """Send the request."""
        await self.device.command(self._path)


class RebootButton(MusicCastExtrasEntity, ButtonEntity):
    """Restart a MusicCast device or its network module."""

    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, device: MusicCastExtrasDevice, key: str, path: str) -> None:
        """Initialize."""
        super().__init__(device, key)
        self._path = path

    async def async_press(self) -> None:
        """Send the reboot request (no refresh: the device goes away)."""
        try:
            await self.device.get(self._path)
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(f"Restart request failed: {err}") from err
        _LOGGER.info("Requested %s on %s (%s)", self._path, self.device.model, self.device.host)


class SceneButton(MusicCastExtrasEntity, ButtonEntity):
    """Recall a scene of a zone."""

    _attr_icon = "mdi:palette"

    def __init__(self, device: MusicCastExtrasDevice, zone_id: str, number: int) -> None:
        """Initialize."""
        super().__init__(
            device,
            f"scene_{number}",
            zone_id=zone_id,
            translation_key="scene",
            placeholders={"number": str(number)},
        )
        self._number = number

    async def async_press(self) -> None:
        """Recall the scene."""
        await self.device.command(f"{self.zone_id}/recallScene?num={self._number}")
