"""Tests for controlling the CD input of the core media player."""

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.media_player import MediaPlayerEntityFeature, MediaPlayerState
from homeassistant.core import HomeAssistant
import pytest

from .conftest import CD_PLAYER, Speaker, refresh

pytestmark = pytest.mark.usefixtures("init_integration")


async def test_state(hass: HomeAssistant) -> None:
    """While on CD, the player shows the disc's track, time and transport."""
    state = hass.states.get(CD_PLAYER)
    assert state.state == MediaPlayerState.PLAYING
    assert state.attributes["media_title"] == "CD Song"
    assert state.attributes["media_artist"] == "CD Band"
    assert state.attributes["media_album_name"] == "CD Album"
    assert state.attributes["media_duration"] == 200
    assert state.attributes["media_position"] == 42
    features = MediaPlayerEntityFeature(state.attributes["supported_features"])
    assert MediaPlayerEntityFeature.PAUSE in features
    assert MediaPlayerEntityFeature.NEXT_TRACK in features


@pytest.mark.parametrize(
    ("playback", "state"),
    [
        pytest.param("pause", MediaPlayerState.PAUSED),
        pytest.param("stop", MediaPlayerState.IDLE),
    ],
)
async def test_playback_state(
    hass: HomeAssistant,
    cd_player: Speaker,
    freezer: FrozenDateTimeFactory,
    playback: str,
    state: MediaPlayerState,
) -> None:
    """The state follows the CD's playback."""
    cd_player.yxc.responses["cd/getPlayInfo"]["playback"] = playback
    await refresh(hass, freezer)
    assert hass.states.get(CD_PLAYER).state == state


@pytest.mark.parametrize(
    ("service", "playback"),
    [
        pytest.param("media_play", "play"),
        pytest.param("media_pause", "pause"),
        pytest.param("media_stop", "stop"),
        pytest.param("media_next_track", "next"),
        pytest.param("media_previous_track", "previous"),
    ],
)
async def test_transport(
    hass: HomeAssistant, cd_player: Speaker, service: str, playback: str
) -> None:
    """The transport buttons control the CD."""
    await hass.services.async_call("media_player", service, {"entity_id": CD_PLAYER}, blocking=True)
    assert cd_player.yxc.paths() == [f"cd/setPlayback?playback={playback}"]


async def test_other_input(
    hass: HomeAssistant, cd_player: Speaker, freezer: FrozenDateTimeFactory
) -> None:
    """On another input the core media player works as before."""
    cd_player.musiccast.data.zones["main"].input = "net_radio"
    cd_player.musiccast.data.netusb_input = "net_radio"
    cd_player.musiccast.netusb_pause = cd_player.musiccast.netusb_play
    await refresh(hass, freezer)
    state = hass.states.get(CD_PLAYER)
    assert state.attributes["media_title"] == "Track"
    await hass.services.async_call(
        "media_player", "media_pause", {"entity_id": CD_PLAYER}, blocking=True
    )
    cd_player.musiccast.netusb_play.assert_awaited_once()
    assert cd_player.yxc.paths() == []
