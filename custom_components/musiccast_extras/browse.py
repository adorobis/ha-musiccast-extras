"""Path-based browsing and search of MusicCast netusb lists (Server, Tidal, ...).

The player keeps one menu cursor per device. The core integration's browse
IDs ("list:tidal:<layer>:<index>") only say "item <index> of whatever list
is at <layer>", and the integration moves the cursor by layer number alone.
The HA media browser also re-fetches parent levels for its header and back
button, which moves the cursor. Together this can leave the player on a
different list than the one on screen (for example the alphabetical Albums
list one level above "Sort by date added"), so the next open or play hits
the item at that index in the wrong list.

Here every ID holds the full path of steps from the source's root menu:

    mctidal:<source>:<step>.<step>...:<page start>[:<search text>]

A step is a list index to select, or "s<index>" for "run the search text
on the search entry at <index>" (netusb/setSearchString). The search text
is URL-quoted in the last field, so a result can always be reached again
even if someone searched for something else in the meantime.

Before any browse or play we walk the cursor to the exact list the item
belongs to: back up to the common prefix of where we left the player and
where we need to be, then select (or search) down, waiting after every
command until the player reports the expected layer. If the player's menu no
longer matches what we left (someone used the app or a button), we start
again from the root. A per-device lock keeps concurrent requests from the
frontend from interleaving.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from aiomusiccast.pyamaha import NetUSB
from homeassistant.components.media_player import (
    BrowseError,
    BrowseMedia,
    MediaClass,
    SearchError,
    SearchMedia,
)
from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)

PREFIX = "mctidal"
PAGE_SIZE = 24  # the player returns at most 8 entries per request
SEARCH_RESULTS = 40
STEP_TIMEOUT = 4.0  # seconds to wait for the player to reach a menu layer
POLL_INTERVAL = 0.15
MAX_RESETS = 15

# getListInfo "attribute" bits
ATTR_SELECTABLE = 0b10
ATTR_PLAYABLE = 0b100
ATTR_SEARCHABLE = 0b1000

# Group headers in search results (lang=en) and the class of their items
SEARCH_GROUPS = {
    "playlists": MediaClass.PLAYLIST,
    "artists": MediaClass.ARTIST,
    "albums": MediaClass.ALBUM,
    "tracks": MediaClass.TRACK,
}
SEARCH_CLASSES = list(SEARCH_GROUPS.values())

Step = int | str  # int = select index, "s<index>" = search on index


@dataclass
class _Cursor:
    """Where we last left the player's menu cursor."""

    path: tuple[Step, ...]
    query: str | None
    layer: int
    name: str


_cursors: dict[tuple[str, str], _Cursor] = {}
_locks: dict[str, asyncio.Lock] = {}
# Menu name of every list we have been in: (host, source, path) -> name
_names: dict[tuple[str, str, tuple], str] = {}

# Last list something was played from, per player host:
# {"id": browse id, "title": str, "thumbnail": str | None}
LAST_ALIAS = "@last"
last_played: dict[str, dict] = {}
on_last_played_changed = None  # set on setup to persist last_played


def player_host(musiccast: Any) -> str:
    return getattr(getattr(musiccast, "device", None), "ip", None) or str(id(musiccast))


def last_alias_id(source: str) -> str:
    """ID that always opens the last played list of the browsing player."""
    return f"{PREFIX}:{source}:{LAST_ALIAS}:0"


LIBRARY_ID = f"{PREFIX}:library"  # the player's normal top level

# The HA media browser only "plays" an item, it has no Play next / Add to
# queue menu. So each list with tracks gets entries for that: the folder as
# a whole, and a click mode (per player) that clicking a track follows.
CLICK_MODES = ("play", "play_next", "add_to_queue")
CLICK_MODE_LABELS = {
    "play": "▶ Play now",
    "play_next": "⏭ Play next",
    "add_to_queue": "➕ Add to queue",
}
MODE_PREFIX = f"{PREFIX}:@mode:"  # + <mode>:<quoted list id>, opens that list
FOLDER_QUEUE_PREFIX = f"{PREFIX}:@queue:"  # + <kind>:<quoted folder id>, playable
click_mode: dict[str, str] = {}  # player host -> mode


