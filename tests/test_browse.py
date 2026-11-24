"""Tests for path-based browsing, search and playing of the device's lists."""

from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.media_player import MediaClass
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest
from pytest_homeassistant.common import async_fire_time_changed
from pytest_homeassistant.typing import WebSocketGenerator

from custom_components.musiccast_extras import browse

from .conftest import (
    RECEIVER_PLAYER,
    TERRACE_PLAYER,
    Speaker,
    browse_media,
    child,
    play_media,
    titles,
)

pytestmark = pytest.mark.usefixtures("init_integration")

MANAGE = "netusb/manageList?list_id=main&type={kind}&index={index}&zone=main&timeout=60000"


async def search_media(
    hass_ws_client: WebSocketGenerator,
    entity_id: str,
    query: str,
    media_id: str | None = None,
    classes: list[str] | None = None,
) -> dict[str, Any]:
    """Search a player through the websocket API; return the result or the error."""
    client = await hass_ws_client()
    message: dict[str, Any] = {
        "type": "media_player/search_media",
        "entity_id": entity_id,
        "search_query": query,
    }
    if media_id:
        message.update(media_content_id=media_id, media_content_type="music")
    if classes:
        message["media_filter_classes"] = classes
    await client.send_json_auto_id(message)
    response = await client.receive_json()
    return response["result"] if response["success"] else response["error"]


async def test_root(hass_ws_client: WebSocketGenerator) -> None:
    """The top level has the device's own lists next to the core's inputs."""
    result = await browse_media(hass_ws_client, RECEIVER_PLAYER)
    assert titles(result) == [
        "Play queue",
        "Recently played",
        "MusicCast playlists",
        "Tuner presets",
        "Presets",
        "NET RADIO",
        "Server",
        "TIDAL",
    ]


async def test_input_root(hass_ws_client: WebSocketGenerator) -> None:
    """Opening an input from the core's list shows its root menu with a search box."""
    result = await browse_media(hass_ws_client, RECEIVER_PLAYER, "input:tidal")
    assert result["title"] == "TIDAL"
    assert result["media_content_id"] == "mctidal:tidal::0"
    assert result["can_search"]
    assert titles(result) == ["Up: Library (all sources)", "Search", "My Music"]
    assert child(result, "Search")["can_search"]
    my_music = child(result, "My Music")
    assert my_music["media_content_id"] == "mctidal:tidal:1:0"
    assert my_music["media_class"] == MediaClass.DIRECTORY
    assert my_music["thumbnail"] == "https://resources.tidal.com/My Music.jpg"


async def test_library_entry(hass_ws_client: WebSocketGenerator) -> None:
    """The library entry opens the core's top level."""
    result = await browse_media(hass_ws_client, RECEIVER_PLAYER, browse.LIBRARY_ID)
    assert "TIDAL" in titles(result)


async def test_pages(hass_ws_client: WebSocketGenerator) -> None:
    """Long lists are shown 24 entries at a time."""
    result = await browse_media(hass_ws_client, RECEIVER_PLAYER, "mctidal:tidal:1.0:0")
    assert result["title"] == "Albums"
    names = titles(result)
    assert names[:3] == ["Up: My Music", "Click: ▶ Play now (tap to change)", "Sort by date added"]
    assert names[-2:] == ["Album 22", "Next page (25–31 of 31)"]
    album = child(result, "Album 0")
    assert album["media_content_id"] == "mctidal:tidal:1.0.1:0"
    assert album["media_class"] == MediaClass.ALBUM
    assert album["can_play"]
    assert child(result, "Next page (25–31 of 31)")["media_content_id"] == "mctidal:tidal:1.0:24"

    result = await browse_media(hass_ws_client, RECEIVER_PLAYER, "mctidal:tidal:1.0:24")
    assert result["title"] == "Albums (25–31)"
    assert titles(result)[:3] == [
        "Up: My Music",
        "Previous page",
        "Click: ▶ Play now (tap to change)",
    ]
    assert titles(result)[-1] == "Album 29"
    assert child(result, "Previous page")["media_content_id"] == "mctidal:tidal:1.0:0"


async def test_sorted_list(hass_ws_client: WebSocketGenerator) -> None:
    """A sort order opens as a sibling of the list it sorts, and "Up" skips it."""
    await browse_media(hass_ws_client, RECEIVER_PLAYER, "mctidal:tidal:1.0:0")
    result = await browse_media(hass_ws_client, RECEIVER_PLAYER, "mctidal:tidal:1.0.0:0")
    assert result["title"] == "Sort by date added"
    assert child(result, "Up: Albums")["media_content_id"] == "mctidal:tidal:1.0:0"
    assert child(result, "Sort by name")["media_content_id"] == "mctidal:tidal:1.0.0:0"
    assert child(result, "Newest")["media_content_id"] == "mctidal:tidal:1.0.0.1:0"


