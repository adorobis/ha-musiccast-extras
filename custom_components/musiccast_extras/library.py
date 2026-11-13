"""Media browser folders for the device's own lists.

Added to the root of a MusicCast player's media browser, each only when
the device reports the function in system/getFeatures:
- Recently played (netusb "recent_info"): getRecentInfo / recallRecentItem;
- MusicCast playlists (netusb "mc_playlist"): getMcPlaylistName,
  getMcPlaylist, manageMcPlaylist?type=play;
- Tuner presets (tuner func_list, per band or "common" depending on the
  preset type): tuner/getPresetInfo, tuner/recallPreset.

IDs: mcx:recent[:<num>], mcx:playlists, mcx:playlist:<bank>[:<index>],
mcx:tuner:<band>[:<num>] (numbers are 1-based like on the device, playlist
indexes 0-based).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.media_player import BrowseError, BrowseMedia, MediaClass

from .browse import _https_thumbnail
from .device import MusicCastExtrasDevice

PREFIX = "mcx"
PAGE = 8  # the device returns at most 8 playlist lines per request
INPUT_CLASSES = {"net_radio": MediaClass.CHANNEL, "tuner": MediaClass.CHANNEL}
TUNER_BANDS = ("am", "fm", "dab")


def is_library_id(media_id: str | None) -> bool:
    """Return True for IDs handled here."""
    return bool(media_id) and media_id.startswith(f"{PREFIX}:")


def _folder(
    media_id: str, title: str, *, can_play: bool = False, thumbnail: str | None = None
) -> BrowseMedia:
    return BrowseMedia(
        media_class=MediaClass.DIRECTORY,
        media_content_id=media_id,
        media_content_type="music",
        title=title,
        can_play=can_play,
        can_expand=True,
        thumbnail=thumbnail,
    )


def _item(media_id: str, title: str, media_class: str, thumbnail: str | None = None) -> BrowseMedia:
    return BrowseMedia(
        media_class=media_class,
        media_content_id=media_id,
        media_content_type="music",
        title=title,
        can_play=True,
        can_expand=False,
        thumbnail=_https_thumbnail(thumbnail) or None,
    )


def _tuner_bands(device: MusicCastExtrasDevice) -> list[str]:
    preset = device.features.raw.get("tuner", {}).get("preset", {})
    if preset.get("type") == "common":
        return ["common"]
    return [b for b in TUNER_BANDS if b in device.features.tuner]


def root_folders(device: MusicCastExtrasDevice) -> list[BrowseMedia]:
    """Return the folders this device supports, for the browser root."""
    folders = []
    if "recent_info" in device.features.netusb:
        folders.append(_folder(f"{PREFIX}:recent", "Recently played"))
    if "mc_playlist" in device.features.netusb:
        folders.append(_folder(f"{PREFIX}:playlists", "MusicCast playlists"))
    if "tuner" in device.features.inputs and _tuner_bands(device):
        folders.append(_folder(f"{PREFIX}:tuner", "Tuner presets"))
    return folders


async def async_browse(device: MusicCastExtrasDevice, media_id: str) -> BrowseMedia:
    """Return a folder."""
    parts = media_id.split(":")[1:]
    kind = parts[0] if parts else ""
    if kind == "recent":
        return await _recent(device)
    if kind == "playlists":
        return await _playlists(device)
    if kind == "playlist" and len(parts) == 2:
        return await _playlist(device, int(parts[1]))
    if kind == "tuner" and len(parts) == 1:
        folder = _folder(media_id, "Tuner presets")
        folder.children = [_folder(f"{PREFIX}:tuner:{b}", b.upper()) for b in _tuner_bands(device)]
        return folder
    if kind == "tuner" and len(parts) == 2:
        return await _tuner(device, parts[1])
    raise BrowseError(f"Unknown folder {media_id}")


async def _recent(device: MusicCastExtrasDevice) -> BrowseMedia:
    entries = (await device.get("netusb/getRecentInfo")).get("recent_info", [])
    folder = _folder(f"{PREFIX}:recent", "Recently played")
    folder.children = [
        _item(
            f"{PREFIX}:recent:{num}",
            entry["text"],
            INPUT_CLASSES.get(entry.get("input", ""), MediaClass.TRACK),
            entry.get("albumart_url"),
        )
        for num, entry in enumerate(entries, 1)
        if entry.get("text") and entry.get("input") not in (None, "", "unknown")
    ]
    return folder


async def _playlists(device: MusicCastExtrasDevice) -> BrowseMedia:
    names = (await device.get("netusb/getMcPlaylistName")).get("name_list", [])
    folder = _folder(f"{PREFIX}:playlists", "MusicCast playlists")
    folder.children = [
        _folder(f"{PREFIX}:playlist:{bank}", name, can_play=True)
        for bank, name in enumerate(names, 1)
    ]
    return folder


def _track_title(track: dict[str, Any], index: int) -> str:
    title = track.get("track") or track.get("text") or track.get("title") or track.get("name")
    artist = track.get("artist")
    if title and artist:
        return f"{title} — {artist}"
    return title or f"{index + 1}"


async def _playlist(device: MusicCastExtrasDevice, bank: int) -> BrowseMedia:
    names = (await device.get("netusb/getMcPlaylistName")).get("name_list", [])
    title = names[bank - 1] if 0 < bank <= len(names) else f"Playlist {bank}"
    tracks: list[dict[str, Any]] = []
    while True:
        page = await device.get(f"netusb/getMcPlaylist?bank={bank}&index={len(tracks)}")
        lines = page.get("track_info", [])
        tracks += lines
        if not lines or len(tracks) >= page.get("max_line", 0):
            break
    folder = _folder(f"{PREFIX}:playlist:{bank}", title, can_play=bool(tracks))
    folder.children = [
        _item(
            f"{PREFIX}:playlist:{bank}:{index}",
            _track_title(track, index),
            MediaClass.TRACK,
            track.get("albumart_url") or track.get("thumbnail"),
        )
        for index, track in enumerate(tracks)
    ]
    return folder


def _preset_title(preset: dict[str, Any]) -> str:
    text = (preset.get("text") or "").strip()
    number = preset.get("number") or 0
    if preset.get("band") == "fm" and number:
        freq = f"{number / 1000:.2f} MHz"
        return f"{text} ({freq})" if text else freq
    if preset.get("band") == "am" and number:
        return f"{text} ({number} kHz)" if text else f"{number} kHz"
    return text or f"DAB {number}"


async def _tuner(device: MusicCastExtrasDevice, band: str) -> BrowseMedia:
    presets = (await device.get(f"tuner/getPresetInfo?band={band}")).get("preset_info", [])
    folder = _folder(f"{PREFIX}:tuner:{band}", band.upper())
    folder.children = [
        _item(f"{PREFIX}:tuner:{band}:{num}", f"{num}. {_preset_title(p)}", MediaClass.CHANNEL)
        for num, p in enumerate(presets, 1)
        if p.get("band") not in (None, "", "unknown")
    ]
    return folder


async def async_play(device: MusicCastExtrasDevice, zone_id: str, media_id: str) -> None:
    """Play an item (or a whole playlist)."""
    parts = media_id.split(":")[1:]
    kind = parts[0] if parts else ""
    if kind == "recent" and len(parts) == 2:
        await device.command(f"netusb/recallRecentItem?zone={zone_id}&num={int(parts[1])}")
    elif kind == "playlist" and len(parts) in (2, 3):
        index = int(parts[2]) if len(parts) == 3 else 0
        await device.command(
            f"netusb/manageMcPlaylist?bank={int(parts[1])}&type=play&index={index}&zone={zone_id}"
        )
    elif kind == "tuner" and len(parts) == 3:
        await device.command(
            f"tuner/recallPreset?zone={zone_id}&band={parts[1]}&num={int(parts[2])}"
        )
    else:
        raise BrowseError(f"Not playable: {media_id}")
