"""Media-source URLs on MusicCast: metadata, seeking and resume.

The core integration pushes URLs to the speaker's DLNA renderer with the
title "HomeAssistant" and nothing else, so the speaker shows no cover, knows
no duration and offers no seeking; after a Home Assistant restart the stream
breaks and the Play button (a NetUSB command) can't restart a pushed stream.

For media sources that register a metadata provider in
``hass.data["media_info_providers"][<domain>]`` (for example the Radio Nowy
Świat integration, https://github.com/adorobis/ha-nowyswiat), this
module instead:
- sends DIDL metadata: title, artist, album, cover, duration, size and the
  DLNA "byte seek supported" flag;
- seeks with the AVTransport Seek action;
- remembers per speaker what was playing and where
  (.storage/musiccast_extras.url_resume), and on Play while the
  speaker is stopped pushes the stream again and seeks to that position;
- right after a Home Assistant restart, pushes such a stream again on
  speakers that still play it from their buffer (the connection to HA broke)
  at their live position, or that stopped recently without being stopped
  by the listener, at the saved position.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
import logging
import re
from typing import TYPE_CHECKING, Any
from xml.sax.saxutils import escape

import aiohttp
from homeassistant.components import media_source
from homeassistant.components.media_player import (
    MediaPlayerState,
    async_process_play_media_url,
)
from homeassistant.core import Event, EventStateChangedData, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from homeassistant.components.yamaha_musiccast.media_player import MusicCastMediaPlayer

_LOGGER = logging.getLogger(__name__)

PROVIDERS_KEY = "media_info_providers"
SERVER_INPUT = "server"
ORPHAN_WINDOW = 600  # a stopped stream this recent counts as broken by the restart
RESUME_MIN_SECONDS = 10  # don't seek for a resume this close to the start
SEEK_WAIT = 15  # seconds to wait for the renderer to start playing
DLNA_FLAGS = "DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01700000000000000000000000000000"
_STATE = re.compile(r"<CurrentTransportState>([^<]*)</CurrentTransportState>")
_REL_TIME = re.compile(r"<RelTime>(\d+:\d+:\d+)")

# host -> what was last pushed there and how far it got
resume: dict[str, dict[str, Any]] = {}
on_resume_changed: Callable[[], None] | None = None
_tracked: set[str] = set()
_pushing: set[str] = set()  # hosts a new item is being pushed to
# Called when a pushed item played to its end (url_queue plays the next one).
on_ended: Callable[[MusicCastMediaPlayer], None] | None = None
END_MARGIN = 20  # seconds; a stop this close to the duration is the end


def _host(entity: MusicCastMediaPlayer) -> str:
    return entity.coordinator.musiccast.device.ip


def _changed() -> None:
    if on_resume_changed:
        on_resume_changed()


def provider_for(hass, media_id: str | None):
    """Return the metadata provider for a media-source ID, if any."""
    if not media_id or not media_source.is_media_source_id(media_id):
        return None
    domain = media_id.removeprefix(media_source.URI_SCHEME).split("/", 1)[0]
    return hass.data.get(PROVIDERS_KEY, {}).get(domain)


def record(entity: MusicCastMediaPlayer) -> dict[str, Any] | None:
    """Return what we last pushed to this speaker, while it is on the server input."""
    if entity.source_id != SERVER_INPUT:
        return None
    return resume.get(_host(entity))


def is_current(entity: MusicCastMediaPlayer) -> dict[str, Any] | None:
    """Return the record if the speaker is still on what we pushed."""
    rec = record(entity)
    if rec and entity.coordinator.data.netusb_track == rec["title"]:
        return rec
    return None


def _hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def _ascii(text: str) -> str:
    """Encode non-ASCII characters as XML character references.

    aiomusiccast sends Content-Length as the number of characters, not
    bytes, so any multi-byte UTF-8 character (ł, ś, ...) makes the request
    look truncated and the speaker answers HTTP 400.
    """
    return text.encode("ascii", "xmlcharrefreplace").decode("ascii")


def _didl(url: str, info: dict[str, Any], image: str | None) -> str:
    flags = DLNA_FLAGS if info.get("seekable") else "*"
    res_attrs = f'protocolInfo="http-get:*:{escape(info["mime"])}:{flags}"'
    if info.get("duration"):
        res_attrs += f' duration="{_hms(info["duration"])}.000"'
    if info.get("size"):
        res_attrs += f' size="{int(info["size"])}"'
    fields = [f"<dc:title>{escape(info['title'])}</dc:title>"]
    if info.get("artist"):
        fields.append(f"<dc:creator>{escape(info['artist'])}</dc:creator>")
        fields.append(f"<upnp:artist>{escape(info['artist'])}</upnp:artist>")
    if info.get("album"):
        fields.append(f"<upnp:album>{escape(info['album'])}</upnp:album>")
    if image:
        fields.append(f"<upnp:albumArtURI>{escape(image)}</upnp:albumArtURI>")
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="0" parentID="-1" restricted="1">'
        + "".join(fields)
        + "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        f"<res {res_attrs}>{escape(url)}</res>"
        "</item></DIDL-Lite>"
    )


async def _soap(entity: MusicCastMediaPlayer, action: str, args: dict[str, Any]) -> str:
    device = entity.coordinator.musiccast.device
    try:
        resp = await device.dlna_avt_request(action, args)
        text = await resp.text()
    except (aiohttp.ClientError, TimeoutError):
        # The speaker closes kept-alive connections; one retry on a new one.
        await asyncio.sleep(0.3)
        try:
            resp = await device.dlna_avt_request(action, args)
            text = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise HomeAssistantError(f"MusicCast {action} failed: {err!r}") from err
    if resp.status >= 400:
        raise HomeAssistantError(f"MusicCast refused {action} (HTTP {resp.status})")
    return text


async def _transport_state(entity: MusicCastMediaPlayer) -> str:
    match = _STATE.search(await _soap(entity, "GetTransportInfo", {"InstanceID": 0}))
    return match.group(1) if match else ""


async def async_play(
    entity: MusicCastMediaPlayer, media_id: str, info: dict[str, Any], position: float = 0
) -> None:
    """Push a media-source item with metadata, optionally from a position."""
    host = _host(entity)
    _pushing.add(host)  # the Stop of the old item is no "end of episode"
    try:
        await _async_play(entity, media_id, info, position)
    finally:
        _pushing.discard(host)


async def _async_play(
    entity: MusicCastMediaPlayer, media_id: str, info: dict[str, Any], position: float
) -> None:
    hass = entity.hass
    play_item = await media_source.async_resolve_media(hass, media_id, entity.entity_id)
    url = async_process_play_media_url(hass, play_item.url)
    image = async_process_play_media_url(hass, info["image"]) if info.get("image") else None
    info = {**info, "mime": info.get("mime") or play_item.mime_type}

    if entity.state == MediaPlayerState.OFF:
        await entity.async_turn_on()
    musiccast = entity.coordinator.musiccast
    await musiccast.select_source(entity._zone_id, SERVER_INPUT, "autoplay_disabled")
    with contextlib.suppress(HomeAssistantError):  # nothing was loaded
        await _soap(entity, "Stop", {"InstanceID": 0})
    await _soap(
        entity,
        "SetAVTransportURI",
        {
            "InstanceID": 0,
            "CurrentURI": _ascii(escape(url)),
            "CurrentURIMetaData": escape(_ascii(_didl(url, info, image))),
        },
    )
    await _soap(entity, "Play", {"InstanceID": 0, "Speed": 1})

    resume[_host(entity)] = {
        "media_id": media_id,
        "title": info["title"],
        "duration": info.get("duration"),
        "seekable": bool(info.get("seekable")),
        "position": position,
        "stopped": False,
        "updated": dt_util.utcnow().isoformat(),
    }
    _changed()
    _track(entity)
    if position >= RESUME_MIN_SECONDS and info.get("seekable"):
        await _seek_when_playing(entity, position)


async def _rel_time(entity: MusicCastMediaPlayer) -> float | None:
    """Return the renderer's position in seconds, None if unknown."""
    match = _REL_TIME.search(await _soap(entity, "GetPositionInfo", {"InstanceID": 0}))
    if not match:
        return None
    h, m, sec = (float(x) for x in match.group(1).split(":"))
    return h * 3600 + m * 60 + sec