async def test_cursor_moves(hass_ws_client: WebSocketGenerator, receiver: Speaker) -> None:
    """The menu cursor goes back only as far as needed, and from the root when it moved."""
    menu = receiver.yxc.menu
    await browse_media(hass_ws_client, RECEIVER_PLAYER, "mctidal:tidal:1.0:0")
    assert menu.path == ["1", "0"]
    receiver.musiccast.select_list_item.reset_mock()

    await browse_media(hass_ws_client, RECEIVER_PLAYER, "mctidal:tidal:1.1:0")
    assert menu.path == ["1", "1"]
    assert receiver.musiccast.return_in_list.call_count == 1
    assert [c.args[0] for c in receiver.musiccast.select_list_item.call_args_list] == [1]

    menu.path = ["1"]  # someone used the app
    result = await browse_media(hass_ws_client, RECEIVER_PLAYER, "mctidal:tidal:1.1:0")
    assert menu.path == ["1", "1"]
    assert titles(result)[-2:] == ["Song A", "Song B"]


async def test_menu_stuck(hass_ws_client: WebSocketGenerator, receiver: Speaker) -> None:
    """A menu that doesn't move is reported."""
    receiver.musiccast.select_list_item.side_effect = None
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(browse, "STEP_TIMEOUT", 0)
        error = await browse_media(hass_ws_client, RECEIVER_PLAYER, "mctidal:tidal:1:0")
    assert "did not reach layer 1" in error["message"]


async def test_invalid_id(hass_ws_client: WebSocketGenerator) -> None:
    """A broken ID is reported."""
    error = await browse_media(hass_ws_client, RECEIVER_PLAYER, "mctidal:tidal:x:0")
    assert "Invalid MusicCast browse ID" in error["message"]


async def test_browse_off(hass_ws_client: WebSocketGenerator) -> None:
    """A zone that is off can't browse (Home Assistant refuses before the integration)."""
    error = await browse_media(hass_ws_client, TERRACE_PLAYER, "mctidal:tidal:1:0")
    assert error["message"] == "Player does not support browsing media"


async def test_search(hass_ws_client: WebSocketGenerator, receiver: Speaker) -> None:
    """Search runs on the source's search entry; results are grouped by kind."""
    result = await search_media(hass_ws_client, RECEIVER_PLAYER, " song ")
    found = result["result"]
    assert [(r["title"], r["media_class"]) for r in found] == [
        ("Artist A", MediaClass.ARTIST),
        ("Found Song", MediaClass.TRACK),
    ]
    assert found[1]["media_content_id"] == "mctidal:tidal:s0.3:0:song"
    assert ("netusb/setSearchString", {"list_id": "main", "string": "song", "index": 0}) in (
        receiver.yxc.commands
    )

    result = await search_media(hass_ws_client, RECEIVER_PLAYER, "song", classes=["track"])
    assert [r["title"] for r in result["result"]] == ["Found Song"]

    # The results list can be opened again from its ID.
    result = await browse_media(hass_ws_client, RECEIVER_PLAYER, "mctidal:tidal:s0:0:song")
    assert result["title"] == "Search: song"
    assert result["can_search"]


@pytest.mark.parametrize(
    ("entity_id", "query", "media_id", "message"),
    [
        pytest.param(RECEIVER_PLAYER, " ", None, "Enter something", id="empty"),
        pytest.param(TERRACE_PLAYER, "song", None, "does not support searching", id="off"),
        pytest.param(
            RECEIVER_PLAYER, "song", "mctidal:hdmi1::0", "not supported for hdmi1", id="source"
        ),
    ],
)
async def test_search_refused(
    hass_ws_client: WebSocketGenerator,
    entity_id: str,
    query: str,
    media_id: str | None,
    message: str,
) -> None:
    """Search needs a text, a zone that is on and a source with lists."""
    error = await search_media(hass_ws_client, entity_id, query, media_id)
    assert message in error["message"]


