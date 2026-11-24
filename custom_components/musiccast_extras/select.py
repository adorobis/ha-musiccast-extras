"""Selects: Auro-3D modes, speaker pattern, alarm source, preset."""

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import MusicCastExtrasConfigEntry, MusicCastExtrasDevice
from .entity import MusicCastExtrasEntity

# One request at a time: the device's web server drops requests under load.
PARALLEL_UPDATES = 1

STEP_OPTIONS = ("prev", "next", "toggle")  # relative commands, not states

# auro_3d status key -> (getFeatures zone list, setter, parameter)
AURO = {
    "listening_mode": ("auro_3d_listening_mode_list", "setAuro3dListeningMode", "mode"),
    "auro_matic_preset": ("auro_matic_preset_list", "setAuroMaticPreset", "preset"),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MusicCastExtrasConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the selects each device supports, with the options it reports."""
    entities: list[SelectEntity] = []
    for device in entry.runtime_data.values():
        f = device.features
        for zone_id in f.zones:
            auro = device.zone_status(zone_id).get("auro_3d", {})
            for key, (list_key, setter, param) in AURO.items():
                options = [o for o in f.zone(zone_id).get(list_key, []) if o not in STEP_OPTIONS]
                if key in auro and options:
                    entities.append(AuroSelect(device, zone_id, key, options, setter, param))
        patterns = f.raw.get("system", {}).get("speaker_pattern_num")
        if "speaker_pattern" in f.system and patterns:
            entities.append(SpeakerPatternSelect(device, int(patterns)))
        alarm = (device.data or {}).get("clock", {}).get("alarm", {})
        inputs = f.raw.get("clock", {}).get("alarm_input_list", [])
        if "oneday" in alarm and inputs:
            entities.append(AlarmSourceSelect(device, inputs))
        if device.netusb_inputs:
            entities += [PresetSelect(device, zone_id) for zone_id in f.zones]
    async_add_entities(entities)


class AuroSelect(MusicCastExtrasEntity, SelectEntity):
    """Auro-3D listening mode or Auro-Matic preset."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        device: MusicCastExtrasDevice,
        zone_id: str,
        key: str,
        options: list[str],
        setter: str,
        param: str,
    ) -> None:
        """Initialize."""
        super().__init__(device, f"auro_{key}" if key == "listening_mode" else key, zone_id=zone_id)
        self._status_key = key
        self._attr_options = options
        self._setter = setter
        self._param = param

    @property
    def current_option(self) -> str | None:
        """Return the current mode."""
        value = self.zone_status.get("auro_3d", {}).get(self._status_key)
        return value if value in self.options else None

    async def async_select_option(self, option: str) -> None:
        """Change the mode."""
        await self.device.command(f"{self.zone_id}/{self._setter}?{self._param}={option}")


class SpeakerPatternSelect(MusicCastExtrasEntity, SelectEntity):
    """Speaker pattern (stored speaker setups of an AV receiver)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:speaker-multiple"

    def __init__(self, device: MusicCastExtrasDevice, count: int) -> None:
        """Initialize."""
        super().__init__(device, "speaker_pattern")
        self._attr_options = [str(n) for n in range(1, count + 1)]

    @property
    def current_option(self) -> str | None:
        """Return the active pattern."""
        value = self.data.get("func", {}).get("speaker_pattern")
        return str(value) if value is not None else None

    async def async_select_option(self, option: str) -> None:
        """Switch pattern."""
        await self.device.command(f"system/setSpeakerPattern?num={option}")


class AlarmSourceSelect(MusicCastExtrasEntity, SelectEntity):
    """What the alarm plays ("none" = beep only)."""

    _attr_icon = "mdi:alarm"

    def __init__(self, device: MusicCastExtrasDevice, inputs: list[str]) -> None:
        """Initialize."""
        super().__init__(device, "alarm_source")
        self._attr_options = list(inputs)

    @property
    def current_option(self) -> str | None:
        """Return the alarm's input."""
        oneday = self.data.get("clock", {}).get("alarm", {}).get("oneday", {})
        value = oneday.get("resume", {}).get("input")
        return value if value in self.options else None

    async def async_select_option(self, option: str) -> None:
        """Set the alarm's input."""
        await self.device.command(
            "clock/setAlarmSettings",
            {"detail": {"day": "oneday", "playback_type": "resume", "resume": {"input": option}}},
        )


class PresetSelect(MusicCastExtrasEntity, SelectEntity):
    """The network/USB preset a zone plays now, by name; selecting one recalls it.

    No option is selected (state unknown) while the zone plays none of the
    presets. Recalling turns the zone on and selects the preset's input.
    """

    _attr_icon = "mdi:star-box-multiple-outline"

    def __init__(self, device: MusicCastExtrasDevice, zone_id: str) -> None:
        """Initialize."""
        super().__init__(device, "preset_select", zone_id=zone_id)

    @property
    def options(self) -> list[str]:
        """Return the names of the stored presets."""
        return list(self.device.netusb_preset_labels())

    @property
    def current_option(self) -> str | None:
        """Return the preset playing now."""
        number, _ = self.device.playing_netusb_preset(self.zone_id)
        if not number:
            return None
        # The match is the lowest number of its name and input, as listed.
        return next(
            (label for label, n in self.device.netusb_preset_labels().items() if n == number),
            None,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the preset number playing now (0 if none)."""
        return {"preset_number": self.device.playing_netusb_preset(self.zone_id)[0]}

    async def async_select_option(self, option: str) -> None:
        """Recall the preset."""
        number = self.device.netusb_preset_labels().get(option)
        if number is None:
            raise ServiceValidationError(f"No preset named {option}")
        await self.device.command(f"netusb/recallPreset?zone={self.zone_id}&num={number}")