async def _seek_when_playing(entity: MusicCastMediaPlayer, position: float) -> None:
    """Seek once the renderer really plays, and check that it took.

    The renderer reports PLAYING while it is still buffering the new stream
    and silently ignores a Seek sent then, so wait for the position to move.
    """
    for _attempt in range(3):
        for _ in range(SEEK_WAIT * 2):
            if await _transport_state(entity) == "PLAYING" and (await _rel_time(entity) or 0) >= 1:
                break
            await asyncio.sleep(0.5)
        else:
            _LOGGER.warning("%s did not start playing; not seeking", entity.entity_id)
            return
        await async_seek(entity, position)
        await asyncio.sleep(3)
        now = await _rel_time(entity)
        if now is not None and abs(now - position) < 30:
            return
        _LOGGER.debug("Seek on %s to %s did not take (at %s)", entity.entity_id, position, now)
    _LOGGER.warning("%s ignored seeking to %s", entity.entity_id, _hms(position))


async def async_seek(entity: MusicCastMediaPlayer, position: float) -> None:
    """Seek within the pushed stream."""
    await _soap(
        entity,
        "Seek",
        {"InstanceID": 0, "Unit": "REL_TIME", "Target": _hms(position)},
    )
    if rec := record(entity):
        rec["position"] = position
        rec["updated"] = dt_util.utcnow().isoformat()
        _changed()


