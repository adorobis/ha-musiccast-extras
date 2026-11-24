"""Tests for media-source items pushed with metadata: seeking, resume and the queue."""

from collections.abc import Generator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import quote

import aiohttp
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.media_player import MediaPlayerEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant.typing import WebSocketGenerator

from custom_components.musiccast_extras import url_playback, url_queue

from .conftest import (
    RECEIVER_HOST,
    RECEIVER_PLAYER,
    Speaker,
    browse_media,
    child,
    play_media,
    titles,
)

FOLDER = "media-source://media_source/local/podcast"
EP1 = f"{FOLDER}/ep1.mp3"
EP2 = f"{FOLDER}/ep2.mp3"
EP3 = f"{FOLDER}/ep3.mp3"
EPISODES = {
    EP1: {
        "title": "Odcinek 1: Łódź",
        "artist": "Host",
        "album": "Show",
        "image": "/local/cover.jpg",
        "duration": 600,
        "seekable": True,
        "size": 1234,
    },
    EP2: {"title": "Odcinek 2", "duration": 300, "seekable": True},
    EP3: {"title": "Odcinek 3", "duration": 300},
}


@pytest.fixture(autouse=True)
def no_waiting() -> Generator[None]:
    """Don't wait for the renderer between DLNA requests."""
    with patch.object(url_playback, "asyncio", SimpleNamespace(sleep=AsyncMock())):
        yield


@pytest.fixture(autouse=True)
def clean_records() -> Generator[None]:
    """Forget what was pushed between tests."""
    yield
    url_playback.resume.clear()
    url_playback._tracked.clear()
    url_playback._resuming.clear()
    url_queue.queues.clear()


@pytest.fixture
async def media(hass: HomeAssistant, tmp_path: Path) -> dict[str, dict[str, Any]]:
    """A podcast folder with a metadata provider, like the Nowy Świat integration's."""
    folder = tmp_path / "podcast"
    folder.mkdir()
    for name in ("ep1.mp3", "ep2.mp3", "ep3.mp3", "ep4.mp3"):
        (folder / name).write_bytes(b"ID3")
    (folder / "notes.txt").write_text("not media")
    hass.config.media_dirs = {"local": str(tmp_path)}
    await hass.config.async_update(internal_url="http://example.local:8123")
    infos = {key: dict(value) for key, value in EPISODES.items()}

    async def provider(hass: HomeAssistant, media_id: str) -> dict[str, Any] | None:
        return infos.get(media_id)

    hass.data["media_info_providers"] = {"media_source": provider}
    return infos


def set_playing(speaker: Speaker, title: str, playback: str = "play", play_time: int = 0) -> None:
    """Make the core data say the zone plays a pushed item on the server input."""
    data = speaker.musiccast.data
    data.zones["main"].power = "on"
    data.zones["main"].input = "server"
    data.netusb_input = "server"
    data.netusb_track = title
    data.netusb_playback = playback
    data.netusb_play_time = play_time
    speaker.entry.runtime_data.async_update_listeners()


def pushed_uris(speaker: Speaker) -> list[str]:
    """Return the URIs pushed to the renderer."""
    return [a["CurrentURI"] for n, a in speaker.renderer.actions if n == "SetAVTransportURI"]


