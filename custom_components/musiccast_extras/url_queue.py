"""A play queue for pushed media-source items (podcast episodes and the like).

The speaker's own play queue only takes items of its own lists, while
media-source items are pushed to its DLNA renderer one URL at a time. So
this keeps a queue per speaker in Home Assistant and pushes the next item
(with metadata, see url_playback) when the current one has played to its
end, or on the Next button.

Items are queueable when their media source registers a metadata provider
(url_playback.provider_for), since the end of an item is told by its
duration. In the media browser, folders of such a source get entries on top,
as for the device's own lists (browse.py): the click mode (play now / play
next / add to queue), "this folder next / to the queue" and the queue itself.

IDs (media_content_id):
    mcxq:mode:<mode>:<quoted folder id>   switch click mode, reopen the folder
    mcxq:folder:<kind>:<quoted folder id> queue a folder (kind play_next|add_to_queue)
    mcxq:list                             the queue
    mcxq:item:<index>                     play the queue from that item
    mcxq:clear                            empty the queue
"""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    BrowseMedia,
    MediaClass,
    MediaPlayerState,
)
from homeassistant.exceptions import HomeAssistantError

from . import browse, url_playback

if TYPE_CHECKING:
    from homeassistant.components.yamaha_musiccast.media_player import MusicCastMediaPlayer

_LOGGER = logging.getLogger(__name__)

PREFIX = "mcxq"
LIST_ID = f"{PREFIX}:list"
CLEAR_ID = f"{PREFIX}:clear"
MAX_ITEMS = 500

# host -> [{"media_id", "title", "thumbnail"}]
queues: dict[str, list[dict[str, Any]]] = {}
on_queue_changed: Callable[[], None] | None = None


def is_queue_id(media_id: str | None) -> bool:
    """Return True for the IDs of this module."""
    return bool(media_id) and media_id.startswith(f"{PREFIX}:")


def _host(entity: MusicCastMediaPlayer) -> str:
    return entity.coordinator.musiccast.device.ip


def _changed() -> None:
    if on_queue_changed:
        on_queue_changed()


def _queueable(hass, item: BrowseMedia) -> bool:
    return (
        item.can_play
        and item.media_class != MediaClass.CHANNEL  # live streams never end
        and url_playback.provider_for(hass, item.media_content_id) is not None
    )


def _entry(item: BrowseMedia) -> dict[str, Any]:
    return {
        "media_id": item.media_content_id,
        "title": item.title,
        "thumbnail": item.thumbnail,
    }


# ------------------------------------------------------------------ browsing


def decorate(entity: MusicCastMediaPlayer, folder: BrowseMedia) -> BrowseMedia:
    """Put the queue entries on top of a media-source folder with episodes."""
    children = folder.children or []
    if not media_source.is_media_source_id(folder.media_content_id) or not any(
        _queueable(entity.hass, c) for c in children
    ):
        return folder
    host = _host(entity)
    folder_id = quote(folder.media_content_id, safe="")
    mode = browse.click_mode.get(host, "play")
    following = browse.CLICK_MODES[(browse.CLICK_MODES.index(mode) + 1) % len(browse.CLICK_MODES)]
    entries = [
        browse.nav_item(
            f"Click: {browse.CLICK_MODE_LABELS[mode]} (tap to change)",
            f"{PREFIX}:mode:{following}:{folder_id}",
        ),
        *(
            BrowseMedia(
                title=title,
                media_class=MediaClass.PLAYLIST,
                media_content_id=f"{PREFIX}:folder:{kind}:{folder_id}",
                media_content_type="music",
                can_play=True,
                can_expand=False,
            )
            for kind, title in (
                ("play_next", "⏭ Play this folder next"),
                ("add_to_queue", "➕ Add this folder to the queue"),
            )
        ),
    ]
    if queue := queues.get(host):
        entries.append(browse.nav_item(f"📋 Queue ({len(queue)})", LIST_ID))
    folder.children = [*entries, *children]
    return folder


def set_mode(entity: MusicCastMediaPlayer, media_id: str) -> str | None:
    """Apply a click-mode switch ID; return the folder to reopen, else None."""
    if not media_id.startswith(f"{PREFIX}:mode:"):
        return None
    mode, _, folder_id = media_id[len(f"{PREFIX}:mode:") :].partition(":")
    if mode in browse.CLICK_MODES:
        browse.click_mode[_host(entity)] = mode
    return unquote(folder_id)


