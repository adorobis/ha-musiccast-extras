"""Tests for seeking in netusb tracks (streaming services, media servers)."""

from homeassistant.components.media_player import MediaPlayerEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.musiccast_extras import patch as extras_patch

from .conftest import RECEIVER_PLAYER, Speaker

# The play info attribute as devices report it
SEEKABLE = 3439337887  # TIDAL on a WX-021 and an RX-A8A (bit 31 set)
NOT_SEEKABLE = 218112415  # TIDAL on a CD-NT670D


def _play(receiver: Speaker, attribute: int | None, total_time: int = 140) -> None:
    """Let the receiver play TIDAL (its fixture input) with that attribute."""
    receiver.musiccast._netusb_play_info = {"input": "tidal", "attribute": attribute}
    receiver.musiccast.data.netusb_total_time = total_time
    receiver.musiccast.data.netusb_play_time = 21
    receiver.entry.runtime_data.async_update_listeners()


def _seek_supported(hass: HomeAssistant) -> bool:
    features = hass.states.get(RECEIVER_PLAYER).attributes["supported_features"]
    return MediaPlayerEntityFeature.SEEK in MediaPlayerEntityFeature(features)


async def _seek(hass: HomeAssistant, position: float) -> None:
    await hass.services.async_call(
        "media_player",
        "media_seek",
        {"entity_id": RECEIVER_PLAYER, "seek_position": position},
        blocking=True,
    )


@pytest.mark.usefixtures("init_integration")
async def test_seek(hass: HomeAssistant, receiver: Speaker) -> None:
    """A track the device allows seeking in can be seeked."""
    _play(receiver, SEEKABLE)
    await hass.async_block_till_done()
    assert _seek_supported(hass)

    await _seek(hass, 51.6)

    assert receiver.yxc.paths()[-1] == "netusb/setPlayPosition?position=51"
    assert hass.states.get(RECEIVER_PLAYER).attributes["media_position"] == 51


@pytest.mark.usefixtures("init_integration")
@pytest.mark.parametrize("attribute", [NOT_SEEKABLE, None])
async def test_not_seekable(hass: HomeAssistant, receiver: Speaker, attribute: int | None) -> None:
    """Without the device's seek flag there is no seeking."""
    _play(receiver, attribute)
    await hass.async_block_till_done()
    assert not _seek_supported(hass)

    with pytest.raises(
        HomeAssistantError, match=r"does not support action media_player\.media_seek"
    ):
        await _seek(hass, 50)


@pytest.mark.usefixtures("init_integration")
async def test_other_input(hass: HomeAssistant, receiver: Speaker) -> None:
    """The flag belongs to the netusb input; a zone on another input can't seek with it."""
    _play(receiver, SEEKABLE)
    receiver.musiccast.data.netusb_input = "server"
    receiver.entry.runtime_data.async_update_listeners()
    await hass.async_block_till_done()

    assert not _seek_supported(hass)


@pytest.mark.usefixtures("init_integration")
async def test_zone_off(hass: HomeAssistant, receiver: Speaker) -> None:
    """A zone in standby offers no seeking, though the play info still says seekable."""
    _play(receiver, SEEKABLE)
    receiver.musiccast.data.zones["main"].power = "standby"
    receiver.entry.runtime_data.async_update_listeners()
    await hass.async_block_till_done()

    assert not _seek_supported(hass)


@pytest.mark.usefixtures("init_integration")
async def test_refused(hass: HomeAssistant, receiver: Speaker) -> None:
    """The device refusing a position (e.g. beyond the end) is an error."""
    _play(receiver, SEEKABLE)
    await hass.async_block_till_done()
    receiver.yxc.refused.add("netusb/setPlayPosition")

    with pytest.raises(HomeAssistantError):
        await _seek(hass, 500)


class NewerMusicCastDevice:
    """A MusicCastDevice of aiomusiccast after 0.15.0, which can seek itself."""

    def __init__(self, seekable: bool) -> None:
        self.seekable = seekable
        self.seeks: list[float] = []

    @property
    def netusb_seekable(self) -> bool:
        return self.seekable

    async def netusb_seek(self, position: float) -> None:
        self.seeks.append(position)


class FakePlayer:
    _is_netusb = True
    _zone_id = "main"

    def __init__(self, musiccast: NewerMusicCastDevice) -> None:
        zone = type("Zone", (), {"power": "on"})()
        data = type("Data", (), {"zones": {"main": zone}})()
        self.coordinator = type("Coordinator", (), {"musiccast": musiccast, "data": data})()


async def test_newer_aiomusiccast() -> None:
    """With aiomusiccast's own seek support, that is used."""
    musiccast = NewerMusicCastDevice(seekable=True)
    player = FakePlayer(musiccast)

    assert extras_patch._netusb_seekable(player)
    await extras_patch._netusb_seek(player, 75)
    assert musiccast.seeks == [75]

    musiccast.seekable = False
    assert not extras_patch._netusb_seekable(player)
