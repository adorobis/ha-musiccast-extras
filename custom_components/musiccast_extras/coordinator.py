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

from dataclasses import dataclass, field
from datetime import timedelta
import logging
import re
from typing import Any

import aiohttp
from aiomusiccast.exceptions import MusicCastException
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import CORE_TAKEOVERS, DOMAIN, error

_LOGGER = logging.getLogger(__name__)

API = "http://{host}/YamahaExtendedControl/v1/"
POLL_INTERVAL = timedelta(seconds=60)
EVENT_COOLDOWN = 5  # seconds between refreshes caused by device events
# The devices' web server sometimes misses a request (more often when other
# clients poll it too): keep the last data through this many failed
# refreshes in a row before marking the entities unavailable.
TOLERATED_FAILURES = 3
MUSICCAST_DOMAIN = "yamaha_musiccast"
MAIN_ZONE = "main"
# The "Visit" link (a device has only one): the advanced "Web Setup" of AV
# receivers if the device serves it, else the basic "Web Settings" /
# "System Configuration" page every MusicCast device has at its root.
WEB_PAGES = ("/Setup/",)
WEB_PROBE_TIMEOUT = 5  # seconds
# A configuration URL this integration set (so it may update it).
OWN_URL = re.compile(r"http://[0-9.]+(/Setup)?/")


type MusicCastExtrasConfigEntry = ConfigEntry[dict[str, MusicCastExtrasDevice]]

