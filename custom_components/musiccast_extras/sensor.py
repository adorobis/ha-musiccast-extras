"""Sensors: signal info (one per parameter), Wi-Fi signal, tuner, alarms."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfDataRate
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .device import MusicCastExtrasDevice
from .entity import MusicCastExtrasEntity

# getSignalInfo "audio" keys -> entity key; created for the keys a zone returns.
SIGNAL_KEYS = {
    "format": "signal_format",
    "fs": "signal_sample_rate",
    "bit": "signal_bit_depth",
    "bitrate": "signal_bitrate",
    "error": "signal_error",
}
SIGNAL_ERRORS = {
    0: "none",
    1: "unknown",
    2: "check_cable",
    3: "check_pc",
    4: "disconnected",
    5: "over_fs",
}
UNKNOWN = ("", "---")
DAYS = ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Create the sensors each device supports."""
    entities: list[SensorEntity] = []
    for device in entry.runtime_data.values():
        for zone_id in device.features.zones:
            for api_key, key in SIGNAL_KEYS.items():
                if api_key in device.zone_signal(zone_id):
                    entities.append(SignalSensor(device, zone_id, api_key, key))
        network = (device.data or {}).get("network", {})
        if network.get("connection") == "wireless_lan" and "strength" in network.get(
            "wireless_lan", {}
        ):
            entities.append(WifiSignalSensor(device))
        if device.features.tuner:
            entities += [TunerSensor(device, key) for key in ("tuner_band", "tuner_station")]
            if device.features.tuner & {"rds", "dab"}:
                entities.append(TunerSensor(device, "tuner_text"))
        clock = (device.data or {}).get("clock", {})
        for index, _ in enumerate(clock.get("multi_alarm", {}).get("settings", [])):
            entities.append(MultiAlarmSensor(device, index))
    async_add_entities(entities)


class SignalSensor(MusicCastExtrasEntity, SensorEntity):
    """One parameter of a zone's playback signal."""

    def __init__(self, device: MusicCastExtrasDevice, zone_id: str, api_key: str, key: str) -> None:
        """Initialize."""
        super().__init__(device, key, zone_id=zone_id)
        self._api_key = api_key
        if api_key == "bitrate":
            self._attr_native_unit_of_measurement = UnitOfDataRate.KILOBITS_PER_SECOND
            self._attr_state_class = SensorStateClass.MEASUREMENT
        if api_key == "error":
            self._attr_device_class = "enum"
            self._attr_options = list(SIGNAL_ERRORS.values())
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> Any:
        """Return the value, None while the device reports none."""
        value = self.device.zone_signal(self.zone_id).get(self._api_key)
        if self._api_key == "error":
            return SIGNAL_ERRORS.get(value, "unknown") if value is not None else None
        if self._api_key == "bitrate":
            return value or None
        return None if value in UNKNOWN or value is None else value


class WifiSignalSensor(MusicCastExtrasEntity, SensorEntity):
    """Wi-Fi signal strength in percent, as reported by the device."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:wifi"

    def __init__(self, device: MusicCastExtrasDevice) -> None:
        """Initialize."""
        super().__init__(device, "wifi_signal")

    @property
    def native_value(self) -> int | None:
        """Return the strength; None on a wired connection or link down."""
        network = self.data.get("network", {})
        strength = network.get("wireless_lan", {}).get("strength")
        if network.get("connection") != "wireless_lan" or strength is None or strength < 0:
            return None
        return strength

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return SSID, channel and connection type."""
        network = self.data.get("network", {})
        wlan = network.get("wireless_lan", {})
        return {
            "connection": network.get("connection"),
            "ssid": wlan.get("ssid"),
            "channel": wlan.get("ch"),
        }


class TunerSensor(MusicCastExtrasEntity, SensorEntity):
    """Tuner band, station and RDS/DAB text."""

    def __init__(self, device: MusicCastExtrasDevice, key: str) -> None:
        """Initialize."""
        super().__init__(device, key)
        self._attr_icon = "mdi:radio"

    @property
    def native_value(self) -> str | None:
        """Return the value for the current band."""
        tuner = self.data.get("tuner", {})
        band = tuner.get("band")
        rds, dab = tuner.get("rds", {}), tuner.get("dab", {})
        if self.key == "tuner_band":
            return band.upper() if band else None
        if self.key == "tuner_station":
            if band == "dab":
                return dab.get("service_label") or None
            if band == "fm" and (rds.get("program_service") or "").strip():
                return rds["program_service"].strip()
            freq = tuner.get(band, {}).get("freq") if band in ("fm", "am") else None
            if freq:
                return f"{freq / 1000:.2f} MHz" if band == "fm" else f"{freq} kHz"
            return None
        if band == "dab":
            return dab.get("dls") or None
        if band == "fm":
            return (rds.get("radio_text_a") or rds.get("radio_text_b") or "").strip() or None
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return DAB details for the station sensor."""
        tuner = self.data.get("tuner", {})
        if self.key != "tuner_station" or tuner.get("band") != "dab":
            return None
        dab = tuner.get("dab", {})
        return {
            "ensemble": dab.get("ensemble_label"),
            "bit_rate": dab.get("bit_rate"),
            "quality": dab.get("quality"),
            "dab_plus": dab.get("dab_plus"),
            "program_type": dab.get("program_type"),
        }


class MultiAlarmSensor(MusicCastExtrasEntity, SensorEntity):
    """One alarm of a device with several alarms (read-only)."""

    _attr_icon = "mdi:alarm"

    def __init__(self, device: MusicCastExtrasDevice, index: int) -> None:
        """Initialize."""
        super().__init__(
            device,
            f"alarm_{index + 1}",
            translation_key="multi_alarm",
            placeholders={"number": str(index + 1)},
        )
        self._index = index

    def _setting(self) -> dict[str, Any]:
        settings = self.data.get("clock", {}).get("multi_alarm", {}).get("settings", [])
        return settings[self._index] if self._index < len(settings) else {}

    @property
    def native_value(self) -> str | None:
        """Return the alarm time while enabled, else "off"."""
        setting = self._setting()
        if not setting:
            return None
        if not setting.get("enable"):
            return "off"
        time = setting.get("time", "")
        return f"{time[:2]}:{time[2:]}" if len(time) == 4 else time

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return days, source and volume."""
        setting = self._setting()
        mask = setting.get("day_of_week") or 0
        return {
            "time": setting.get("time"),
            "days": [day for bit, day in enumerate(DAYS) if mask & (1 << bit)],
            "input": setting.get("resume", {}).get("input"),
            "volume": setting.get("volume"),
            "beep": setting.get("beep"),
            "snooze": setting.get("snooze"),
        }