def browse_queue(entity: MusicCastMediaPlayer, media_id: str) -> BrowseMedia:
    """Return the queue folder."""
    host = _host(entity)
    if media_id != LIST_ID:
        raise HomeAssistantError(f"Unknown queue folder {media_id}")
    queue = queues.get(host, [])
    folder = BrowseMedia(
        title=f"Queue ({len(queue)})",
        media_class=MediaClass.PLAYLIST,
        media_content_id=LIST_ID,
        media_content_type="music",
        can_play=False,
        can_expand=True,
        children_media_class=MediaClass.TRACK,
    )
    folder.children = [
        BrowseMedia(
            title=f"{n}. {entry['title']}",
            media_class=MediaClass.TRACK,
            media_content_id=f"{PREFIX}:item:{n - 1}",
            media_content_type="music",
            can_play=True,
            can_expand=False,
            thumbnail=entry.get("thumbnail"),
        )
        for n, entry in enumerate(queue, 1)
    ]
    if queue:
        folder.children.append(
            BrowseMedia(
                title="🗑 Clear the queue",
                media_class=MediaClass.DIRECTORY,
                media_content_id=CLEAR_ID,
                media_content_type="music",
                can_play=True,
                can_expand=False,
            )
        )
    return folder


# ------------------------------------------------------------------- playing


def click_mode(entity: MusicCastMediaPlayer) -> str:
    """Return what clicking an item does on this player."""
    return browse.click_mode.get(_host(entity), "play")


def _idle(entity: MusicCastMediaPlayer) -> bool:
    """Return True if nothing of ours is playing or paused on the speaker."""
    return url_playback.is_current(entity) is None or entity.state not in (
        MediaPlayerState.PLAYING,
        MediaPlayerState.PAUSED,
        MediaPlayerState.BUFFERING,
    )


async def async_enqueue(
    entity: MusicCastMediaPlayer, entries: list[dict[str, Any]], kind: str
) -> None:
    """Add entries (play_next: right after the current item); start if idle."""
    if not entries:
        raise HomeAssistantError("Nothing here can be queued")
    host = _host(entity)
    queue = queues.setdefault(host, [])
    if kind == "play_next":
        queue[:0] = entries
    else:
        queue.extend(entries)
    del queue[MAX_ITEMS:]
    _changed()
    _LOGGER.info(
        "%s: %s %d item(s), queue has %d", entity.entity_id, kind, len(entries), len(queue)
    )
    if _idle(entity):
        await async_play_next(entity)


async def async_entry_for(hass, media_id: str) -> dict[str, Any]:
    """Return a queue entry for a single media-source item."""
    provider = url_playback.provider_for(hass, media_id)
    info = await provider(hass, media_id) if provider else None
    if not info:
        raise HomeAssistantError(f"{media_id} can't be queued")
    return {"media_id": media_id, "title": info["title"], "thumbnail": None}


async def async_play(entity: MusicCastMediaPlayer, media_id: str) -> None:
    """Handle a click on one of the queue entries."""
    host = _host(entity)
    queue = queues.setdefault(host, [])
    if media_id == CLEAR_ID:
        queue.clear()
        _changed()
        return
    if media_id.startswith(f"{PREFIX}:item:"):
        index = int(media_id.rsplit(":", 1)[1])
        if not 0 <= index < len(queue):
            raise HomeAssistantError("The queue has changed, open it again")
        del queue[:index]
        _changed()
        await async_play_next(entity)
        return
    if media_id.startswith(f"{PREFIX}:folder:"):
        kind, _, folder_id = media_id[len(f"{PREFIX}:folder:") :].partition(":")
        folder = await media_source.async_browse_media(entity.hass, unquote(folder_id))
        entries = [_entry(c) for c in folder.children or [] if _queueable(entity.hass, c)]
        await async_enqueue(entity, entries, kind)
        return
    raise HomeAssistantError(f"Unknown queue action {media_id}")


async def async_play_next(entity: MusicCastMediaPlayer) -> bool:
    """Push the first queued item that still plays; False if the queue is empty."""
    queue = queues.get(_host(entity), [])
    while queue:
        entry = queue.pop(0)
        _changed()
        provider = url_playback.provider_for(entity.hass, entry["media_id"])
        info = await provider(entity.hass, entry["media_id"]) if provider else None
        if not info:
            _LOGGER.warning("Skipping %s, it can't be played any more", entry["title"])
            continue
        await url_playback.async_play(entity, entry["media_id"], info)
        return True
    return False


def has_next(entity: MusicCastMediaPlayer) -> bool:
    """Return True if a pushed item plays and the queue has more."""
    return bool(queues.get(_host(entity))) and url_playback.record(entity) is not None


def on_ended(entity: MusicCastMediaPlayer) -> None:
    """A pushed item played to its end: go on with the queue."""
    if not queues.get(_host(entity)):
        return

    async def _next() -> None:
        try:
            await async_play_next(entity)
        except HomeAssistantError as err:
            _LOGGER.warning("Could not play the next queued item on %s: %s", entity.entity_id, err)

    entity.hass.async_create_task(_next(), f"musiccast queue {entity.entity_id}")