def nav_item(title: str, media_id: str, thumbnail: str | None = None) -> BrowseMedia:
    return BrowseMedia(
        title=title,
        media_class=MediaClass.DIRECTORY,
        media_content_id=media_id,
        media_content_type="music",
        can_play=False,
        can_expand=True,
        thumbnail=thumbnail,
    )


async def async_browse_last(musiccast: Any, zone_id: str, source: str) -> BrowseMedia | None:
    """The last played list of this player, with ways out of it on top.

    Used as the media browser's first screen: the browser opened from a
    player card always starts at the top level and has no back button there,
    so besides the "Up" / "Previous page" entries every list has, this one
    also gets a direct "Library (all sources)" entry.
    """
    last = last_played.get(player_host(musiccast))
    if not last or not last["id"].startswith(f"{PREFIX}:{source}:"):
        return None
    result = await async_browse(musiccast, zone_id, last["id"])
    _source, path, _start, _query = parse_id(last["id"])
    if path:
        result.children = [
            nav_item("Library (all sources)", LIBRARY_ID),
            *(result.children or []),
        ]
    return result


def resume_item(musiccast: Any) -> BrowseMedia | None:
    """'Continue' entry pointing at this player's last played list."""
    last = last_played.get(player_host(musiccast))
    if not last:
        return None
    return BrowseMedia(
        title=f"Continue: {last['title']}",
        media_class=MediaClass.DIRECTORY,
        media_content_id=last["id"],
        media_content_type="music",
        can_play=False,
        can_expand=True,
        thumbnail=last.get("thumbnail"),
    )


def _has_search(path: tuple[Step, ...]) -> bool:
    return any(isinstance(s, str) for s in path)


def make_id(source: str, path: tuple[Step, ...], start: int = 0, query: str | None = None) -> str:
    """Build a browse ID."""
    media_id = f"{PREFIX}:{source}:{'.'.join(map(str, path))}:{start}"
    if query is not None and _has_search(path):
        media_id += ":" + quote(query, safe="")
    return media_id


def parse_id(media_id: str) -> tuple[str, tuple[Step, ...], int, str | None]:
    """Split a browse ID into source, path, page start and search text."""
    try:
        parts = media_id.split(":")
        if parts[0] != PREFIX or len(parts) not in (4, 5):
            raise ValueError
        _prefix, source, raw_path, start = parts[:4]
        query = unquote(parts[4]) if len(parts) == 5 else None
        steps: list[Step] = []
        for raw in raw_path.split(".") if raw_path else ():
            if raw.startswith("s"):
                int(raw[1:])
                steps.append(raw)
            else:
                steps.append(int(raw))
        if _has_search(tuple(steps)) and query is None:
            raise ValueError
        return source, tuple(steps), int(start), query
    except ValueError as err:
        raise BrowseError(f"Invalid MusicCast browse id: {media_id}") from err


def is_our_id(media_id: str | None) -> bool:
    """Return True for IDs handled here."""
    return bool(media_id) and media_id.startswith(PREFIX + ":")


