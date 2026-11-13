"""MusicCast Extras: Tidal browsing, metadata, seeking and resume for MusicCast.

Extends the core Yamaha MusicCast integration (see patch.py for what and
how). Set up from the UI; removing the integration undoes the changes.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store

from . import browse, patch, url_playback
from .const import DOMAIN, LEGACY_DOMAIN

_LOGGER = logging.getLogger(__name__)

RESUME_SAVE_INTERVAL = 30  # seconds


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
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Undo the patches."""
    patch.remove()
    browse.on_last_played_changed = None
    url_playback.on_resume_changed = None
    return True
