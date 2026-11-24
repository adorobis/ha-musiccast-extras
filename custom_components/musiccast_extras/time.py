"""Time: the alarm time."""

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import MusicCastExtrasConfigEntry, MusicCastExtrasDevice
from .entity import MusicCastExtrasEntity, async_setup_device_entities

# One request at a time: the device's web server drops requests under load.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MusicCastExtrasConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create an alarm time for devices with a (single) alarm."""
    async_setup_device_entities(hass, entry, async_add_entities, _entities)


def _entities(device: MusicCastExtrasDevice) -> list[AlarmTime]:
    """Return the alarm time of one device, if it has one alarm."""
    if "oneday" in (device.data or {}).get("clock", {}).get("alarm", {}):
        return [AlarmTime(device)]
    return []


class AlarmTime(MusicCastExtrasEntity, TimeEntity):
    """When the alarm goes off."""

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
