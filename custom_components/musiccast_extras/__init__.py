"""MusicCast Extras: more of what MusicCast devices can do, in Home Assistant.

Extends the core Yamaha MusicCast integration: patches its media player
(patch.py: Tidal browsing, the device's own lists, CD control, metadata,
seeking, resume) and adds entities for what each device reports in
system/getFeatures (device.py and the platforms). Set up from the UI;
removing the integration undoes the changes.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.util import slugify

from . import browse, device as extras_device, patch, url_playback
from .const import DOMAIN, LEGACY_DOMAIN

_LOGGER = logging.getLogger(__name__)

RESUME_SAVE_INTERVAL = 30  # seconds
PLATFORMS = [
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TIME,
]


async def _load_store(hass: HomeAssistant, name: str) -> tuple[Store[dict], dict]:
    """Load a store, taking over the data of the earlier version once."""
    store: Store[dict] = Store(hass, 1, f"{DOMAIN}.{name}")
    data = await store.async_load()
    if data is None:
        legacy: Store[dict] = Store(hass, 1, f"{LEGACY_DOMAIN}.{name}")
        data = await legacy.async_load()
        if data:
            _LOGGER.info("Took over %s from %s", name, LEGACY_DOMAIN)
            await store.async_save(data)
    return store, data or {}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Patch the MusicCast media players."""
    missing = await hass.async_add_executor_job(patch.missing_hooks)
    if missing:
        ir.async_create_issue(
            hass,
            DOMAIN,
            "incompatible",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="incompatible",
            translation_placeholders={"missing": ", ".join(missing)},
        )
        raise ConfigEntryError(f"Incompatible Home Assistant version, missing: {missing}")
    ir.async_delete_issue(hass, DOMAIN, "incompatible")

    last_store, last_played = await _load_store(hass, "last_played")
    browse.last_played.clear()
    browse.last_played.update(last_played)

    def _save_last() -> None:
        last_store.async_delay_save(lambda: dict(browse.last_played), 5)

    browse.on_last_played_changed = _save_last

    resume_store, resume = await _load_store(hass, "url_resume")
    url_playback.resume.clear()
    url_playback.resume.update(resume)
    deadline = {"at": 0.0}

    def _save_resume() -> None:
        # Positions change every few seconds; a fixed deadline keeps the
        # delayed save from being pushed back for as long as it plays.
        now = hass.loop.time()
        if deadline["at"] <= now:
            deadline["at"] = now + RESUME_SAVE_INTERVAL
        resume_store.async_delay_save(lambda: dict(url_playback.resume), deadline["at"] - now)

    url_playback.on_resume_changed = _save_resume

    patch.apply()
    _LOGGER.info("MusicCast extras enabled (path browsing for %s)", patch.PATH_INPUTS)

    @callback
    def _resume_after_restart(_event: Event | None = None) -> None:
        url_playback.async_resume_after_restart(hass)

    if hass.is_running:
        _resume_after_restart()
    else:
        entry.async_on_unload(
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _resume_after_restart)
        )
    devices = await extras_device.async_create_devices(hass, entry)
    entry.runtime_data = devices
    _rename_legacy_entity_ids(hass, entry, devices)
    extras_device.REGISTRY.update(devices)
    _LOGGER.info("Extra entities for %d MusicCast device(s)", len(devices))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


@callback
def _rename_legacy_entity_ids(
    hass: HomeAssistant, entry: ConfigEntry, devices: dict[str, extras_device.MusicCastExtrasDevice]
) -> None:
    """Rename entity IDs from 1.2.0, which were all named after "MusicCast Extras".

    1.2.0 gave its devices no name, so their entities became e.g.
    button.musiccast_extras_restart_2. Rename them after the MusicCast device
    (zone) they belong to; IDs the user changed are left alone.
    """
    ent_reg = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        platform, object_id = reg_entry.entity_id.split(".", 1)
        if not object_id.startswith("musiccast_extras"):
            continue
        device = next(
            (d for d in devices.values() if reg_entry.unique_id.startswith(f"{d.device_id}_")),
            None,
        )
        if device is None:
            continue
        rest = reg_entry.unique_id[len(device.device_id) + 1 :]
        zone_id = rest.split("_", 1)[0]
        if zone_id not in device.features.zones or zone_id == extras_device.MAIN_ZONE:
            zone_id = extras_device.MAIN_ZONE
        name = reg_entry.original_name or rest
        new_id = ent_reg.async_generate_entity_id(
            platform, slugify(f"{device.zone_name(zone_id)} {name}")
        )
        _LOGGER.info("Renaming %s to %s", reg_entry.entity_id, new_id)
        ent_reg.async_update_entity(reg_entry.entity_id, new_entity_id=new_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Remove the buttons and undo the patches."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    for device in entry.runtime_data.values():
        device.async_stop_events()
    extras_device.REGISTRY.clear()
    patch.remove()
    browse.on_last_played_changed = None
    url_playback.on_resume_changed = None
    return True