@pytest.mark.usefixtures("media", "init_integration")
async def test_push_with_metadata(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    receiver: Speaker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A media-source item is pushed with its title, cover, duration and seek flag."""
    await play_media(hass, RECEIVER_PLAYER, EP1)
    receiver.musiccast.select_source.assert_awaited_once_with("main", "server", "autoplay_disabled")
    assert receiver.renderer.names() == ["Stop", "SetAVTransportURI", "Play"]
    args = dict(receiver.renderer.actions)["SetAVTransportURI"]
    assert args["CurrentURI"].startswith("http://example.local:8123/media/local/podcast/ep1.mp3?")
    metadata = args["CurrentURIMetaData"]
    assert metadata.isascii()  # the speaker refuses multi-byte characters
    assert "&lt;dc:title&gt;Odcinek 1: &amp;#321;&amp;#243;d&amp;#378;&lt;/dc:title&gt;" in metadata
    assert "&lt;upnp:artist&gt;Host&lt;/upnp:artist&gt;" in metadata
    assert "&lt;upnp:album&gt;Show&lt;/upnp:album&gt;" in metadata
    assert "http://example.local:8123/local/cover.jpg" in metadata
    assert 'duration="0:10:00.000"' in metadata
    assert 'size="1234"' in metadata
    assert "DLNA.ORG_OP=01" in metadata

    freezer.tick(timedelta(seconds=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    saved = hass_storage["musiccast_extras.url_resume"]["data"][RECEIVER_HOST]
    assert saved["title"] == "Odcinek 1: Łódź"
    assert saved["seekable"]


@pytest.mark.usefixtures("media", "init_integration")
async def test_seek(hass: HomeAssistant, receiver: Speaker) -> None:
    """A pushed item with a seek flag shows its duration and can be seeked."""
    await play_media(hass, RECEIVER_PLAYER, EP1)
    set_playing(receiver, "Odcinek 1: Łódź")
    await hass.async_block_till_done()
    state = hass.states.get(RECEIVER_PLAYER)
    assert state.attributes["media_duration"] == 600
    assert MediaPlayerEntityFeature.SEEK in MediaPlayerEntityFeature(
        state.attributes["supported_features"]
    )

    await hass.services.async_call(
        "media_player",
        "media_seek",
        {"entity_id": RECEIVER_PLAYER, "seek_position": 125},
        blocking=True,
    )
    assert receiver.renderer.actions[-1] == (
        "Seek",
        {"InstanceID": 0, "Unit": "REL_TIME", "Target": "0:02:05"},
    )
    assert url_playback.resume[RECEIVER_HOST]["position"] == 125


@pytest.mark.usefixtures("media", "init_integration")
async def test_seek_other_media(hass: HomeAssistant) -> None:
    """Only pushed items with a seek flag can be seeked (Home Assistant refuses the rest)."""
    with pytest.raises(
        HomeAssistantError, match=r"does not support action media_player\.media_seek"
    ):
        await hass.services.async_call(
            "media_player",
            "media_seek",
            {"entity_id": RECEIVER_PLAYER, "seek_position": 10},
            blocking=True,
        )


@pytest.mark.usefixtures("media", "init_integration")
async def test_push_retries_once(hass: HomeAssistant, receiver: Speaker) -> None:
    """A request on a closed connection is sent once more."""
    original = receiver.musiccast.device.dlna_avt_request.side_effect
    failures = iter([aiohttp.ClientError("closed")])

    async def request(action: str, args: dict[str, Any]) -> Any:
        if action == "SetAVTransportURI" and (error := next(failures, None)):
            raise error
        return await original(action, args)

    receiver.musiccast.device.dlna_avt_request.side_effect = request
    await play_media(hass, RECEIVER_PLAYER, EP1)
    assert len(pushed_uris(receiver)) == 1
    receiver.musiccast.play_url_media.assert_not_awaited()


@pytest.mark.usefixtures("media", "init_integration")
@pytest.mark.parametrize(
    "broken",
    [
        pytest.param({"status": 500}, id="refused"),
        pytest.param({"error": aiohttp.ClientError("down")}, id="unreachable"),
    ],
)
async def test_push_falls_back(hass: HomeAssistant, receiver: Speaker, broken: dict) -> None:
    """If the renderer refuses the metadata, the item still plays the core way."""
    if "status" in broken:
        receiver.renderer.status["SetAVTransportURI"] = broken["status"]
    else:
        original = receiver.musiccast.device.dlna_avt_request.side_effect

        async def request(action: str, args: dict[str, Any]) -> Any:
            if action == "SetAVTransportURI":
                raise broken["error"]
            return await original(action, args)

        receiver.musiccast.device.dlna_avt_request.side_effect = request
    await play_media(hass, RECEIVER_PLAYER, EP1)
    receiver.musiccast.play_url_media.assert_awaited_once()


@pytest.mark.usefixtures("media", "init_integration")
async def test_no_metadata(hass: HomeAssistant, receiver: Speaker) -> None:
    """An item its provider knows nothing about is played the core way."""
    await play_media(hass, RECEIVER_PLAYER, f"{FOLDER}/ep4.mp3")
    receiver.musiccast.play_url_media.assert_awaited_once()
    assert receiver.renderer.actions == []


@pytest.mark.usefixtures("media", "init_integration")
async def test_follows_position(hass: HomeAssistant, receiver: Speaker) -> None:
    """The position is followed while it plays; a stop by the listener is remembered."""
    await play_media(hass, RECEIVER_PLAYER, EP1)
    set_playing(receiver, "Odcinek 1: Łódź", play_time=50)
    await hass.async_block_till_done()
    record = url_playback.resume[RECEIVER_HOST]
    assert record["position"] == 50
    assert not record["stopped"]

    set_playing(receiver, "Odcinek 1: Łódź", playback="pause", play_time=50)
    await hass.async_block_till_done()
    assert record["stopped"]


@pytest.mark.usefixtures("media", "init_integration")
@pytest.mark.parametrize(
    ("position", "ended", "seek"),
    [
        pytest.param(120, False, "0:02:00", id="saved_position"),
        pytest.param(595, False, None, id="near_the_end"),
        pytest.param(120, True, None, id="ended"),
    ],
)
async def test_play_resumes(
    hass: HomeAssistant, receiver: Speaker, position: int, ended: bool, seek: str | None
) -> None:
    """Play on a stopped pushed item pushes it again, at the saved position."""
    await play_media(hass, RECEIVER_PLAYER, EP1)
    set_playing(receiver, "Odcinek 1: Łódź", playback="stop")
    await hass.async_block_till_done()
    url_playback.resume[RECEIVER_HOST].update(position=position, ended=ended)
    receiver.renderer.actions.clear()

    await hass.services.async_call(
        "media_player", "media_play", {"entity_id": RECEIVER_PLAYER}, blocking=True
    )
    assert len(pushed_uris(receiver)) == 1
    seeks = [a["Target"] for n, a in receiver.renderer.actions if n == "Seek"]
    assert seeks == ([seek] if seek else [])
    receiver.musiccast.netusb_play.assert_not_awaited()


@pytest.mark.usefixtures("media", "init_integration")
async def test_play_without_record(hass: HomeAssistant, receiver: Speaker) -> None:
    """Play on anything else is the core's play."""
    await hass.services.async_call(
        "media_player", "media_play", {"entity_id": RECEIVER_PLAYER}, blocking=True
    )
    receiver.musiccast.netusb_play.assert_awaited_once()


@pytest.mark.usefixtures("media", "init_integration")
async def test_ignored_seek(hass: HomeAssistant, receiver: Speaker, caplog) -> None:
    """A renderer that ignores the seek of a resume gets it three times, then a warning."""
    await play_media(hass, RECEIVER_PLAYER, EP1)
    set_playing(receiver, "Odcinek 1: Łódź", playback="stop")
    await hass.async_block_till_done()
    url_playback.resume[RECEIVER_HOST]["position"] = 120
    original = receiver.renderer.request

    async def request(action: str, args: dict[str, Any]) -> Any:
        response = await original(action, args)
        receiver.renderer.position = "0:00:05"  # the seek doesn't take
        return response

    receiver.musiccast.device.dlna_avt_request.side_effect = request
    await hass.services.async_call(
        "media_player", "media_play", {"entity_id": RECEIVER_PLAYER}, blocking=True
    )
    assert [n for n, _ in receiver.renderer.actions].count("Seek") == 3
    assert "ignored seeking to 0:02:00" in caplog.text


@pytest.mark.usefixtures("media", "init_integration")
async def test_not_playing_no_seek(hass: HomeAssistant, receiver: Speaker, caplog) -> None:
    """A renderer that doesn't start isn't seeked."""
    await play_media(hass, RECEIVER_PLAYER, EP1)
    set_playing(receiver, "Odcinek 1: Łódź", playback="stop")
    await hass.async_block_till_done()
    url_playback.resume[RECEIVER_HOST]["position"] = 120
    receiver.renderer.position = "0:00:00"
    await hass.services.async_call(
        "media_player", "media_play", {"entity_id": RECEIVER_PLAYER}, blocking=True
    )
    assert "Seek" not in receiver.renderer.names()
    assert "did not start playing" in caplog.text


@pytest.mark.usefixtures("media", "init_integration")
async def test_power_cycle_resumes(hass: HomeAssistant, receiver: Speaker) -> None:
    """Switched off while playing and on again on the server input, it plays on."""
    await play_media(hass, RECEIVER_PLAYER, EP1)
    set_playing(receiver, "Odcinek 1: Łódź", play_time=200)
    await hass.async_block_till_done()

    receiver.musiccast.data.zones["main"].power = "standby"
    receiver.entry.runtime_data.async_update_listeners()
    await hass.async_block_till_done()
    assert url_playback.resume[RECEIVER_HOST]["power_off"]["entity_id"] == RECEIVER_PLAYER
    receiver.renderer.actions.clear()

    set_playing(receiver, "", playback="stop")
    await hass.async_block_till_done()
    assert len(pushed_uris(receiver)) == 1
    assert [a["Target"] for n, a in receiver.renderer.actions if n == "Seek"] == ["0:03:20"]
    assert hass.states.get(RECEIVER_PLAYER).attributes["resuming"] is False


@pytest.mark.usefixtures("media", "init_integration")
async def test_power_on_other_input(hass: HomeAssistant, receiver: Speaker) -> None:
    """Switched on to another input, nothing is resumed."""
    await play_media(hass, RECEIVER_PLAYER, EP1)
    set_playing(receiver, "Odcinek 1: Łódź", play_time=200)
    await hass.async_block_till_done()
    receiver.musiccast.data.zones["main"].power = "standby"
    receiver.entry.runtime_data.async_update_listeners()
    await hass.async_block_till_done()
    receiver.renderer.actions.clear()

    receiver.musiccast.data.zones["main"].power = "on"
    receiver.musiccast.data.zones["main"].input = "tidal"
    receiver.entry.runtime_data.async_update_listeners()
    await hass.async_block_till_done()
    assert pushed_uris(receiver) == []


@pytest.mark.usefixtures("media")
@pytest.mark.parametrize(
    ("speaker_state", "stopped", "minutes_ago", "resumed"),
    [
        pytest.param({"playback": "play", "play_time": 300}, False, 1, "0:05:00", id="playing"),
        pytest.param({"playback": "stop"}, False, 1, "0:02:00", id="stopped_recently"),
        pytest.param({"playback": "stop"}, True, 1, None, id="stopped_by_listener"),
        pytest.param({"playback": "stop"}, False, 60, None, id="stopped_long_ago"),
        pytest.param({"playback": "pause"}, False, 1, None, id="paused"),
    ],
)
async def test_resume_after_restart(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    core_setup: list[Speaker],
    mock_config_entry: MockConfigEntry,
    receiver: Speaker,
    speaker_state: dict[str, Any],
    stopped: bool,
    minutes_ago: int,
    resumed: str | None,
) -> None:
    """After a restart, a stream the restart broke is pushed again."""
    updated = dt_util.utcnow() - timedelta(minutes=minutes_ago)
    hass_storage["musiccast_extras.url_resume"] = {
        "version": 1,
        "key": "musiccast_extras.url_resume",
        "data": {
            RECEIVER_HOST: {
                "media_id": EP1,
                "title": "Odcinek 1: Łódź",
                "duration": 600,
                "seekable": True,
                "position": 120,
                "stopped": stopped,
                "updated": updated.isoformat(),
            }
        },
    }
    receiver.yxc.responses["netusb/getPlayInfo"] = {
        "input": "server",
        "track": "Odcinek 1: Łódź",
        **speaker_state,
    }
    receiver.musiccast.data.zones["main"].input = "server"
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    seeks = [a["Target"] for n, a in receiver.renderer.actions if n == "Seek"]
    assert seeks == ([resumed] if resumed else [])
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)


