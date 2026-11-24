"""MusicCast Extras: more of what MusicCast devices can do, in Home Assistant.

Extends the core Yamaha MusicCast integration: patches its media player
(patch.py: Tidal browsing, the device's own lists, CD control, metadata,
seeking, resume) and adds entities for what each device reports in
system/getFeatures (coordinator.py and the platforms). Set up from the UI;
removing the integration undoes the changes.
"""

import asyncio
from collections.abc import Callable
import logging
from typing import Any

from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    ConfigEntry,
    ConfigEntryChange,
    ConfigEntryState,
)
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import slugify

from . import browse, coordinator as extras_device, patch, url_playback, url_queue
from .const import DOMAIN, LEGACY_DOMAIN, error
from .coordinator import MusicCastExtrasConfigEntry, MusicCastExtrasDevice

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


async def async_setup_entry(hass: HomeAssistant, entry: MusicCastExtrasConfigEntry) -> None:
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
        raise error("incompatible", ConfigEntryError, missing=", ".join(missing))
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

    queue_store, queues = await _load_store(hass, "url_queue")
    url_queue.queues.clear()
    url_queue.queues.update(queues)

    def _save_queue() -> None:
        queue_store.async_delay_save(lambda: dict(url_queue.queues), 2)

    url_queue.on_queue_changed = _save_queue
    url_playback.on_ended = url_queue.on_ended

    patch.apply()
    _LOGGER.info("MusicCast extras enabled (path browsing for %s)", patch.PATH_INPUTS)

    unsub_started: Callable[[], None] | None = None

    @callback
    def _resume_after_restart(_event: Event | None = None) -> None:
        # A once-listener removes itself when it fires; calling its unsubscribe
        # afterwards (on unload) makes HA log "Unable to remove unknown job listener".
        nonlocal unsub_started
        unsub_started = None
        url_playback.async_resume_after_restart(hass)

    @callback
    def _cancel_resume_listener() -> None:
        if unsub_started is not None:
            unsub_started()

    if hass.is_running:
        _resume_after_restart()
    else:
        unsub_started = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, _resume_after_restart
        )
        entry.async_on_unload(_cancel_resume_listener)
    devices = await extras_device.async_create_devices(hass, entry)
    entry.runtime_data = devices
    _migrate_registry(hass, entry, devices)
    extras_device.REGISTRY.update(devices)
    for device in devices.values():
        # Show what the patch adds (CD track, seeking, ...) without waiting for
        # the core players' next update.
        device.core.async_update_listeners()
    _LOGGER.info("Extra entities for %d MusicCast device(s)", len(devices))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    lock = asyncio.Lock()

    @callback
    def _core_entry_changed(change: ConfigEntryChange, core_entry: ConfigEntry) -> None:
        if core_entry.domain == extras_device.MUSICCAST_DOMAIN:
            entry.async_create_background_task(
                hass,
                _async_sync_device(hass, entry, core_entry, lock),
                f"musiccast_extras sync {core_entry.title}",
            )

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, _core_entry_changed)
    )
    # Catch up with entries that changed while the devices were being created.
    for core_entry in hass.config_entries.async_entries(extras_device.MUSICCAST_DOMAIN):
        _core_entry_changed(ConfigEntryChange.UPDATED, core_entry)


async def _async_sync_device(
    hass: HomeAssistant,
    entry: MusicCastExtrasConfigEntry,
    core_entry: ConfigEntry,
    lock: asyncio.Lock,
) -> None:
    """Follow a MusicCast entry that loaded or unloaded after the extras were set up.

    A speaker that was offline when Home Assistant started, one added later
    or a reloaded entry gets its extras without reloading them; an unloaded
    entry's device and entities go away, so nothing uses its old connection.
    """
    async with lock:
        devices = entry.runtime_data
        current = next(
            (d for d in devices.values() if d.core.config_entry.entry_id == core_entry.entry_id),
            None,
        )
        loaded = core_entry.state is ConfigEntryState.LOADED
        if current is not None and (not loaded or current.core is not core_entry.runtime_data):
            _LOGGER.info("Removing the extras of %s", core_entry.title)
            current.async_stop_events()
            devices.pop(current.host, None)
            if extras_device.REGISTRY.get(current.host) is current:
                del extras_device.REGISTRY[current.host]
            async_dispatcher_send(hass, current.removed_signal)
            await current.async_shutdown()
            current = None
        if not loaded or current is not None:
            return
        device = await extras_device.async_create_device(hass, entry, core_entry)
        if device is None:
            return
        if entry.state is not ConfigEntryState.LOADED:
            device.async_stop_events()  # the extras were unloaded meanwhile
            return
        _LOGGER.info("Adding the extras of %s", core_entry.title)
        devices[device.host] = device
        extras_device.REGISTRY[device.host] = device
        device.core.async_update_listeners()
        async_dispatcher_send(hass, extras_device.SIGNAL_NEW_DEVICE, device)


@callback
def _migrate_registry(
    hass: HomeAssistant,
    entry: MusicCastExtrasConfigEntry,
    devices: dict[str, MusicCastExtrasDevice],
) -> None:
    """Bring entities registered by 1.2.x in line with this version.

    - 1.2.0 named every entity after "MusicCast Extras" (e.g.
      button.musiccast_extras_restart_2): rename them after the MusicCast
      device (zone); IDs the user changed are left alone.
    - 1.2.x put the entities on separate devices linked to the MusicCast
      ones: move them onto the MusicCast devices themselves and remove those
      devices (unless the user renamed one or put it in an area).
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    own_devices = {d.id for d in dr.async_entries_for_config_entry(dev_reg, entry.entry_id)}
    for reg_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        found = next(
            (
                (d, zone)
                for d in devices.values()
                if (zone := d.zone_of_unique_id(reg_entry.unique_id)) is not None
            ),
            None,
        )
        if found is None:
            continue
        device, zone_id = found
        changes: dict[str, Any] = {}
        platform, object_id = reg_entry.entity_id.split(".", 1)
        if object_id.startswith("musiccast_extras"):
            name = reg_entry.original_name or reg_entry.unique_id
            changes["new_entity_id"] = ent_reg.async_generate_entity_id(
                platform, slugify(f"{device.zone_name(zone_id)} {name}")
            )
        core_device = device.core_device_entry(zone_id)
        if core_device and reg_entry.device_id != core_device.id:
            changes["device_id"] = core_device.id
        if changes:
            _LOGGER.info("Updating %s: %s", reg_entry.entity_id, changes)
            ent_reg.async_update_entity(reg_entry.entity_id, **changes)
    for device_id in own_devices:
        if er.async_entries_for_device(ent_reg, device_id, include_disabled_entities=True):
            continue  # still used (no core device for that zone)
        own = dev_reg.async_get(device_id)
        if own is not None and own.name_by_user is None and own.area_id is None:
            _LOGGER.info("Removing the linked device %s, its entities moved", own.name)
            dev_reg.async_remove_device(device_id)


async def async_unload_entry(hass: HomeAssistant, entry: MusicCastExtrasConfigEntry) -> None:
    """Remove the entities and undo the patches."""
    await hass.config_entries.async_unload_platforms_or_raise(entry, PLATFORMS)
    for device in entry.runtime_data.values():
        device.async_stop_events()
    extras_device.REGISTRY.clear()
    patch.remove()
    browse.on_last_played_changed = None
    url_playback.on_resume_changed = None
    url_playback.on_ended = None
    url_queue.on_queue_changed = None
