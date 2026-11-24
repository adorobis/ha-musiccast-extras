"""Numbers: balance, volume in dB, Auro-Matic strength, alarm volume, preset."""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
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
        if device.netusb_inputs or f.tuner:
            entities += [PresetNumber(device, zone_id) for zone_id in f.zones]
    _remove_preset_sensors(hass, entry)
    async_add_entities(entities)


def _remove_preset_sensors(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the Preset sensors of 1.9.0, replaced by the Preset numbers."""
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            reg_entry.domain == "sensor"
            and reg_entry.platform == DOMAIN
            and reg_entry.unique_id.endswith("_preset")
        ):
            registry.async_remove(reg_entry.entity_id)


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


class PresetNumber(MusicCastExtrasEntity, NumberEntity):
    """The preset a zone plays now (0 when none); setting it recalls that preset.

    For the tuner the device reports the preset of the tuned station; for
    network sources see MusicCastExtrasDevice.playing_netusb_preset.

    Setting a number recalls the tuner preset of the current band while the
    zone plays the tuner, else the network/USB preset (which also turns the
    zone on and selects its input). 0 does nothing.
    """

    _attr_icon = "mdi:star-box-outline"
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_step = 1

    def __init__(self, device: MusicCastExtrasDevice, zone_id: str) -> None:
        """Initialize."""
        super().__init__(device, "preset", zone_id=zone_id)
        raw = device.features.raw
        self._netusb_max = (raw.get("netusb", {}).get("preset") or {}).get("num", 0)
        self._tuner_max = (raw.get("tuner", {}).get("preset") or {}).get("num", 0)
        self._tuner_type = (raw.get("tuner", {}).get("preset") or {}).get("type")
        self._attr_native_max_value = max(self._netusb_max, self._tuner_max) or 40

    def _match(self) -> tuple[int, str | None]:
        zone = self.device.core.data.zones.get(self.zone_id)
        if zone is not None and zone.power == "on" and zone.input == "tuner":
            tuner = self.data.get("tuner", {})
            band = tuner.get("band") or ""
            return (tuner.get(band) or {}).get("preset") or 0, None
        return self.device.playing_netusb_preset(self.zone_id)

    @property
    def native_value(self) -> int:
        """Return the preset number, 0 if none matches."""
        return self._match()[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the preset's name."""
        return {"preset_name": self._match()[1]}

    async def async_set_native_value(self, value: float) -> None:
        """Recall the preset."""
        number = int(value)
        if number <= 0:
            self.async_write_ha_state()
            return
        zone = self.device.core.data.zones.get(self.zone_id)
        band = self.data.get("tuner", {}).get("band")
        if zone is not None and zone.power == "on" and zone.input == "tuner" and band:
            if number > self._tuner_max:
                raise ServiceValidationError(
                    f"The tuner has presets 1-{self._tuner_max}, not {number}"
                )
            if self._tuner_type == "common":
                band = "common"
            await self.device.command(
                f"tuner/recallPreset?zone={self.zone_id}&band={band}&num={number}"
            )
            return
        if number not in self.device.core.data.netusb_preset_list:
            raise ServiceValidationError(f"Preset {number} is empty")
        await self.device.command(f"netusb/recallPreset?zone={self.zone_id}&num={number}")