class Navigator:
    """Moves one player's menu cursor along step paths."""

    def __init__(self, musiccast: Any, zone_id: str, source: str) -> None:
        self.mc = musiccast
        self.zone = zone_id
        self.source = source
        host = player_host(musiccast)
        self.key = (host, source)
        self.lock = _locks.setdefault(host, asyncio.Lock())

    async def info(self, start: int = 0) -> dict:
        data = await self.mc.get_list_info(self.source, start)
        if "menu_layer" not in data:
            raise HomeAssistantError(f"MusicCast returned no list for {self.source}: {data}")
        return data

    async def _wait_layer(self, layer: int) -> dict:
        """Wait until the player is at ``layer`` and reports it twice in a row.

        The player sometimes reports a layer number that doesn't match the
        list it returns yet, so one matching read is not trusted.
        """
        deadline = time.monotonic() + STEP_TIMEOUT
        previous = None
        while True:
            data = await self.info()
            signature = (
                int(data["menu_layer"]),
                data.get("menu_name"),
                data.get("max_line"),
            )
            if signature[0] == layer and signature == previous:
                return data
            previous = signature if signature[0] == layer else None
            if time.monotonic() > deadline:
                raise HomeAssistantError(
                    f"MusicCast menu did not reach layer {layer} "
                    f"(still at {data['menu_layer']} '{data.get('menu_name')}')"
                )
            await asyncio.sleep(POLL_INTERVAL)

    async def _to_root(self) -> dict:
        data = await self.info()
        for _ in range(MAX_RESETS):
            layer = int(data["menu_layer"])
            if layer == 0:
                return data
            await self.mc.return_in_list(self.zone)
            data = await self._wait_layer(layer - 1)
        raise HomeAssistantError("Could not return MusicCast menu to the root")

    async def _step(self, step: Step, query: str | None) -> None:
        if isinstance(step, int):
            await self.mc.select_list_item(step, self.zone)
            return
        result = await self.mc.device.request_json(
            NetUSB.set_search_string(query or "", "main", int(step[1:]))
        )
        if result.get("response_code") not in (0, None):
            raise HomeAssistantError(f"MusicCast search failed: {result}")

    def _remember_name(self, path: list[Step], data: dict) -> None:
        if name := data.get("menu_name"):
            _names[(*self.key, tuple(path))] = name

    def name_of(self, path: tuple[Step, ...]) -> str | None:
        return _names.get((*self.key, tuple(path)))

    def is_sorted_list(self, path: tuple[Step, ...]) -> bool:
        return is_sort_name(self.name_of(path))

    def up_path(self, path: tuple[Step, ...]) -> tuple[Step, ...]:
        """Parent list, one level up.

        The player puts each sort order one level below the list it sorts
        (My music > Albums > Sort by date added). From a sorted list this
        goes to the list the sort belongs to (Albums), skipping any sorted
        lists nested above it, which would look like the same screen.
        """
        p = list(path)
        from_sorted = self.is_sorted_list(tuple(p))
        if p:
            p.pop()
        while from_sorted and p and self.is_sorted_list(tuple(p)):
            p.pop()
        return tuple(p)

    async def goto(self, target: tuple[Step, ...], query: str | None = None) -> dict:
        """Move the cursor into the list at ``target``. Caller holds the lock.

        If a step fails (another client moved the menu mid-walk, or the
        player got into an odd menu state), start over from the root once.
        """
        try:
            return await self._goto(target, query)
        except HomeAssistantError as err:
            _LOGGER.debug("Menu walk failed (%s), retrying from the root", err)
            _cursors.pop(self.key, None)
            return await self._goto(target, query)

    async def _goto(self, target: tuple[Step, ...], query: str | None) -> dict:
        try:
            data = await self.info()
            known = _cursors.get(self.key)
            if (
                known
                and int(data["menu_layer"]) == known.layer
                and data.get("menu_name") == known.name
                and known.layer == len(known.path)
            ):
                cur = list(known.path)
                cur_query = known.query
            else:
                data = await self._to_root()
                cur = []
                cur_query = None

            def same(a: Step, b: Step) -> bool:
                # a search step only matches when the search text matches too
                return a == b and (isinstance(a, int) or cur_query == query)

            common = 0
            while common < len(cur) and common < len(target) and same(cur[common], target[common]):
                common += 1

            self._remember_name(cur, data)
            while len(cur) > common:
                await self.mc.return_in_list(self.zone)
                cur.pop()
                data = await self._wait_layer(len(cur))
                self._remember_name(cur, data)

            for step in target[len(cur) :]:
                await self._step(step, query)
                cur.append(step)
                data = await self._wait_layer(len(cur))
                self._remember_name(cur, data)

            _cursors[self.key] = _Cursor(
                tuple(cur),
                query if _has_search(tuple(cur)) else None,
                int(data["menu_layer"]),
                data.get("menu_name", ""),
            )
            return data
        except Exception:
            _cursors.pop(self.key, None)
            raise

    async def read(self, data: dict, start: int, count: int) -> list[dict]:
        """Read up to ``count`` entries of the current list from ``start``."""
        max_line = int(data.get("max_line", 0))
        entries: list[dict] = []
        pos = start
        while pos < min(start + count, max_line):
            page = data if pos == 0 else await self.info(pos)
            got = page.get("list_info", [])
            if not got:
                break
            entries.extend(got)
            pos += len(got)
        return entries[:count]