@pytest.mark.usefixtures("media", "init_integration")
async def test_queue_folder(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, receiver: Speaker
) -> None:
    """A podcast folder can be queued; the next item plays when one ends."""
    result = await browse_media(hass_ws_client, RECEIVER_PLAYER, FOLDER)
    assert titles(result)[:3] == [
        "Click: ▶ Play now (tap to change)",
        "⏭ Play this folder next",
        "➕ Add this folder to the queue",
    ]
    await play_media(
        hass, RECEIVER_PLAYER, child(result, "➕ Add this folder to the queue")["media_content_id"]
    )
    # Idle: the first one plays right away, the rest waits (ep4 has no metadata).
    assert [uri.split("?")[0][-7:] for uri in pushed_uris(receiver)] == ["ep1.mp3"]
    assert [e["title"] for e in url_queue.queues[RECEIVER_HOST]] == [
        "ep2.mp3",
        "ep3.mp3",
        "ep4.mp3",
    ]

    set_playing(receiver, "Odcinek 1: Łódź", play_time=590)
    await hass.async_block_till_done()
    result = await browse_media(hass_ws_client, RECEIVER_PLAYER)
    assert titles(result)[0] == "📋 Media queue (3)"
    result = await browse_media(hass_ws_client, RECEIVER_PLAYER, url_queue.LIST_ID)
    assert titles(result) == [
        "▶ Now: Odcinek 1: Łódź",
        "1. ep2.mp3",
        "2. ep3.mp3",
        "3. ep4.mp3",
        "🗑 Clear the queue",
    ]

    # The episode plays to its end: the next one is pushed.
    set_playing(receiver, "Odcinek 1: Łódź", playback="stop", play_time=590)
    await hass.async_block_till_done()
    assert pushed_uris(receiver)[-1].split("?")[0].endswith("ep2.mp3")