async def async_resume(entity: MusicCastMediaPlayer) -> bool:
    """Restart the last pushed item where it stopped; False if nothing to do."""
    rec = record(entity)
    if not rec:
        return False
    provider = provider_for(entity.hass, rec["media_id"])
    info = await provider(entity.hass, rec["media_id"]) if provider else None
    if not info:
        return False
    _LOGGER.info("Resuming %s on %s at %s", rec["title"], entity.entity_id, _hms(rec["position"]))
    await async_play(entity, rec["media_id"], info, rec["position"])
    return True


def _track(entity: MusicCastMediaPlayer) -> None:
    """Follow the speaker's position so a resume knows where to start.

    The position is the speaker's own play_time. Extrapolating it with
    media_position_updated_at overshoots after a restart, when that
    timestamp is stale.
    """
    if entity.entity_id in _tracked:
        return
    _tracked.add(entity.entity_id)

    @callback
    def _on_change(event: Event[EventStateChangedData]) -> None:
        _check_ended(entity)
        rec = is_current(entity)
        if rec is None:
            return
        playback = entity.coordinator.data.netusb_playback
        if playback == "play":
            pos = entity.coordinator.data.netusb_play_time
            if pos:
                rec["position"] = float(pos)
                rec["stopped"] = False
                rec["updated"] = dt_util.utcnow().isoformat()
                _changed()
        elif playback in ("stop", "pause") and not rec.get("stopped"):
            # Stopped or paused while HA watched: the listener did it (or the
            # episode ended), so don't restart it after the next HA restart.
            rec["stopped"] = True
            _changed()

    async_track_state_change_event(entity.hass, entity.entity_id, _on_change)


def _check_ended(entity: MusicCastMediaPlayer) -> None:
    """Report a pushed item that stopped at (or near) its end, once."""
    rec = record(entity)
    if (
        rec is None
        or on_ended is None
        or _host(entity) in _pushing
        or rec.get("ended")
        or entity.coordinator.data.netusb_playback != "stop"
        or not rec.get("duration")
        or rec["position"] < rec["duration"] - END_MARGIN
    ):
        return
    rec["ended"] = True
    _changed()
    on_ended(entity)


async def _speaker_play_info(entity: MusicCastMediaPlayer) -> dict[str, Any]:
    """Read the speaker's current NetUSB state, bypassing cached data."""
    from aiomusiccast.pyamaha import NetUSB  # noqa: PLC0415

    return await entity.coordinator.musiccast.device.request_json(NetUSB.get_play_info())


async def _resume_after_restart(entity: MusicCastMediaPlayer) -> None:
    """Pick up a pushed stream that broke when Home Assistant restarted.

    Still playing from the buffer: push it again right away at the live
    position. Already stopped: resume from the saved position if it was
    playing until recently and nobody stopped it.
    """
    host = _host(entity)
    rec = resume.get(host)
    if not rec:
        return
    try:
        info = await _speaker_play_info(entity)
    except Exception as err:
        _LOGGER.debug("No play info from %s: %s", entity.entity_id, err)
        return
    if info.get("input") != SERVER_INPUT or info.get("track") != rec["title"]:
        return  # the speaker moved on to something else
    playback = info.get("playback")
    if playback == "play":
        rec["position"] = float(info.get("play_time") or rec["position"])
    elif playback == "stop":
        updated = dt_util.parse_datetime(rec.get("updated", ""))
        recent = updated and (dt_util.utcnow() - updated).total_seconds() < ORPHAN_WINDOW
        if rec.get("stopped") or not recent:
            return
    else:
        return  # paused: leave it to the listener
    _LOGGER.info(
        "Resuming %s on %s at %s after restart",
        rec["title"],
        entity.entity_id,
        _hms(rec["position"]),
    )
    try:
        await async_resume(entity)
    except HomeAssistantError as err:
        _LOGGER.warning("Could not resume %s after restart: %s", entity.entity_id, err)


@callback
def async_resume_after_restart(hass) -> None:
    """At startup, resume streams the restart broke, one per speaker."""
    from homeassistant.helpers.entity_platform import async_get_platforms  # noqa: PLC0415

    seen: set[str] = set()
    for platform in async_get_platforms(hass, "yamaha_musiccast"):
        if platform.domain != "media_player":
            continue
        for entity in platform.entities.values():
            host = _host(entity)
            if host in seen or host not in resume or entity.source_id != SERVER_INPUT:
                continue
            seen.add(host)
            hass.async_create_task(
                _resume_after_restart(entity), f"musiccast resume {entity.entity_id}"
            )