def is_sort_name(name: str | None) -> bool:
    return (name or "").strip().lower().startswith("sort by")


HTTPS_THUMBNAIL_HOSTS = ("tidal.com",)


def _https_thumbnail(url: str | None) -> str | None:
    """Return the https version of a Tidal cover URL, else the URL unchanged.

    The player reports Tidal covers as http://resources.tidal.com:80/...,
    which a frontend served over HTTPS may block as mixed content.
    """
    if not url or not url.startswith("http://"):
        return url
    parts = urlsplit(url)
    host = parts.hostname or ""
    if not any(host == h or host.endswith("." + h) for h in HTTPS_THUMBNAIL_HOSTS):
        return url
    port = parts.port
    netloc = host if port in (None, 80) else f"{host}:{port}"
    return urlunsplit(("https", netloc, parts.path, parts.query, parts.fragment))


def _attrs(entry: dict) -> tuple[bool, bool, bool]:
    attr = int(entry.get("attribute") or 0)
    return (
        bool(attr & ATTR_SELECTABLE),
        bool(attr & ATTR_PLAYABLE),
        bool(attr & ATTR_SEARCHABLE),
    )


def _item(
    source: str,
    path: tuple[Step, ...],
    entry: dict,
    query: str | None,
    media_class: MediaClass | None = None,
) -> BrowseMedia:
    can_expand, can_play, can_search = _attrs(entry)
    if media_class is None:
        if can_expand and can_play:
            media_class = MediaClass.ALBUM
        elif can_expand:
            media_class = MediaClass.DIRECTORY
        else:
            media_class = MediaClass.TRACK
    return BrowseMedia(
        title=entry.get("text") or "",
        media_class=media_class,
        media_content_id=make_id(source, path, 0, query),
        media_content_type="music",
        can_play=can_play,
        can_expand=can_expand,
        thumbnail=_https_thumbnail(entry.get("thumbnail")) or None,
        can_search=can_search,
        search_media_classes=SEARCH_CLASSES if can_search else None,
    )


def _search_entry_index(entries: list[dict]) -> int | None:
    for i, entry in enumerate(entries):
        if _attrs(entry)[2]:
            return i
    return None


def _queue_entries(
    musiccast: Any,
    source: str,
    path: tuple[Step, ...],
    start: int,
    query: str | None,
    entries: list[dict],
) -> list[BrowseMedia]:
    """Return the click-mode switch and the folder queue entries for a list."""
    flags = [_attrs(e) for e in entries]
    has_tracks = any(can_play and not can_expand for can_expand, can_play, _ in flags)
    if not path or not any(can_play for _, can_play, _ in flags):
        return []
    list_id = make_id(source, path, start, query)
    mode = click_mode.get(player_host(musiccast), "play")
    following = CLICK_MODES[(CLICK_MODES.index(mode) + 1) % len(CLICK_MODES)]
    result = [
        nav_item(
            f"Click: {CLICK_MODE_LABELS[mode]} (tap to change)",
            f"{MODE_PREFIX}{following}:{quote(list_id, safe='')}",
        )
    ]
    if has_tracks and not isinstance(path[-1], str):
        folder_id = quote(make_id(source, path, 0, query), safe="")
        result += [
            BrowseMedia(
                title=title,
                media_class=MediaClass.PLAYLIST,
                media_content_id=f"{FOLDER_QUEUE_PREFIX}{kind}:{folder_id}",
                media_content_type="music",
                can_play=True,
                can_expand=False,
            )
            for kind, title in (
                ("play_next", "⏭ Play this folder next"),
                ("add_to_queue", "➕ Add this folder to the queue"),
            )
        ]
    return result


