"""Per-device data for the extra entities.

For every loaded Yamaha MusicCast config entry, MusicCastExtrasDevice reads
system/getFeatures once and keeps a coordinator that fetches only what the
device reports it supports: zone status (fields the core integration
ignores), signal info, function status, network status, clock (alarm),
tuner and CD play info. Nothing is tied to model names; entities are
created from these features when the device is added.

Data is polled every minute and refreshed shortly after the device sends an
event (the core integration's UDP push), so changes made on the device or
in the MusicCast app show up quickly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

API = "http://{host}/YamahaExtendedControl/v1/"
POLL_INTERVAL = timedelta(seconds=60)
EVENT_COOLDOWN = 5  # seconds between refreshes caused by device events
MUSICCAST_DOMAIN = "yamaha_musiccast"
MAIN_ZONE = "main"


# host -> device, for the media player patch (filled while the entry is loaded)
REGISTRY: dict[str, MusicCastExtrasDevice] = {}


class MusicCastApiError(HomeAssistantError):
    """The device answered with a response code other than 0."""


@dataclass
class Features:
    """What a device supports, from system/getFeatures."""

    raw: dict[str, Any]
    system: set[str] = field(default_factory=set)
    zones: dict[str, set[str]] = field(default_factory=dict)
    netusb: set[str] = field(default_factory=set)
    tuner: set[str] = field(default_factory=set)
    clock: set[str] = field(default_factory=set)
    inputs: set[str] = field(default_factory=set)

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> Features:
        """Parse a getFeatures response."""
        system = raw.get("system", {})
        return cls(
            raw=raw,
            system=set(system.get("func_list", [])),
            zones={z["id"]: set(z.get("func_list", [])) for z in raw.get("zone", [])},
            netusb=set(raw.get("netusb", {}).get("func_list", [])),
            tuner=set(raw.get("tuner", {}).get("func_list", [])),
            clock=set(raw.get("clock", {}).get("func_list", [])),
            inputs={i["id"] for i in system.get("input_list", [])},
        )

    def zone(self, zone_id: str) -> dict[str, Any]:
        """Return the raw feature block of a zone."""
        return next((z for z in self.raw.get("zone", []) if z["id"] == zone_id), {})

    def zone_range(self, zone_id: str, range_id: str) -> dict[str, Any] | None:
        """Return min/max/step of a zone setting, if the device reports one."""
        return next(
            (r for r in self.zone(zone_id).get("range_step", []) if r["id"] == range_id), None
        )

    def clock_range(self, range_id: str) -> dict[str, Any] | None:
        """Return min/max/step of a clock setting."""
        return next(
            (r for r in self.raw.get("clock", {}).get("range_step", []) if r["id"] == range_id),
            None,
        )

    def zones_with(self, func: str) -> list[str]:
        """Return the zones that list a function."""
        return [z for z, funcs in self.zones.items() if func in funcs]


class MusicCastExtrasDevice(DataUpdateCoordinator[dict[str, Any]]):
    """Extra data of one MusicCast device."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, core_coordinator: Any, features: Features
    ) -> None:
        """Initialize for a loaded core MusicCast coordinator."""
        self.core = core_coordinator
        self.musiccast = core_coordinator.musiccast
        self.features = features
        self.device_id: str = core_coordinator.data.device_id
        self.model: str = core_coordinator.data.model_name
        self._unsub_event: Any = None
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {self.model} {self.host}",
            update_interval=POLL_INTERVAL,
            request_refresh_debouncer=Debouncer(
                hass, _LOGGER, cooldown=EVENT_COOLDOWN, immediate=False
            ),
        )

    @property
    def host(self) -> str:
        """Return the device IP address."""
        return self.musiccast.device.ip

    # ------------------------------------------------------------ requests

    async def get(self, path: str) -> dict[str, Any]:
        """GET a YXC path such as "main/getStatus"; raise on error codes."""
        return self._check(path, await self.musiccast.device.request_json(API + path))

    async def post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        """POST JSON to a YXC path; raise on error codes."""
        return self._check(path, await self.musiccast.device.request_json((API + path, data)))

    @staticmethod
    def _check(path: str, result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise MusicCastApiError(f"{path}: unexpected answer")
        code = result.get("response_code", 0)
        if code != 0:
            raise MusicCastApiError(f"{path}: response code {code}")
        return result

    async def command(self, path: str, data: dict[str, Any] | None = None) -> None:
        """Send a setting change and refresh the data right after."""
        try:
            if data is None:
                await self.get(path)
            else:
                await self.post(path, data)
        except MusicCastApiError:
            raise
        except Exception as err:
            raise HomeAssistantError(f"{self.model} ({self.host}): {path} failed: {err}") from err
        await self.async_refresh()

    # ------------------------------------------------------------ polling

    async def _async_update_data(self) -> dict[str, Any]:
        f = self.features
        data: dict[str, Any] = {"zones": {}}
        try:
            for zone_id, funcs in f.zones.items():
                zone: dict[str, Any] = {"status": await self.get(f"{zone_id}/getStatus")}
                if "signal_info" in funcs:
                    zone["signal"] = (await self.get(f"{zone_id}/getSignalInfo")).get("audio", {})
                data["zones"][zone_id] = zone
            data["func"] = await self.get("system/getFuncStatus")
            data["network"] = await self.get("system/getNetworkStatus")
            if f.clock & {"alarm", "multi_alarm"}:
                data["clock"] = await self.get("clock/getSettings")
            if f.tuner:
                data["tuner"] = await self.get("tuner/getPlayInfo")
            if "cd" in f.inputs:
                data["cd"] = await self.get("cd/getPlayInfo")
                data["cd_updated"] = dt_util.utcnow()
        except Exception as err:
            raise UpdateFailed(f"{self.model} ({self.host}): {err}") from err
        return data

    @callback
    def async_start_events(self) -> None:
        """Refresh soon after the device reports a change."""

        @callback
        def _on_event() -> None:
            self.hass.async_create_task(self.async_request_refresh())

        self.musiccast.register_callback(_on_event)
        self._unsub_event = _on_event

    @callback
    def async_stop_events(self) -> None:
        """Stop listening to device events."""
        if self._unsub_event is not None:
            self.musiccast.remove_callback(self._unsub_event)
            self._unsub_event = None

    # ------------------------------------------------------------ helpers

    def zone_status(self, zone_id: str) -> dict[str, Any]:
        """Return the last getStatus of a zone."""
        return (self.data or {}).get("zones", {}).get(zone_id, {}).get("status", {})

    def zone_signal(self, zone_id: str) -> dict[str, Any]:
        """Return the last getSignalInfo audio block of a zone."""
        return (self.data or {}).get("zones", {}).get(zone_id, {}).get("signal", {})

    def zone_name(self, zone_id: str) -> str:
        """Return the name of the core MusicCast device (zone) in Home Assistant.

        Uses the user's rename if there is one, so the extras' entity IDs
        match the core ones; falls back to the name the device reports.
        """
        identifier = self.core_device_identifier(zone_id)
        dev_reg = dr.async_get(self.hass)
        for entry in dr.async_entries_for_config_entry(dev_reg, self.core.config_entry.entry_id):
            if identifier in entry.identifiers:
                return entry.name_by_user or entry.name or zone_id
        zone = self.core.data.zones.get(zone_id)
        return (zone.name if zone and zone.name else None) or f"{self.model} {zone_id}"

    def core_device_identifier(self, zone_id: str) -> tuple[str, str]:
        """Return the core integration's device identifier for a zone."""
        if zone_id == MAIN_ZONE:
            return (MUSICCAST_DOMAIN, self.device_id)
        return (MUSICCAST_DOMAIN, f"{self.device_id}_{zone_id}")


async def async_create_devices(
    hass: HomeAssistant, extras_entry: ConfigEntry
) -> dict[str, MusicCastExtrasDevice]:
    """Create a MusicCastExtrasDevice for every loaded MusicCast entry."""
    devices: dict[str, MusicCastExtrasDevice] = {}
    for entry in hass.config_entries.async_loaded_entries(MUSICCAST_DOMAIN):
        core = entry.runtime_data
        try:
            raw = await core.musiccast.device.request_json(API + "system/getFeatures")
        except Exception as err:
            _LOGGER.warning("Skipping %s: no features (%s)", entry.title, err)
            continue
        device = MusicCastExtrasDevice(hass, extras_entry, core, Features.parse(raw))
        await device.async_refresh()
        if not device.last_update_success:
            _LOGGER.warning("Skipping %s: could not read its state", entry.title)
            continue
        device.async_start_events()
        devices[device.host] = device
    return devices
