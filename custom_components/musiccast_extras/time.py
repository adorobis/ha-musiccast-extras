"""Time: the alarm time."""

from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .device import MusicCastExtrasDevice
from .entity import MusicCastExtrasEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Create an alarm time for devices with a (single) alarm."""
    async_add_entities(
        AlarmTime(device)
        for device in entry.runtime_data.values()
        if "oneday" in (device.data or {}).get("clock", {}).get("alarm", {})
    )


class AlarmTime(MusicCastExtrasEntity, TimeEntity):
    """When the alarm goes off."""

    _attr_icon = "mdi:alarm"

    def __init__(self, device: MusicCastExtrasDevice) -> None:
        """Initialize."""
        super().__init__(device, "alarm_time")

    @property
    def native_value(self) -> time | None:
        """Return the alarm time (the device stores "hhmm")."""
        value = self.data.get("clock", {}).get("alarm", {}).get("oneday", {}).get("time", "")
        if len(value) != 4 or not value.isdigit():
            return None
        return time(int(value[:2]), int(value[2:]))

    async def async_set_value(self, value: time) -> None:
        """Set the alarm time."""
        await self.device.command(
            "clock/setAlarmSettings",
            {"detail": {"day": "oneday", "time": f"{value.hour:02d}{value.minute:02d}"}},
        )