async def async_browse(musiccast: Any, zone_id: str, media_id: str) -> BrowseMedia:
    """Browse the list at the path in ``media_id``."""
    if media_id.startswith(MODE_PREFIX):
        mode, _, list_id = media_id[len(MODE_PREFIX) :].partition(":")
        if mode in CLICK_MODES:
            click_mode[player_host(musiccast)] = mode
        return await async_browse(musiccast, zone_id, unquote(list_id))
    parts = media_id.split(":")
    if len(parts) >= 3 and parts[2] == LAST_ALIAS:
        last = last_played.get(player_host(musiccast))
        media_id = last["id"] if last else make_id(parts[1], ())
    source, path, start, query = parse_id(media_id)
    nav = Navigator(musiccast, zone_id, source)
    async with nav.lock:
        data = await nav.goto(path, query)
        max_line = int(data.get("max_line", 0))
        entries = await nav.read(data, start, PAGE_SIZE)

    # In a sorted list, the "Sort by ..." entries open as siblings of it
    # (same indices as in the list above) instead of nesting another level.
    in_sorted = bool(path) and is_sort_name(data.get("menu_name"))
    children = [
        _item(
            source,
            (*path[:-1], start + i)
            if in_sorted and is_sort_name(e.get("text"))
            else (*path, start + i),
            e,
            query,
        )
        for i, e in enumerate(entries)
    ]
    # Ways out on top of every list. Lists reached via "Up" are opened as
    # sub-screens, so the browser's own back arrow can't go further up.
    nav_entries: list[BrowseMedia] = []
    if path:
        up = nav.up_path(path)
        up_name = nav.name_of(up) if up else source.upper()
        nav_entries.append(
            nav_item(
                f"Up: {up_name}" if up_name else "Up one level",
                make_id(source, up, 0, query),
            )
        )
    else:
        nav_entries.append(nav_item("Up: Library (all sources)", LIBRARY_ID))
    if start:
        nav_entries.append(
            nav_item(
                "Previous page",
                make_id(source, path, max(0, start - PAGE_SIZE), query),
            )
        )
    if not path and not start and (resume := resume_item(musiccast)):
        nav_entries.append(resume)
    nav_entries += _queue_entries(musiccast, source, path, start, query, entries)
    children = [*nav_entries, *children]
    if start + len(entries) < max_line:
        children.append(
            BrowseMedia(
                title=f"Next page ({start + len(entries) + 1}–"
                f"{min(start + 2 * PAGE_SIZE, max_line)} of {max_line})",
                media_class=MediaClass.DIRECTORY,
                media_content_id=make_id(source, path, start + len(entries), query),
                media_content_type="music",
                can_play=False,
                can_expand=True,
            )
        )
    title = data.get("menu_name") or source
    if path and isinstance(path[-1], str):
        title = f"Search: {query}"
    if start:
        title = f"{title} ({start + 1}–{start + len(entries)})"
    # The root and the search lists get a search box in the media browser.
    searchable = (
        not path
        or isinstance(path[-1], str)
        or (len(path) == 1 and (data.get("menu_name") or "").lower() == "search")
    )
    return BrowseMedia(
        title=title,
        media_class=MediaClass.DIRECTORY,
        media_content_id=make_id(source, path, start, query),
        media_content_type="music",
        can_play=False,
        can_expand=True,
        children=children,
        can_search=searchable,
        search_media_classes=SEARCH_CLASSES if searchable else None,
    )


