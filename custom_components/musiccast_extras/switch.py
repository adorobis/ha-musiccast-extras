"""Switches: zone sound options, HDMI outputs and the alarm."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .device import MusicCastExtrasDevice
from .entity import MusicCastExtrasEntity

# zone func_list entry -> (getStatus key, setter); only when getStatus has the key.
ZONE_SWITCHES = {
    "surround_ai": ("surround_ai", "setSurroundAI"),
    "adaptive_dsp_level": ("adaptive_dsp_level", "setAdaptiveDspLevel"),
    "mono": ("mono", "setMono"),
}
HDMI_OUTPUTS = (1, 2, 3)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Create the switches each device supports."""
    entities: list[SwitchEntity] = []
    for device in entry.runtime_data.values():
        f = device.features
        for zone_id, funcs in f.zones.items():
            status = device.zone_status(zone_id)
            for func, (status_key, setter) in ZONE_SWITCHES.items():
                if func in funcs and status_key in status:
                    entities.append(ZoneSwitch(device, zone_id, status_key, setter))
        func_status = (device.data or {}).get("func", {})
        for n in HDMI_OUTPUTS:
            if f"hdmi_out_{n}" in f.system and f"hdmi_out_{n}" in func_status:
                entities.append(HdmiOutSwitch(device, n))
        clock = (device.data or {}).get("clock", {})
        alarm_block = (
            "alarm" if "alarm" in clock else "multi_alarm" if "multi_alarm" in clock else None
        )
        if alarm_block:
            entities.append(
                AlarmSwitch(
                    device,
                    "alarm_on",
                    lambda c, b=alarm_block: c[b].get("alarm_on"),
                    lambda v: {"alarm_on": v},
                )
            )
        if "oneday" in clock.get("alarm", {}):
            entities.append(
                AlarmSwitch(
                    device,
                    "alarm_enabled",
                    lambda c: c["alarm"]["oneday"].get("enable"),
                    lambda v: {"detail": {"day": "oneday", "enable": v}},
                )
            )
            entities.append(
                AlarmSwitch(
                    device,
                    "alarm_beep",
                    lambda c: c["alarm"]["oneday"].get("beep"),
                    lambda v: {"detail": {"day": "oneday", "beep": v}},
                )
            )
            if "snooze" in f.clock:
                entities.append(
                    AlarmSwitch(
                        device,
                        "alarm_snooze",
                        lambda c: c["alarm"]["oneday"].get("snooze"),
                        lambda v: {"detail": {"day": "oneday", "snooze": v}},
                    )
                )
    async_add_entities(entities)


class ZoneSwitch(MusicCastExtrasEntity, SwitchEntity):
    """An on/off sound option of a zone (Surround:AI, adaptive DSP level, mono)."""

    def __init__(
        self, device: MusicCastExtrasDevice, zone_id: str, status_key: str, setter: str
    ) -> None:
        """Initialize."""
        super().__init__(device, status_key, zone_id=zone_id)
        self._status_key = status_key
        self._setter = setter

    @property
    def is_on(self) -> bool | None:
        """Return the zone's setting."""
        return self.zone_status.get(self._status_key)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable."""
        await self.device.command(f"{self.zone_id}/{self._setter}?enable=true")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable."""
        await self.device.command(f"{self.zone_id}/{self._setter}?enable=false")


class HdmiOutSwitch(MusicCastExtrasEntity, SwitchEntity):
    """An HDMI output of an AV receiver."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:video-input-hdmi"

    def __init__(self, device: MusicCastExtrasDevice, number: int) -> None:
        """Initialize."""
        super().__init__(
            device,
            f"hdmi_out_{number}",
            translation_key="hdmi_out",
            placeholders={"number": str(number)},
        )
        self._number = number

    @property
    def is_on(self) -> bool | None:
        """Return whether the output is enabled."""
        return self.data.get("func", {}).get(f"hdmi_out_{self._number}")

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the output."""
        await self.device.command(f"system/setHdmiOut{self._number}?enable=true")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the output."""
        await self.device.command(f"system/setHdmiOut{self._number}?enable=false")


class AlarmSwitch(MusicCastExtrasEntity, SwitchEntity):
    """An alarm setting (clock/setAlarmSettings)."""

    _attr_icon = "mdi:alarm"

    def __init__(
        self,
        device: MusicCastExtrasDevice,
        key: str,
        read: Callable[[dict[str, Any]], bool | None],
        payload: Callable[[bool], dict[str, Any]],
    ) -> None:
        """Initialize."""
        super().__init__(device, key)
        self._read = read
        self._payload = payload

    @property
    def is_on(self) -> bool | None:
        """Return the setting."""
        try:
            return self._read(self.data.get("clock", {}))
        except KeyError:
            return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable."""
        await self.device.command("clock/setAlarmSettings", self._payload(True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable."""
        await self.device.command("clock/setAlarmSettings", self._payload(False))