@pytest.mark.usefixtures("media", "init_integration")
async def test_queue_controls(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, receiver: Speaker
) -> None:
    """Play next / Add to queue, Next, playing from an entry and clearing the queue."""
    await play_media(hass, RECEIVER_PLAYER, EP1)
    set_playing(receiver, "Odcinek 1: Łódź", play_time=10)
    await hass.async_block_till_done()
    await play_media(hass, RECEIVER_PLAYER, EP3, enqueue="add")
    await play_media(hass, RECEIVER_PLAYER, EP2, enqueue="next")
    assert [e["title"] for e in url_queue.queues[RECEIVER_HOST]] == ["Odcinek 2", "Odcinek 3"]
    assert len(pushed_uris(receiver)) == 1
    state = hass.states.get(RECEIVER_PLAYER)
    assert MediaPlayerEntityFeature.NEXT_TRACK in MediaPlayerEntityFeature(
        state.attributes["supported_features"]
    )

    await hass.services.async_call(
        "media_player", "media_next_track", {"entity_id": RECEIVER_PLAYER}, blocking=True
    )
    assert pushed_uris(receiver)[-1].split("?")[0].endswith("ep2.mp3")
    assert [e["title"] for e in url_queue.queues[RECEIVER_HOST]] == ["Odcinek 3"]
    set_playing(receiver, "Odcinek 2", play_time=10)
    await hass.async_block_till_done()

    await play_media(hass, RECEIVER_PLAYER, EP1, enqueue="add")
    await play_media(hass, RECEIVER_PLAYER, "mcxq:item:1")
    assert pushed_uris(receiver)[-1].split("?")[0].endswith("ep1.mp3")
    assert url_queue.queues[RECEIVER_HOST] == []

    await play_media(hass, RECEIVER_PLAYER, EP3, enqueue="add")
    await play_media(hass, RECEIVER_PLAYER, url_queue.CLEAR_ID)
    assert url_queue.queues[RECEIVER_HOST] == []
    result = await browse_media(hass_ws_client, RECEIVER_PLAYER, url_queue.LIST_ID)
    assert titles(result)[-1] == "(empty: use ➕ at the top of a podcast folder)"