async def async_search(
    musiccast: Any,
    zone_id: str,
    source: str,
    search_text: str,
    classes: list[MediaClass] | None = None,
) -> SearchMedia:
    """Search ``source`` from its root search entry."""
    search_text = search_text.strip()
    if not search_text:
        raise SearchError("Enter something to search for")
    nav = Navigator(musiccast, zone_id, source)
    async with nav.lock:
        root = await nav.goto(())
        index = _search_entry_index(await nav.read(root, 0, 64))
        if index is None:
            raise SearchError(f"{source} has no search entry")
        path: tuple[Step, ...] = (f"s{index}",)
        data = await nav.goto(path, search_text)
        entries = await nav.read(data, 0, SEARCH_RESULTS)

    wanted = set(classes or [])
    results: list[BrowseMedia] = []
    group: MediaClass | None = None
    for i, entry in enumerate(entries):
        can_expand, can_play, _ = _attrs(entry)
        text = (entry.get("text") or "").strip()
        if not can_expand and not can_play:
            # group header ("Playlists", "Artists", ...)
            group = SEARCH_GROUPS.get(text.lower())
            continue
        if wanted and group not in wanted:
            continue
        media_class = group
        if text.lower().startswith("all ") and can_expand and not can_play:
            media_class = MediaClass.DIRECTORY
        results.append(_item(source, (*path, i), entry, search_text, media_class))
    return SearchMedia(result=results)


async def async_play(musiccast: Any, zone_id: str, media_id: str) -> None:
    """Play the item at the path in ``media_id`` (or queue it, see click_mode)."""
    if media_id.startswith(FOLDER_QUEUE_PREFIX):
        kind, _, folder_id = media_id[len(FOLDER_QUEUE_PREFIX) :].partition(":")
        await async_enqueue(musiccast, zone_id, unquote(folder_id), kind)
        return
    mode = click_mode.get(player_host(musiccast), "play")
    if mode != "play":
        await async_enqueue(musiccast, zone_id, media_id, mode)
        return
    source, path, _start, query = parse_id(media_id)
    if not path or isinstance(path[-1], str):
        raise HomeAssistantError("Nothing to play here")
    nav = Navigator(musiccast, zone_id, source)
    index = path[-1]
    async with nav.lock:
        data = await nav.goto(path[:-1], query)
        try:
            block = (await nav.info(index - index % 8)).get("list_info") or []
            entry = block[index % 8] if index % 8 < len(block) else {}
        except HomeAssistantError:
            entry = {}
        await musiccast.play_list_media(index, zone_id)

    folder = path[:-1]
    if folder and isinstance(folder[-1], str):
        title = f"Search: {query}"
    else:
        title = data.get("menu_name") or source
    last_played[player_host(musiccast)] = {
        "id": make_id(source, folder, (index // PAGE_SIZE) * PAGE_SIZE, query),
        "title": title,
        "thumbnail": _https_thumbnail(entry.get("thumbnail")) or None,
    }
    if on_last_played_changed:
        on_last_played_changed()


ENQUEUE_TYPES = ("play_now", "play_next", "add_to_queue")
MANAGE_TIMEOUT_MS = 60000


async def async_enqueue(musiccast: Any, zone_id: str, media_id: str, kind: str) -> None:
    """Add the item (track or folder) at the path in ``media_id`` to the play queue.

    The MusicCast app does this with netusb/manageList (documented for
    bookmarks and the like) and the newer types play_now / play_next /
    add_to_queue, on the list the menu cursor is on; ``index`` is the item's
    position in that list.
    """
    if kind not in ENQUEUE_TYPES:
        raise HomeAssistantError(f"Unknown queue action {kind}")
    source, path, _start, query = parse_id(media_id)
    if not path or isinstance(path[-1], str):
        raise HomeAssistantError("Nothing to add here")
    nav = Navigator(musiccast, zone_id, source)
    index = path[-1]
    async with nav.lock:
        await nav.goto(path[:-1], query)
        result = await musiccast.device.request_json(
            "http://{host}/YamahaExtendedControl/v1/netusb/manageList"
            f"?list_id=main&type={kind}&index={index}&zone={zone_id}"
            f"&timeout={MANAGE_TIMEOUT_MS}"
        )
    code = result.get("response_code") if isinstance(result, dict) else None
    if code not in (0, None):
        raise HomeAssistantError(f"The device refused {kind} (response code {code})")
