"""Extend the core yamaha_musiccast media player in place.

Patched on MusicCastMediaPlayer (class level, so existing entities get it):
- async_browse_media / async_play_media / async_search_media: Tidal
  path-based browsing, playing and search (browse.py); pushed media-source
  items with metadata (url_playback.py); search inside media sources.
- async_media_play: resume a pushed stream that stopped.
- async_media_next_track and enqueue: the queue of pushed media-source
  items kept in Home Assistant (url_queue.py).
- async_media_seek, media_duration, supported_features: seeking and the
  duration of pushed items; SEARCH_MEDIA for every browsable player.
Path-based browsing (browse.py) covers every input the core integration
browses (aiomusiccast's MusicCastMediaContent.BROWSABLE_INPUTS: server,
net_radio, usb, qobuz, deezer, ...) plus Tidal, which is added to that list.

Everything is undone by remove(), so the integration can be unloaded
without a restart. missing_hooks() lists what this relies on and is checked
before patching (and in CI against the newest Home Assistant), so a change
in Home Assistant disables the extras instead of breaking MusicCast.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    MediaPlayerEnqueue,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    SearchError,
    SearchMedia,
    SearchMediaQuery,
)
from homeassistant.exceptions import HomeAssistantError

from . import browse, device as extras_device, library, url_playback, url_queue

_LOGGER = logging.getLogger(__name__)

# Inputs browsed with full-path IDs: the core's browsable inputs plus these.
EXTRA_PATH_INPUTS = ("tidal",)
PATH_INPUTS: tuple[str, ...] = EXTRA_PATH_INPUTS  # set by apply()
SEARCH_FALLBACK = "tidal"  # searched when the player is not on a list input

# What we replace or call on the core entity and in aiomusiccast.
ENTITY_METHODS = (
    "async_browse_media",
    "async_play_media",
    "async_search_media",
    "async_media_play",
    "async_media_seek",
    "async_media_pause",
    "async_media_stop",
    "async_media_next_track",
    "async_media_previous_track",
)
# Properties we replace (the CD ones may be inherited from MediaPlayerEntity).
PATCHED_PROPERTIES = (
    "media_duration",
    "supported_features",
    "state",
    "media_title",
    "media_artist",
    "media_album_name",
    "media_position",
    "media_position_updated_at",
)
ENTITY_PROPERTIES = (*PATCHED_PROPERTIES, "source_id")
CD_TRANSPORT = {
    "async_media_pause": "pause",
    "async_media_stop": "stop",
    "async_media_next_track": "next",
    "async_media_previous_track": "previous",
}
CD_FEATURES = (
    MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.STOP
    | MediaPlayerEntityFeature.NEXT_TRACK
    | MediaPlayerEntityFeature.PREVIOUS_TRACK
)
DEVICE_METHODS = ("select_source", "get_list_info", "return_in_list", "select_list_item")
PYAMAHA_DEVICE_METHODS = ("dlna_avt_request", "request_json")

_originals: dict[str, Any] = {}
_original_inputs: tuple[str, ...] | None = None


def missing_hooks() -> list[str]:
    """Return what this integration needs but the installed versions lack."""
    missing: list[str] = []
    try:
        from aiomusiccast.musiccast_device import MusicCastDevice  # noqa: PLC0415
        from aiomusiccast.musiccast_media_content import MusicCastMediaContent  # noqa: PLC0415
        from aiomusiccast.pyamaha import AsyncDevice, NetUSB  # noqa: PLC0415
        from homeassistant.components.yamaha_musiccast import (  # noqa: PLC0415
            media_player as mc_media_player,
        )
    except ImportError as err:
        return [f"import: {err}"]

    cls = getattr(mc_media_player, "MusicCastMediaPlayer", None)
    if cls is None:
        return ["MusicCastMediaPlayer"]
    missing += [
        f"MusicCastMediaPlayer.{m}" for m in ENTITY_METHODS if not callable(getattr(cls, m, None))
    ]
    missing += [
        f"MusicCastMediaPlayer.{p}"
        for p in ENTITY_PROPERTIES
        if not isinstance(getattr(cls, p, None), property)
    ]
    if not isinstance(getattr(MusicCastMediaContent, "BROWSABLE_INPUTS", None), tuple):
        missing.append("MusicCastMediaContent.BROWSABLE_INPUTS")
    missing += [f"MusicCastDevice.{m}" for m in DEVICE_METHODS if not hasattr(MusicCastDevice, m)]
    missing += [
        f"pyamaha.AsyncDevice.{m}" for m in PYAMAHA_DEVICE_METHODS if not hasattr(AsyncDevice, m)
    ]
    for name in ("get_play_info", "set_search_string"):
        if not hasattr(NetUSB, name):
            missing.append(f"pyamaha.NetUSB.{name}")
    return missing


def _extras(entity: Any) -> extras_device.MusicCastExtrasDevice | None:
    """Return the extras data of the entity's device, if set up."""
    return extras_device.REGISTRY.get(entity.coordinator.musiccast.device.ip)