@pytest.mark.usefixtures("media", "init_integration")
async def test_click_mode_queues(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, receiver: Speaker
) -> None:
    """With the click mode on Add to queue, a click on an episode queues it."""
    await play_media(hass, RECEIVER_PLAYER, EP1)
    set_playing(receiver, "Odcinek 1: Łódź", play_time=10)
    await hass.async_block_till_done()
    switch = f"mcxq:mode:play_next:{quote(FOLDER, safe='')}"
    result = await browse_media(hass_ws_client, RECEIVER_PLAYER, switch)
    assert titles(result)[0] == "Click: ⏭ Play next (tap to change)"
    await play_media(hass, RECEIVER_PLAYER, EP2)
    assert [e["title"] for e in url_queue.queues[RECEIVER_HOST]] == ["Odcinek 2"]
    assert len(pushed_uris(receiver)) == 1


@pytest.mark.usefixtures("media", "init_integration")
async def test_queue_errors(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, media: dict[str, Any]
) -> None:
    """Stale or unknown queue entries are refused; items gone from their source are skipped."""
    with pytest.raises(HomeAssistantError, match="The queue has changed"):
        await play_media(hass, RECEIVER_PLAYER, "mcxq:item:5")
    with pytest.raises(HomeAssistantError, match="Unknown queue action"):
        await play_media(hass, RECEIVER_PLAYER, "mcxq:nothing")
    error = await browse_media(hass_ws_client, RECEIVER_PLAYER, "mcxq:other")
    assert "Unknown queue folder" in error["message"]
    with pytest.raises(HomeAssistantError, match="can't be queued"):
        await play_media(hass, RECEIVER_PLAYER, f"{FOLDER}/ep4.mp3", enqueue="add")

    url_queue.queues[RECEIVER_HOST] = [{"media_id": f"{FOLDER}/gone.mp3", "title": "Gone"}]
    assert not await url_queue.async_play_next(
        hass.data["entity_components"]["media_player"].get_entity(RECEIVER_PLAYER)
    )


