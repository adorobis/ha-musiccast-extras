"""Tests for the device's own lists in the media browser."""

from homeassistant.components.media_player import MediaClass
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import WebSocketGenerator

from .conftest import RECEIVER_PLAYER, Speaker, browse_media, child, play_media, titles

pytestmark = pytest.mark.usefixtures("init_integration")


async def test_play_queue(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, receiver: Speaker
) -> None:
    """The device's play queue shows what plays and plays from an entry."""
    result = await browse_media(hass_ws_client, RECEIVER_PLAYER, "mcx:queue")
    assert titles(result) == ["Queued one", "▶ Queued two"]
    assert child(result, "▶ Queued two")["thumbnail"] == "http://x/t.jpg"
    await play_media(hass, RECEIVER_PLAYER, "mcx:queue:1")
    assert receiver.yxc.paths() == ["netusb/managePlayQueue?type=play&index=1&zone=main"]


async def test_recent(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, receiver: Speaker
) -> None:
    """Recently played lists the known items and recalls one."""
    result = await browse_media(hass_ws_client, RECEIVER_PLAYER, "mcx:recent")
    assert titles(result) == ["Radio Example", "An Album"]
    radio, album = result["children"]
    assert radio["media_class"] == MediaClass.CHANNEL
    assert album["media_class"] == MediaClass.TRACK
    assert album["thumbnail"] == "https://resources.tidal.com/x.jpg"
    await play_media(hass, RECEIVER_PLAYER, album["media_content_id"])
    assert receiver.yxc.paths() == ["netusb/recallRecentItem?zone=main&num=2"]


async def test_playlists(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, receiver: Speaker
) -> None:
    """MusicCast playlists open, and play as a whole or from a track."""
    result = await browse_media(hass_ws_client, RECEIVER_PLAYER, "mcx:playlists")
    assert titles(result) == ["Party", "Chill"]
    assert child(result, "Party")["can_play"]

    result = await browse_media(hass_ws_client, RECEIVER_PLAYER, "mcx:playlist:1")
    assert result["title"] == "Party"
    assert titles(result) == ["First — Band", "Second"]
    result = await browse_media(hass_ws_client, RECEIVER_PLAYER, "mcx:playlist:9")
    assert result["title"] == "Playlist 9"

    await play_media(hass, RECEIVER_PLAYER, "mcx:playlist:1")
    await play_media(hass, RECEIVER_PLAYER, "mcx:playlist:1:1")
    assert receiver.yxc.paths() == [
        "netusb/manageMcPlaylist?bank=1&type=play&index=0&zone=main",
        "netusb/manageMcPlaylist?bank=1&type=play&index=1&zone=main",
    ]


async def test_tuner_presets(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, receiver: Speaker
) -> None:
    """Tuner presets are listed per band and recalled."""
    result = await browse_media(hass_ws_client, RECEIVER_PLAYER, "mcx:tuner")
    assert titles(result) == ["FM", "DAB"]

    result = await browse_media(hass_ws_client, RECEIVER_PLAYER, "mcx:tuner:fm")
    assert titles(result) == ["1. FM ONE (98.40 MHz)", "3. 531 kHz"]
    await play_media(hass, RECEIVER_PLAYER, "mcx:tuner:fm:1")
    assert receiver.yxc.paths() == ["tuner/recallPreset?zone=main&band=fm&num=1"]


async def test_tuner_common_presets(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    receiver: Speaker,
    init_integration: MockConfigEntry,
) -> None:
    """Devices with one preset list for all bands get one folder."""
    receiver.yxc.features["tuner"]["preset"]["type"] = "common"
    receiver.yxc.responses["tuner/getPresetInfo"] = {
        "preset_info": [{"band": "dab", "number": 5, "text": ""}, {"band": "fm", "number": 87500}]
    }
    # The features are read when the integration is set up.
    assert await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()
    result = await browse_media(hass_ws_client, RECEIVER_PLAYER, "mcx:tuner")
    assert titles(result) == ["COMMON"]
    result = await browse_media(hass_ws_client, RECEIVER_PLAYER, "mcx:tuner:common")
    assert titles(result) == ["1. DAB 5", "2. 87.50 MHz"]


async def test_unknown_ids(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    """Unknown folders and items are refused."""
    error = await browse_media(hass_ws_client, RECEIVER_PLAYER, "mcx:nothing")
    assert "Unknown folder" in error["message"]
    with pytest.raises(HomeAssistantError, match="Not playable"):
        await play_media(hass, RECEIVER_PLAYER, "mcx:playlists")


async def test_turns_on(hass: HomeAssistant, receiver: Speaker) -> None:
    """Playing from a list turns a zone that is off on first."""
    receiver.musiccast.data.zones["zone2"].power = "standby"
    await play_media(hass, "media_player.terrace_rx_a8a_zone2", "mcx:recent:1")
    receiver.musiccast.turn_on.assert_awaited_once_with("zone2")
    assert receiver.yxc.paths() == ["netusb/recallRecentItem?zone=zone2&num=1"]