async def test_search_without_entry(
    hass_ws_client: WebSocketGenerator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source without a search entry can't be searched."""
    from . import conftest  # noqa: PLC0415

    monkeypatch.setitem(conftest.TIDAL_MENU, "", ("TIDAL", [("My Music", conftest.FOLDER)]))
    error = await search_media(hass_ws_client, RECEIVER_PLAYER, "song")
    assert "has no search entry" in error["message"]


async def test_play(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_storage: dict[str, Any],
    receiver: Speaker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Playing an entry walks to its list, plays it and remembers the list."""
    await play_media(hass, RECEIVER_PLAYER, "mctidal:tidal:1.1.1:0")
    assert receiver.yxc.menu.path == ["1", "1"]
    receiver.musiccast.play_list_media.assert_awaited_once_with(1, "main")

    freezer.tick(timedelta(seconds=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass_storage["musiccast_extras.last_played"]["data"] == {
        "192.168.1.10": {
            "id": "mctidal:tidal:1.1:0",
            "title": "Tracks",
            "thumbnail": "https://resources.tidal.com/Song B.jpg",
        }
    }

    # The browser opens on that list while the player is on the source.
    result = await browse_media(hass_ws_client, RECEIVER_PLAYER)
    assert result["title"] == "Tracks"
    assert titles(result)[:2] == ["Library (all sources)", "Up: My Music"]
    # And its root offers to continue there.
    result = await browse_media(hass_ws_client, RECEIVER_PLAYER, "mctidal:tidal::0")
    assert child(result, "Continue: Tracks")["media_content_id"] == "mctidal:tidal:1.1:0"
    result = await browse_media(hass_ws_client, RECEIVER_PLAYER, browse.last_alias_id("tidal"))
    assert result["title"] == "Tracks"


async def test_play_search_result(hass: HomeAssistant, receiver: Speaker) -> None:
    """A search result is played by running the search again."""
    await play_media(hass, RECEIVER_PLAYER, "mctidal:tidal:s0.3:0:found%20it")
    assert receiver.yxc.menu.search == "found it"
    receiver.musiccast.play_list_media.assert_awaited_once_with(3, "main")
    assert browse.last_played["192.168.1.10"]["title"] == "Search: found it"


async def test_play_turns_on(hass: HomeAssistant, receiver: Speaker) -> None:
    """A zone that is off is turned on first."""
    receiver.musiccast.data.zones["zone2"].power = "standby"
    await play_media(hass, TERRACE_PLAYER, "mctidal:tidal:1.1.0:0")
    receiver.musiccast.turn_on.assert_awaited_once_with("zone2")
    receiver.musiccast.play_list_media.assert_awaited_once_with(0, "zone2")


async def test_play_nothing(hass: HomeAssistant) -> None:
    """A list itself can't be played."""
    with pytest.raises(HomeAssistantError, match="Nothing to play here"):
        await play_media(hass, RECEIVER_PLAYER, "mctidal:tidal::0")


@pytest.mark.parametrize(
    ("enqueue", "kind"), [pytest.param("next", "play_next"), pytest.param("add", "add_to_queue")]
)
async def test_enqueue(hass: HomeAssistant, receiver: Speaker, enqueue: str, kind: str) -> None:
    """Play next and Add to queue use the device's play queue."""
    await play_media(hass, RECEIVER_PLAYER, "mctidal:tidal:1.1.1:0", enqueue=enqueue)
    assert receiver.yxc.paths() == [MANAGE.format(kind=kind, index=1)]
    receiver.musiccast.play_list_media.assert_not_awaited()


async def test_enqueue_refused(hass: HomeAssistant, receiver: Speaker) -> None:
    """A refused queue request is reported; other items can't be queued."""
    receiver.yxc.refused.add("netusb/manageList")
    with pytest.raises(HomeAssistantError, match="refused play_next"):
        await play_media(hass, RECEIVER_PLAYER, "mctidal:tidal:1.1.1:0", enqueue="next")
    with pytest.raises(HomeAssistantError, match="Only items browsed"):
        await play_media(hass, RECEIVER_PLAYER, "1", enqueue="next")
    with pytest.raises(HomeAssistantError, match="Nothing to add here"):
        await play_media(hass, RECEIVER_PLAYER, "mctidal:tidal::0", enqueue="add")


async def test_click_mode(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, receiver: Speaker
) -> None:
    """The click mode entry switches what a click does; folders can be queued as a whole."""
    result = await browse_media(hass_ws_client, RECEIVER_PLAYER, "mctidal:tidal:1.1:0")
    assert titles(result)[:4] == [
        "Up: My Music",
        "Click: ▶ Play now (tap to change)",
        "⏭ Play this folder next",
        "➕ Add this folder to the queue",
    ]
    switch = child(result, "Click: ▶ Play now (tap to change)")["media_content_id"]
    result = await browse_media(hass_ws_client, RECEIVER_PLAYER, switch)
    assert "Click: ⏭ Play next (tap to change)" in titles(result)

    await play_media(hass, RECEIVER_PLAYER, child(result, "Song B")["media_content_id"])
    folder = child(result, "➕ Add this folder to the queue")["media_content_id"]
    await play_media(hass, RECEIVER_PLAYER, folder)
    assert receiver.yxc.paths() == [
        MANAGE.format(kind="play_next", index=1),
        MANAGE.format(kind="add_to_queue", index=1),
    ]
    receiver.musiccast.play_list_media.assert_not_awaited()


async def test_enqueue_unknown_kind() -> None:
    """Only the device's queue actions are sent."""
    with pytest.raises(HomeAssistantError, match="Unknown queue action"):
        await browse.async_enqueue(None, "main", "mctidal:tidal:1.1.1:0", "delete")