@pytest.mark.usefixtures("media", "init_integration")
async def test_push_turns_on(hass: HomeAssistant, receiver: Speaker) -> None:
    """A zone that is off is turned on before the push."""
    await play_media(hass, "media_player.terrace_rx_a8a_zone2", EP1)
    receiver.musiccast.turn_on.assert_awaited_once_with("zone2")
    receiver.musiccast.select_source.assert_awaited_once_with(
        "zone2", "server", "autoplay_disabled"
    )


@pytest.mark.usefixtures("media", "init_integration")
async def test_power_on_much_later(
    hass: HomeAssistant, receiver: Speaker, freezer: FrozenDateTimeFactory
) -> None:
    """Switched on again more than 12 hours later, nothing is resumed."""
    await play_media(hass, RECEIVER_PLAYER, EP1)
    set_playing(receiver, "Odcinek 1: Łódź", play_time=200)
    await hass.async_block_till_done()
    receiver.musiccast.data.zones["main"].power = "standby"
    receiver.entry.runtime_data.async_update_listeners()
    await hass.async_block_till_done()
    receiver.renderer.actions.clear()

    freezer.tick(timedelta(hours=13))
    set_playing(receiver, "", playback="stop")
    await hass.async_block_till_done()
    assert pushed_uris(receiver) == []
    assert "power_off" not in url_playback.resume[RECEIVER_HOST]


@pytest.mark.usefixtures("media", "init_integration")
async def test_power_on_resume_fails(hass: HomeAssistant, receiver: Speaker, caplog) -> None:
    """A resume after power-on that the renderer refuses is logged, not raised."""
    await play_media(hass, RECEIVER_PLAYER, EP1)
    set_playing(receiver, "Odcinek 1: Łódź", play_time=200)
    await hass.async_block_till_done()
    receiver.musiccast.data.zones["main"].power = "standby"
    receiver.entry.runtime_data.async_update_listeners()
    await hass.async_block_till_done()

    receiver.renderer.status["Play"] = 500
    set_playing(receiver, "", playback="stop")
    await hass.async_block_till_done()
    assert "Could not resume media_player.living_room_rx_a8a after power-on" in caplog.text
    assert hass.states.get(RECEIVER_PLAYER).attributes["resuming"] is False


@pytest.mark.usefixtures("media")
@pytest.mark.parametrize(
    ("case", "log"),
    [
        pytest.param("moved_on", "", id="moved_on"),
        pytest.param("no_answer", "", id="no_answer"),
        pytest.param("refused", "Could not resume", id="refused"),
    ],
)
async def test_restart_not_resumed(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    core_setup: list[Speaker],
    mock_config_entry: MockConfigEntry,
    receiver: Speaker,
    caplog: pytest.LogCaptureFixture,
    case: str,
    log: str,
) -> None:
    """After a restart nothing is pushed if the speaker moved on, doesn't answer or refuses."""
    hass_storage["musiccast_extras.url_resume"] = {
        "version": 1,
        "key": "musiccast_extras.url_resume",
        "data": {
            RECEIVER_HOST: {
                "media_id": EP1,
                "title": "Odcinek 1: Łódź",
                "duration": 600,
                "seekable": True,
                "position": 120,
                "stopped": False,
                "updated": dt_util.utcnow().isoformat(),
            }
        },
    }
    track = "Something else" if case == "moved_on" else "Odcinek 1: Łódź"
    receiver.yxc.responses["netusb/getPlayInfo"] = {
        "input": "server",
        "track": track,
        "playback": "play",
        "play_time": 300,
    }
    if case == "no_answer":
        receiver.yxc.broken.add("netusb/getPlayInfo")
    if case == "refused":
        receiver.renderer.status["Play"] = 500
    receiver.musiccast.data.zones["main"].input = "server"
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert "Seek" not in receiver.renderer.names()
    assert log in caplog.text
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)


