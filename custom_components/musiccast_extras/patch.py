"""Extend the core yamaha_musiccast media player in place.

Patched on MusicCastMediaPlayer (class level, so existing entities get it):
- async_browse_media / async_play_media / async_search_media: Tidal
  path-based browsing, playing and search (browse.py); pushed media-source
  items with metadata (url_playback.py); search inside media sources.
- async_media_play: resume a pushed stream that stopped.
- async_media_seek, media_duration, supported_features: seeking and the
  duration of pushed items; SEARCH_MEDIA for every browsable player.
And "tidal" is added to aiomusiccast's MusicCastMediaContent.BROWSABLE_INPUTS.

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
    MediaPlayerEntityFeature,
    MediaPlayerState,
    SearchError,
    SearchMedia,
    SearchMediaQuery,
)
from homeassistant.exceptions import HomeAssistantError

from . import browse, url_playback

_LOGGER = logging.getLogger(__name__)

PATH_INPUTS = ("tidal",)

# What we replace or call on the core entity and in aiomusiccast.
ENTITY_METHODS = (
    "async_browse_media",
    "async_play_media",
    "async_search_media",
    "async_media_play",
    "async_media_seek",
)
ENTITY_PROPERTIES = ("media_duration", "supported_features", "source_id", "state")
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


def _root_id(media_id: str | None) -> str | None:
    """Map the core root entry "input:<source>" to our ID for path inputs."""
    if media_id:
        parts = media_id.split(":")
        if len(parts) == 2 and parts[0] == "input" and parts[1] in PATH_INPUTS:
            return browse.make_id(parts[1], ())
    return None


def apply() -> None:
    """Patch the core MusicCast media player."""
    global _original_inputs  # noqa: PLW0603
    from aiomusiccast.musiccast_media_content import MusicCastMediaContent  # noqa: PLC0415
    from homeassistant.components.yamaha_musiccast import (  # noqa: PLC0415
        media_player as mc_media_player,
    )

    cls = mc_media_player.MusicCastMediaPlayer
    if _originals:
        return
    orig = {
        name: cls.__dict__.get(name)
        for name in (*ENTITY_METHODS, "media_duration", "supported_features")
    }
    orig_browse = cls.async_browse_media
    orig_play = cls.async_play_media
    orig_media_play = cls.async_media_play
    orig_duration = cls.media_duration.fget
    orig_features = cls.supported_features.fget

    async def async_browse_media(self, media_content_type=None, media_content_id=None):
        our_id = _root_id(media_content_id) or (
            media_content_id if browse.is_our_id(media_content_id) else None
        )
        if (
            not media_content_id
            and self.state != MediaPlayerState.OFF
            and self.source_id in PATH_INPUTS
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
        if our_id is None:
            return await orig_browse(self, media_content_type, media_content_id)
        if self.state == MediaPlayerState.OFF:
            raise HomeAssistantError("The device has to be turned on to be able to browse media.")
        return await browse.async_browse(self.coordinator.musiccast, self._zone_id, our_id)

    async def async_play_media(self, media_type, media_id, **kwargs):
        if provider := url_playback.provider_for(self.hass, media_id):
            info = await provider(self.hass, media_id)
            if info:
                try:
                    await url_playback.async_play(self, media_id, info)
                    return
                except HomeAssistantError as err:
                    # Still play it, the core way (no title/cover/seeking).
                    _LOGGER.warning("Playing %s without metadata: %s", media_id, err)
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
        source = PATH_INPUTS[0]
        if browse.is_our_id(query.media_content_id):
            source = browse.parse_id(query.media_content_id)[0]
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
        return orig_duration(self)

    def supported_features(self) -> MediaPlayerEntityFeature:
        features = orig_features(self)
        if MediaPlayerEntityFeature.BROWSE_MEDIA in features:
            features |= MediaPlayerEntityFeature.SEARCH_MEDIA
        rec = url_playback.is_current(self)
        if rec and rec["seekable"]:
            features |= MediaPlayerEntityFeature.SEEK
        return features

    cls.async_browse_media = async_browse_media
    cls.async_play_media = async_play_media
    cls.async_search_media = async_search_media
    cls.async_media_play = async_media_play
    cls.async_media_seek = async_media_seek
    cls.media_duration = property(media_duration)
    cls.supported_features = property(supported_features)
    _originals.update(orig)

    _original_inputs = MusicCastMediaContent.BROWSABLE_INPUTS
    missing = tuple(i for i in PATH_INPUTS if i not in _original_inputs)
    if missing:
        MusicCastMediaContent.BROWSABLE_INPUTS = _original_inputs + missing


def remove() -> None:
    """Undo apply()."""
    global _original_inputs  # noqa: PLW0603
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
