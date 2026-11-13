"""Numbers: balance, volume in dB, Auro-Matic strength, alarm volume."""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .device import MusicCastExtrasDevice
from .entity import MusicCastExtrasEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Create the numbers each device supports, with the ranges it reports."""
    entities: list[NumberEntity] = []
    for device in entry.runtime_data.values():
        f = device.features
        for zone_id, funcs in f.zones.items():
            status = device.zone_status(zone_id)
            if (
                "balance" in funcs
                and "balance" in status
                and (r := f.zone_range(zone_id, "balance"))
            ):
                entities.append(ZoneNumber(device, zone_id, "balance", r))
            if (
                "actual_volume" in funcs
                and status.get("actual_volume", {}).get("mode") == "db"
                and (r := f.zone_range(zone_id, "actual_volume_db"))
            ):
                entities.append(ZoneNumber(device, zone_id, "actual_volume", r))
            if "auro_matic_strength" in status.get("auro_3d", {}) and (
                r := f.zone_range(zone_id, "auro_matic_strength")
            ):
                entities.append(ZoneNumber(device, zone_id, "auro_matic_strength", r))
        alarm = (device.data or {}).get("clock", {}).get("alarm", {})
        if "volume" in alarm and (r := f.clock_range("alarm_volume")):
            entities.append(AlarmVolume(device, r))
    async_add_entities(entities)


class ZoneNumber(MusicCastExtrasEntity, NumberEntity):
    """A numeric zone setting with the device's own range."""

    _attr_mode = NumberMode.SLIDER

    def __init__(
        self, device: MusicCastExtrasDevice, zone_id: str, key: str, rng: dict[str, Any]
    ) -> None:
        """Initialize."""
        super().__init__(device, key, zone_id=zone_id)
        self._attr_native_min_value = rng["min"]
        self._attr_native_max_value = rng["max"]
        self._attr_native_step = rng["step"]
        if key == "actual_volume":
            self._attr_native_unit_of_measurement = "dB"
            self._attr_icon = "mdi:volume-high"
        elif key == "balance":
            self._attr_icon = "mdi:scale-balance"
            self._attr_entity_category = EntityCategory.CONFIG
        else:
            self._attr_entity_category = EntityCategory.CONFIG

    @property
    def native_value(self) -> float | None:
        """Return the zone's value."""
        status = self.zone_status
        if self.key == "actual_volume":
            return status.get("actual_volume", {}).get("value")
        if self.key == "auro_matic_strength":
            return status.get("auro_3d", {}).get("auro_matic_strength")
        return status.get(self.key)

    async def async_set_native_value(self, value: float) -> None:
        """Set the value."""
        zone = self.zone_id
        if self.key == "actual_volume":
            await self.device.command(f"{zone}/setActualVolume?mode=db&value={value:g}")
        elif self.key == "auro_matic_strength":
            await self.device.command(f"{zone}/setAuroMaticStrength?value={int(value)}")
        else:
            await self.device.command(f"{zone}/setBalance?value={int(value)}")


class AlarmVolume(MusicCastExtrasEntity, NumberEntity):
    """Volume of the alarm."""

    _attr_icon = "mdi:alarm"

    def __init__(self, device: MusicCastExtrasDevice, rng: dict[str, Any]) -> None:
        """Initialize."""
        super().__init__(device, "alarm_volume")
        self._attr_native_min_value = rng["min"]
        self._attr_native_max_value = rng["max"]
        self._attr_native_step = rng["step"]

    @property
    def native_value(self) -> float | None:
        """Return the alarm volume."""
        return self.data.get("clock", {}).get("alarm", {}).get("volume")

    async def async_set_native_value(self, value: float) -> None:
        """Set the alarm volume."""
        await self.device.command("clock/setAlarmSettings", {"volume": int(value)})