def _cd(entity: Any) -> tuple[extras_device.MusicCastExtrasDevice, dict[str, Any]] | None:
    """Return (device, cd play info) while the zone plays the CD input."""
    ext = _extras(entity)
    if ext is None or entity.source_id != "cd":
        return None
    info = (ext.data or {}).get("cd")
    return (ext, info) if info is not None else None


def _root_id(media_id: str | None) -> str | None:
    """Map the core root entry "input:<source>" to our ID for path inputs."""
    if media_id:
        parts = media_id.split(":")
        if len(parts) == 2 and parts[0] == "input" and parts[1] in PATH_INPUTS:
            return browse.make_id(parts[1], ())
    return None


def apply() -> None:
    """Patch the core MusicCast media player."""
    global _original_inputs, PATH_INPUTS  # noqa: PLW0603
    from aiomusiccast.musiccast_media_content import MusicCastMediaContent  # noqa: PLC0415
    from homeassistant.components.yamaha_musiccast import (  # noqa: PLC0415
        media_player as mc_media_player,
    )

    cls = mc_media_player.MusicCastMediaPlayer
    if _originals:
        return
    orig = {name: cls.__dict__.get(name) for name in (*ENTITY_METHODS, *PATCHED_PROPERTIES)}
    orig_browse = cls.async_browse_media
    orig_play = cls.async_play_media
    orig_media_play = cls.async_media_play
    orig_duration = cls.media_duration.fget
    orig_features = cls.supported_features.fget
    orig_state = cls.state.fget

    async def async_browse_media(self, media_content_type=None, media_content_id=None):
        if url_queue.is_queue_id(media_content_id):
            folder_id = url_queue.set_mode(self, media_content_id)
            if folder_id is None:
                return url_queue.browse_queue(self, media_content_id)
            return await async_browse_media(self, media_content_type, folder_id)
        our_id = _root_id(media_content_id) or (
            media_content_id if browse.is_our_id(media_content_id) else None
        )
        if (
            not media_content_id
            and self.state != MediaPlayerState.OFF
            and self.source_id in PATH_INPUTS
            and url_playback.is_current(self) is None  # a pushed item: show the queue
        ):
            # Start where we left off while the player is still on that source.
            try:
                last = await browse.async_browse_last(
                    self.coordinator.musiccast, self._zone_id, self.source_id
                )
            except HomeAssistantError as err:
                _LOGGER.warning("Could not reopen last list, showing library: %s", err)
                last = None
            if last is not None:
                return last
        if media_content_id == browse.LIBRARY_ID:
            media_content_id = None
            our_id = None
        if library.is_library_id(media_content_id) and (ext := _extras(self)):
            return await library.async_browse(ext, media_content_id)
        if our_id is None:
            result = await orig_browse(self, media_content_type, media_content_id)
            if not media_content_id and result.children is not None:
                # The queue of pushed items and the device's own lists, next to the inputs.
                own = library.root_folders(ext) if (ext := _extras(self)) else []
                if url_queue.show_at_root(self):
                    own.insert(0, url_queue.root_entry(self))
                result.children = [*own, *result.children]
            if media_content_id and media_source.is_media_source_id(media_content_id):
                result = url_queue.decorate(self, result)
            return result
        if self.state == MediaPlayerState.OFF:
            raise HomeAssistantError("The device has to be turned on to be able to browse media.")
        return await browse.async_browse(self.coordinator.musiccast, self._zone_id, our_id)

    async def async_play_media(self, media_type, media_id, **kwargs):
        enqueue = kwargs.get("enqueue")
        if url_queue.is_queue_id(media_id):
            if self.state == MediaPlayerState.OFF:
                await self.async_turn_on()
            await url_queue.async_play(self, media_id)
            return None
        provider = url_playback.provider_for(self.hass, media_id)
        if provider and enqueue in (MediaPlayerEnqueue.NEXT, MediaPlayerEnqueue.ADD):
            # Media-source items go to the queue kept in Home Assistant.
            kind = "play_next" if enqueue == MediaPlayerEnqueue.NEXT else "add_to_queue"
            entry = await url_queue.async_entry_for(self.hass, media_id)
            await url_queue.async_enqueue(self, [entry], kind)
            return None
        if enqueue in (MediaPlayerEnqueue.NEXT, MediaPlayerEnqueue.ADD):
            # "Play next" / "Add to queue": only items of the device's lists.
            if not browse.is_our_id(media_id):
                raise HomeAssistantError(
                    "Only items browsed from the device's own sources can be queued"
                )
            if self.state == MediaPlayerState.OFF:
                await self.async_turn_on()
            kind = "play_next" if enqueue == MediaPlayerEnqueue.NEXT else "add_to_queue"
            await browse.async_enqueue(self.coordinator.musiccast, self._zone_id, media_id, kind)
            return None
        if provider:
            info = await provider(self.hass, media_id)
            mode = url_queue.click_mode(self)
            if info and info.get("duration") and enqueue is None and mode != "play":
                # A click in the media browser while the click mode queues.
                entry = {"media_id": media_id, "title": info["title"], "thumbnail": None}
                await url_queue.async_enqueue(self, [entry], mode)
                return None
            if info:
                try:
                    await url_playback.async_play(self, media_id, info)
                    return
                except HomeAssistantError as err:
                    # Still play it, the core way (no title/cover/seeking).
                    _LOGGER.warning("Playing %s without metadata: %s", media_id, err)
        if library.is_library_id(media_id) and (ext := _extras(self)):
            if self.state == MediaPlayerState.OFF:
                await self.async_turn_on()
            await library.async_play(ext, self._zone_id, media_id)
            return None
        if not browse.is_our_id(media_id):
            return await orig_play(self, media_type, media_id, **kwargs)
        if self.state == MediaPlayerState.OFF:
            await self.async_turn_on()
        await browse.async_play(self.coordinator.musiccast, self._zone_id, media_id)
        return None

    async def async_search_media(self, query: SearchMediaQuery) -> SearchMedia:
        if query.media_content_id and media_source.is_media_source_id(query.media_content_id):
            # Searching inside a media source (if the source supports it).
            return await media_source.async_search_media(self.hass, query.media_content_id, query)
        # Search the source being browsed, else the one the player is on.
        if browse.is_our_id(query.media_content_id):
            source = browse.parse_id(query.media_content_id)[0]
        elif self.source_id in PATH_INPUTS:
            source = self.source_id
        else:
            source = SEARCH_FALLBACK
        if source not in PATH_INPUTS:
            raise SearchError(f"Search is not supported for {source}")
        if self.state == MediaPlayerState.OFF:
            raise SearchError("The device has to be turned on to search.")
        return await browse.async_search(
            self.coordinator.musiccast,
            self._zone_id,
            source,
            query.search_query,
            query.media_filter_classes,
        )

    async def async_media_play(self) -> None:
        if cd := _cd(self):
            await cd[0].command("cd/setPlayback?playback=play")
            return
        # A pushed stream that stopped (e.g. HA restarted) can't be restarted
        # with the NetUSB play command; push it again at the saved position.
        if (
            url_playback.record(self)
            and self.coordinator.data.netusb_playback == "stop"
            and await url_playback.async_resume(self)
        ):
            return
        await orig_media_play(self)

    async def async_media_seek(self, position: float) -> None:
        rec = url_playback.is_current(self)
        if not rec or not rec["seekable"]:
            raise HomeAssistantError("Seeking is only supported for pushed media with metadata")
        await url_playback.async_seek(self, position)

    def media_duration(self):
        rec = url_playback.is_current(self)
        if rec and rec.get("duration"):
            return int(rec["duration"])
        if (cd := _cd(self)) and cd[1].get("total_time"):
            return cd[1]["total_time"]
        return orig_duration(self)

    def state(self):
        current = orig_state(self)
        if (cd := _cd(self)) and current != MediaPlayerState.OFF:
            return {"play": MediaPlayerState.PLAYING, "pause": MediaPlayerState.PAUSED}.get(
                cd[1].get("playback"), MediaPlayerState.IDLE
            )
        return current

    def _cd_transport(name: str, playback: str):
        original = getattr(cls, name)

        async def method(self) -> None:
            if cd := _cd(self):
                await cd[0].command(f"cd/setPlayback?playback={playback}")
                return
            await original(self)

        return method

    def _cd_property(name: str, read):
        original = getattr(cls, name).fget

        def getter(self):
            if cd := _cd(self):
                return read(cd[0], cd[1])
            return original(self)

        return property(getter)

    def supported_features(self) -> MediaPlayerEntityFeature:
        features = orig_features(self)
        if MediaPlayerEntityFeature.BROWSE_MEDIA in features:
            features |= (
                MediaPlayerEntityFeature.SEARCH_MEDIA | MediaPlayerEntityFeature.MEDIA_ENQUEUE
            )
        rec = url_playback.is_current(self)
        if rec and rec["seekable"]:
            features |= MediaPlayerEntityFeature.SEEK
        if url_queue.has_next(self):
            features |= MediaPlayerEntityFeature.NEXT_TRACK
        if _cd(self):
            features |= CD_FEATURES
        return features

    cls.async_browse_media = async_browse_media
    cls.async_play_media = async_play_media
    cls.async_search_media = async_search_media
    cls.async_media_play = async_media_play
    cls.async_media_seek = async_media_seek
    cls.media_duration = property(media_duration)
    cls.supported_features = property(supported_features)
    cls.state = property(state)
    for name, playback in CD_TRANSPORT.items():
        setattr(cls, name, _cd_transport(name, playback))
    cd_next = cls.async_media_next_track

    async def async_media_next_track(self) -> None:
        # Next on a pushed item goes on with the queue kept in Home Assistant.
        if url_queue.has_next(self) and await url_queue.async_play_next(self):
            return
        await cd_next(self)

    cls.async_media_next_track = async_media_next_track
    cd_info = {
        "media_title": lambda ext, cd: cd.get("track") or None,
        "media_artist": lambda ext, cd: cd.get("artist") or None,
        "media_album_name": lambda ext, cd: cd.get("album") or None,
        "media_position": lambda ext, cd: cd.get("play_time"),
        "media_position_updated_at": lambda ext, cd: (ext.data or {}).get("cd_updated"),
    }
    for name, read in cd_info.items():
        setattr(cls, name, _cd_property(name, read))
    _originals.update(orig)

    _original_inputs = MusicCastMediaContent.BROWSABLE_INPUTS
    PATH_INPUTS = tuple(dict.fromkeys((*_original_inputs, *EXTRA_PATH_INPUTS)))
    missing = tuple(i for i in EXTRA_PATH_INPUTS if i not in _original_inputs)
    if missing:
        MusicCastMediaContent.BROWSABLE_INPUTS = _original_inputs + missing


def remove() -> None:
    """Undo apply()."""
    global _original_inputs, PATH_INPUTS  # noqa: PLW0603
    if not _originals:
        return
    from aiomusiccast.musiccast_media_content import MusicCastMediaContent  # noqa: PLC0415
    from homeassistant.components.yamaha_musiccast import (  # noqa: PLC0415
        media_player as mc_media_player,
    )

    cls = mc_media_player.MusicCastMediaPlayer
    for name, value in _originals.items():
        if value is None:
            # Was inherited (e.g. async_media_seek from MediaPlayerEntity).
            if name in cls.__dict__:
                delattr(cls, name)
        else:
            setattr(cls, name, value)
    _originals.clear()
    if _original_inputs is not None:
        MusicCastMediaContent.BROWSABLE_INPUTS = _original_inputs
        _original_inputs = None
    PATH_INPUTS = EXTRA_PATH_INPUTS