# host -> device, for the media player patch (filled while the entry is loaded)
REGISTRY: dict[str, MusicCastExtrasDevice] = {}
# Sent with a device added after setup (a MusicCast entry that loaded later).
SIGNAL_NEW_DEVICE = f"{DOMAIN}_new_device"


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
    netusb_inputs: set[str] = field(default_factory=set)

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
            netusb_inputs={
                i["id"] for i in system.get("input_list", []) if i.get("play_info_type") == "netusb"
            },
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
        self,
        hass: HomeAssistant,
        entry: MusicCastExtrasConfigEntry,
        core_coordinator: Any,
        features: Features,
    ) -> None:
        """Initialize for a loaded core MusicCast coordinator."""
        self.core = core_coordinator
        self.musiccast = core_coordinator.musiccast
        self.features = features
        self.device_id: str = core_coordinator.data.device_id
        self.model: str = core_coordinator.data.model_name
        self._unsub_event: Any = None
        self._unsub_url: Any = None
        self.web_path = "/"
        self._failures = 0
        # Sent when the device goes away: its entities remove themselves.
        self.removed_signal = f"{DOMAIN}_removed_{id(self)}"
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
        """Return the answer, raise if it isn't one or reports an error code."""
        if not isinstance(result, dict):
            raise error("unexpected_answer", MusicCastApiError, path=path)
        code = result.get("response_code", 0)
        if code != 0:
            raise error("response_code", MusicCastApiError, path=path, code=code)
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
            raise error(
                "command_failed", model=self.model, host=self.host, path=path, error=err
            ) from err
        await self.async_refresh()

    # ------------------------------------------------------------ polling

    async def _async_update_data(self) -> dict[str, Any]:
        """Read what the device supports; keep the last data through a few failures."""
        if self.hass.is_stopping:
            # The HTTP session closes during shutdown; keep the last data.
            return self.data or {"zones": {}}
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
            reason = f"{type(err).__name__}: {err}".rstrip(": ")
            self._failures += 1
            if self.data and self._failures <= TOLERATED_FAILURES:
                _LOGGER.debug(
                    "%s (%s): refresh failed (%s), keeping the last data (%d/%d)",
                    self.model,
                    self.host,
                    reason,
                    self._failures,
                    TOLERATED_FAILURES,
                )
                return self.data
            raise error(
                "update_failed",
                UpdateFailed,
                model=self.model,
                host=self.host,
                reason=reason,
                count=self._failures,
            ) from err
        if self._failures:
            _LOGGER.debug("%s (%s): refresh works again", self.model, self.host)
        self._failures = 0
        return data

    @callback
    def async_start_events(self) -> None:
        """Refresh soon after the device reports a change."""

        @callback
        def _on_event() -> None:
            if not self.hass.is_stopping:
                self.hass.async_create_task(self.async_request_refresh())

        self.musiccast.register_callback(_on_event)
        self._unsub_event = _on_event
        self.async_update_configuration_url()
        self._unsub_url = self.async_add_listener(self._async_on_update)

    @callback
    def _async_on_update(self) -> None:
        """Follow the IP address; let the core players show the new CD data."""
        self.async_update_configuration_url()
        if "cd" in self.features.inputs:
            # The patched media player shows the CD's track and transport from
            # this data, which arrives after the core player wrote its state.
            self.core.async_update_listeners()

    @callback
    def async_stop_events(self) -> None:
        """Stop listening to device events."""
        if self._unsub_event is not None:
            self.musiccast.remove_callback(self._unsub_event)
            self._unsub_event = None
        if self._unsub_url is not None:
            self._unsub_url()
            self._unsub_url = None

    async def async_find_web_page(self) -> None:
        """Pick the device's web page: the first of WEB_PAGES it serves, else "/"."""
        session = async_get_clientsession(self.hass)
        for path in WEB_PAGES:
            try:
                async with session.get(
                    f"http://{self.host}{path}",
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=WEB_PROBE_TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        self.web_path = path
                        return
            except (aiohttp.ClientError, TimeoutError) as err:  # "not there"
                _LOGGER.debug("%s: no %s (%s)", self.host, path, err)

    @property
    def netusb_inputs(self) -> set[str]:
        """Return the inputs played by the network/USB module."""
        return self.features.netusb_inputs

    @property
    def ip_address(self) -> str:
        """Return the IP address the device reports, else the one used to reach it."""
        return (self.data or {}).get("network", {}).get("ip_address") or self.host

    @callback
    def async_update_configuration_url(self) -> None:
        """Point the "Visit" link of the core devices at the device's web page.

        The core integration sets no configuration URL. A URL someone else
        set is left alone; ours follows IP address changes.
        """
        url = f"http://{self.ip_address}{self.web_path}"
        dev_reg = dr.async_get(self.hass)
        for zone_id in self.features.zones:
            core_device = self.core_device_entry(zone_id)
            if core_device is None or core_device.configuration_url == url:
                continue
            current = core_device.configuration_url
            if current and not OWN_URL.fullmatch(current):
                continue
            dev_reg.async_update_device(core_device.id, configuration_url=url)

    # ------------------------------------------------------------ helpers

    def zone_status(self, zone_id: str) -> dict[str, Any]:
        """Return the last getStatus of a zone."""
        return (self.data or {}).get("zones", {}).get(zone_id, {}).get("status", {})

    def zone_signal(self, zone_id: str) -> dict[str, Any]:
        """Return the last getSignalInfo audio block of a zone."""
        return (self.data or {}).get("zones", {}).get(zone_id, {}).get("signal", {})

    def playing_netusb_preset(self, zone_id: str) -> tuple[int, str | None]:
        """Return the network/USB preset a zone plays now, (0, None) if none.

        The device does not say which preset was recalled: the preset with
        the zone's input whose name equals the station, album or track the
        netusb module plays is taken, the lowest number if several match.
        """
        core = self.core.data
        zone = core.zones.get(zone_id)
        if zone is None or zone.power != "on":
            return 0, None
        source = zone.input
        if (
            source not in self.netusb_inputs
            or core.netusb_input != source
            or core.netusb_playback == "stop"
        ):
            return 0, None
        playing = {core.netusb_artist, core.netusb_album, core.netusb_track} - {None, ""}
        for number, (preset_input, text) in sorted(core.netusb_preset_list.items()):
            if preset_input == source and text in playing:
                return number, text
        return 0, None

    def netusb_preset_labels(self) -> dict[str, int]:
        """Return label -> number of the stored network/USB presets.

        The label is the preset's name; the input is added when one name is
        stored for different inputs. A name stored twice for the same input
        plays the same thing, so it is listed once (lowest number).
        """
        presets = sorted(self.core.data.netusb_preset_list.items())
        inputs: dict[str, set[str]] = {}
        for _, (preset_input, text) in presets:
            inputs.setdefault(text, set()).add(preset_input)
        names = self.core.data.input_names or {}
        labels: dict[str, int] = {}
        for number, (preset_input, text) in presets:
            if not text:
                continue
            label = text
            if len(inputs[text]) > 1:
                label = f"{text} [{names.get(preset_input) or preset_input}]"
            labels.setdefault(label, number)
        return labels

    def core_device_entry(self, zone_id: str) -> dr.DeviceEntry | None:
        """Return the core integration's registered device for a zone."""
        identifier = self.core_device_identifier(zone_id)
        dev_reg = dr.async_get(self.hass)
        for entry in dr.async_entries_for_config_entry(dev_reg, self.core.config_entry.entry_id):
            if identifier in entry.identifiers:
                return entry
        return None

    def unique_id(self, key: str, zone_id: str = MAIN_ZONE) -> str:
        """Return the unique ID of an extras entity."""
        suffix = key if zone_id == MAIN_ZONE else f"{zone_id}_{key}"
        return f"{self.device_id}_{suffix}"

    def core_capability(self, key: str, zone_id: str = MAIN_ZONE) -> tuple[str, str] | None:
        """Return (platform, core unique ID) when the core has an entity for an extra.

        Newer aiomusiccast versions offer some extras (Surround AI, speaker
        pattern) as capabilities, which the core integration makes entities of.
        """
        for platform, takeover_key, capability_id, per_zone in CORE_TAKEOVERS:
            if takeover_key != key:
                continue
            data = self.core.data
            if per_zone:
                zone = data.zones.get(zone_id)
                capabilities = zone.capabilities if zone is not None else []
                core_device_id = self.core_device_identifier(zone_id)[1]
            else:
                capabilities = data.capabilities
                core_device_id = self.device_id
            if any(getattr(c, "id", None) == capability_id for c in capabilities):
                return platform, f"{core_device_id}_{capability_id}"
        return None

    def zone_of_unique_id(self, unique_id: str) -> str | None:
        """Return the zone an extras entity belongs to, None if not this device's."""
        if not unique_id.startswith(f"{self.device_id}_"):
            return None
        zone_id = unique_id[len(self.device_id) + 1 :].split("_", 1)[0]
        return zone_id if zone_id in self.features.zones else MAIN_ZONE

    def zone_name(self, zone_id: str) -> str:
        """Return the name of the core MusicCast device (zone) in Home Assistant.

        Uses the user's rename if there is one, so the extras' entity IDs
        match the core ones; falls back to the name the device reports.
        """
        if core_device := self.core_device_entry(zone_id):
            return core_device.name_by_user or core_device.name or zone_id
        zone = self.core.data.zones.get(zone_id)
        return (zone.name if zone and zone.name else None) or f"{self.model} {zone_id}"

    def core_device_identifier(self, zone_id: str) -> tuple[str, str]:
        """Return the core integration's device identifier for a zone."""
        if zone_id == MAIN_ZONE:
            return (MUSICCAST_DOMAIN, self.device_id)
        return (MUSICCAST_DOMAIN, f"{self.device_id}_{zone_id}")


async def async_create_devices(
    hass: HomeAssistant, extras_entry: MusicCastExtrasConfigEntry
) -> dict[str, MusicCastExtrasDevice]:
    """Create a MusicCastExtrasDevice for every loaded MusicCast entry."""
    devices: dict[str, MusicCastExtrasDevice] = {}
    for entry in hass.config_entries.async_loaded_entries(MUSICCAST_DOMAIN):
        if device := await async_create_device(hass, extras_entry, entry):
            devices[device.host] = device
    return devices


async def async_create_device(
    hass: HomeAssistant, extras_entry: MusicCastExtrasConfigEntry, core_entry: ConfigEntry
) -> MusicCastExtrasDevice | None:
    """Create the MusicCastExtrasDevice of a loaded MusicCast entry, None if it can't be read."""
    core = core_entry.runtime_data
    try:
        raw = await core.musiccast.device.request_json(API + "system/getFeatures")
    except MusicCastException as err:
        _LOGGER.warning("Skipping %s: no features (%s)", core_entry.title, err)
        return None
    device = MusicCastExtrasDevice(hass, extras_entry, core, Features.parse(raw))
    await device.async_refresh()
    if not device.last_update_success:
        _LOGGER.warning("Skipping %s: could not read its state", core_entry.title)
        return None
    await device.async_find_web_page()
    device.async_start_events()
    return device