@pytest.mark.usefixtures("media", "init_integration")
async def test_resume_without_provider(hass: HomeAssistant, receiver: Speaker) -> None:
    """A stopped item its source no longer knows is left to the core's play."""
    await play_media(hass, RECEIVER_PLAYER, EP1)
    set_playing(receiver, "Odcinek 1: Łódź", playback="stop")
    await hass.async_block_till_done()
    del hass.data["media_info_providers"]["media_source"]
    await hass.services.async_call(
        "media_player", "media_play", {"entity_id": RECEIVER_PLAYER}, blocking=True
    )
    receiver.musiccast.netusb_play.assert_awaited_once()


@pytest.mark.usefixtures("media", "init_integration")
async def test_search_in_media_source(hass_ws_client: WebSocketGenerator) -> None:
    """Search inside a media source goes to that source."""
    client = await hass_ws_client()
    await client.send_json_auto_id(
        {
            "type": "media_player/search_media",
            "entity_id": RECEIVER_PLAYER,
            "search_query": "odcinek",
            "media_content_id": FOLDER,
            "media_content_type": "music",
        }
    )
    response = await client.receive_json()
    assert response["success"], response
    assert isinstance(response["result"]["result"], list)


@pytest.mark.usefixtures("media", "init_integration")
async def test_queue_in_folders(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, receiver: Speaker
) -> None:
    """Folders with episodes list the queue while it has items; others are left alone."""
    result = await browse_media(
        hass_ws_client, RECEIVER_PLAYER, "media-source://media_source/local/."
    )
    assert titles(result) == ["podcast"]

    await play_media(hass, RECEIVER_PLAYER, EP1)
    set_playing(receiver, "Odcinek 1: Łódź", play_time=10)
    await hass.async_block_till_done()
    await play_media(hass, RECEIVER_PLAYER, EP2, enqueue="add")
    result = await browse_media(hass_ws_client, RECEIVER_PLAYER, FOLDER)
    assert "📋 Media queue (1)" in titles(result)


@pytest.mark.usefixtures("media", "init_integration")
async def test_end_with_empty_queue(
    hass: HomeAssistant, receiver: Speaker, caplog: pytest.LogCaptureFixture
) -> None:
    """An episode that ends with nothing queued just ends."""
    await play_media(hass, RECEIVER_PLAYER, EP1)
    set_playing(receiver, "Odcinek 1: Łódź", play_time=590)
    await hass.async_block_till_done()
    set_playing(receiver, "Odcinek 1: Łódź", playback="stop", play_time=590)
    await hass.async_block_till_done()
    assert "item ended, the queue is empty" in caplog.text
    assert url_playback.resume[RECEIVER_HOST]["ended"]


@pytest.mark.usefixtures("media", "init_integration")
async def test_next_after_end_fails(
    hass: HomeAssistant, receiver: Speaker, caplog: pytest.LogCaptureFixture
) -> None:
    """A next episode the renderer refuses is logged."""
    await play_media(hass, RECEIVER_PLAYER, EP1)
    set_playing(receiver, "Odcinek 1: Łódź", play_time=590)
    await hass.async_block_till_done()
    await play_media(hass, RECEIVER_PLAYER, EP2, enqueue="add")
    receiver.renderer.status["Play"] = 500
    set_playing(receiver, "Odcinek 1: Łódź", playback="stop", play_time=590)
    await hass.async_block_till_done()
    assert "Could not play the next queued item" in caplog.text
