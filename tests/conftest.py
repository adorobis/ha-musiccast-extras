"""Fixtures for the MusicCast Extras tests.

The core Yamaha MusicCast integration is set up for real, with aiomusiccast's
MusicCastDevice mocked. Its YXC requests (``device.device.request_json``)
go to FakeYxc, which answers from the JSON fixtures and records every other
request, and its list calls go to FakeMenu, a netusb list with one shared
cursor like the device has.
"""

from collections.abc import AsyncGenerator, Generator
import copy
from dataclasses import dataclass, field
from datetime import timedelta
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import unquote

from aiomusiccast.features import DeviceFeature, ZoneFeature
from aiomusiccast.musiccast_data import MusicCastData, MusicCastZoneData
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
import pytest
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant.typing import WebSocketGenerator

from custom_components.musiccast_extras import PLATFORMS, browse
from custom_components.musiccast_extras.const import DOMAIN

FIXTURES = Path(__file__).parent / "fixtures"

RECEIVER_HOST = "192.168.1.10"
CD_HOST = "192.168.1.20"
RECEIVER_ID = "receiver_id"
CD_ID = "cd_id"

# netusb list attribute bits
FOLDER = 0b10
TRACK = 0b100
SEARCH = 0b1000

ALBUMS = [f"Album {i}" for i in range(30)]
# path ("." joined indexes, "s" for the search results) -> (menu name, entries)
TIDAL_MENU: dict[str, tuple[str, list[tuple[str, int]]]] = {
    "": ("TIDAL", [("Search", SEARCH), ("My Music", FOLDER)]),
    "1": ("My Music", [("Albums", FOLDER), ("Tracks", FOLDER)]),
    "1.0": ("Albums", [("Sort by date added", FOLDER), *((a, FOLDER | TRACK) for a in ALBUMS)]),
    "1.0.0": ("Sort by date added", [("Sort by name", FOLDER), ("Newest", FOLDER | TRACK)]),
    "1.1": ("Tracks", [("Song A", TRACK), ("Song B", TRACK)]),
    "s": (
        "Search Results",
        [("Artists", 0), ("Artist A", FOLDER), ("Tracks", 0), ("Found Song", TRACK)],
    ),
}


class FakeMenu:
    """The netusb list of a device: one cursor, moved by every request."""

    def __init__(self) -> None:
        """Start at the root."""
        self.path: list[str] = []
        self.search: str | None = None

    @property
    def key(self) -> str:
        """Return the menu key of the cursor."""
        return ".".join(self.path)

    async def get_list_info(self, source: str, start_index: int) -> dict[str, Any]:
        """Return a page of the list the cursor is on (8 entries)."""
        name, entries = TIDAL_MENU[self.key]
        return {
            "response_code": 0,
            "input": source,
            "menu_layer": len(self.path),
            "max_line": len(entries),
            "index": start_index,
            "menu_name": name,
            "list_info": [
                {
                    "text": text,
                    "thumbnail": f"http://resources.tidal.com:80/{text}.jpg",
                    "attribute": a,
                }
                for text, a in entries[start_index : start_index + 8]
            ],
        }

    async def select_list_item(self, item: int | str, zone_id: str) -> None:
        """Open a folder."""
        _, entries = TIDAL_MENU[self.key]
        if entries[int(item)][1] & FOLDER:
            self.path.append(str(item))

    async def return_in_list(self, zone_id: str) -> None:
        """Go back up."""
        if self.path:
            self.path.pop()

    def run_search(self, text: str) -> None:
        """Open the search results."""
        self.path = ["s"]
        self.search = text


@dataclass
class FakeYxc:
    """Answers the YXC requests of one device from its fixture."""

    features: dict[str, Any]
    responses: dict[str, Any]
    menu: FakeMenu
    commands: list[tuple[str, Any]] = field(default_factory=list)
    fail: bool = False
    refused: set[str] = field(default_factory=set)
    broken: set[str] = field(default_factory=set)

    async def request_json(self, request: str | tuple[str, Any]) -> dict[str, Any]:
        """Answer a request (a URL, or a URL and the JSON to POST)."""
        url, data = request if isinstance(request, tuple) else (request, None)
        path = url.split("/YamahaExtendedControl/v1/", 1)[1]
        base = path.split("?", 1)[0]
        if self.fail or base in self.broken:
            raise TimeoutError("no answer")
        if base == "system/getFeatures":
            return copy.deepcopy(self.features)
        if base in self.responses:
            return {"response_code": 0, **copy.deepcopy(self.responses[base])}
        self.commands.append((path, data))
        if base in self.refused:
            return {"response_code": 3}
        if base == "netusb/setSearchString":
            self.menu.run_search(unquote(data["string"]))
        return {"response_code": 0}

    def paths(self) -> list[str]:
        """Return the command paths sent so far."""
        return [path for path, _ in self.commands]


class FakeRenderer:
    """The DLNA renderer of a device: answers AVTransport actions, follows the position."""

    def __init__(self) -> None:
        """Start stopped."""
        self.actions: list[tuple[str, dict[str, Any]]] = []
        self.position = "0:00:05"
        self.status: dict[str, int] = {}  # action -> HTTP status to answer

    async def request(self, action: str, args: dict[str, Any]) -> MagicMock:
        """Answer an action."""
        self.actions.append((action, args))
        if action == "Seek":
            self.position = args["Target"]
        body = {
            "GetTransportInfo": "<CurrentTransportState>PLAYING</CurrentTransportState>",
            "GetPositionInfo": f"<RelTime>{self.position}</RelTime>",
        }.get(action, "<ok/>")
        response = MagicMock(status=self.status.get(action, 200))
        response.text = AsyncMock(return_value=body)
        return response

    def names(self) -> list[str]:
        """Return the actions received, without the state queries."""
        return [a for a, _ in self.actions if not a.startswith("Get")]


@dataclass
class Speaker:
    """A mocked MusicCast device: the aiomusiccast mock and its fake API."""

    host: str
    musiccast: MagicMock
    yxc: FakeYxc
    renderer: FakeRenderer
    entry: MockConfigEntry


def _zone(name: str, power: str, source: str, inputs: list[str]) -> MusicCastZoneData:
    zone = MusicCastZoneData()
    zone.name = name
    zone.power = power
    zone.input = source
    zone.input_list = inputs
    zone.current_volume = 30
    zone.max_volume = 60
    zone.min_volume = 0
    zone.func_list = ["power", "volume", "mute"]
    zone.features = ZoneFeature.POWER | ZoneFeature.VOLUME | ZoneFeature.MUTE
    return zone


def _musiccast(host: str, device_id: str, model: str, zones: dict[str, MusicCastZoneData]):
    data = MusicCastData()
    data.device_id = device_id
    data.model_name = model
    data.system_version = "1.0"
    data.api_version = "2.11"
    data.mac_addresses = {"wired_lan": device_id.upper()}
    data.network_name = model
    data.input_names = {"tidal": "TIDAL", "server": "Server", "net_radio": "NET RADIO", "cd": "CD"}
    data.zones = zones
    data.netusb_input = "tidal"
    data.netusb_playback = "play"
    data.netusb_artist = "Artist"
    data.netusb_album = "Album 3"
    data.netusb_track = "Track"
    data.netusb_play_time = 0
    data.netusb_preset_list = {
        1: ("tidal", "Album 3"),
        2: ("net_radio", "Album 3"),
        5: ("tidal", "Mix"),
    }

    menu = FakeMenu()
    musiccast = MagicMock()
    musiccast.data = data
    musiccast.ip = host
    musiccast.features = DeviceFeature.WIRED_LAN
    musiccast.media_image_url = None
    musiccast.fetch = AsyncMock()
    musiccast.turn_on = AsyncMock()
    musiccast.select_source = AsyncMock()
    musiccast.play_list_media = AsyncMock()
    musiccast.play_url_media = AsyncMock()
    musiccast.netusb_play = AsyncMock()
    musiccast.get_list_info = AsyncMock(side_effect=menu.get_list_info)
    musiccast.select_list_item = AsyncMock(side_effect=menu.select_list_item)
    musiccast.return_in_list = AsyncMock(side_effect=menu.return_in_list)
    musiccast.device = MagicMock()
    musiccast.device.ip = host
    musiccast.device.enable_polling = AsyncMock()
    return musiccast, menu


def _speaker(fixture: str, host: str, device_id: str, model: str, zones) -> Speaker:
    spec = json.loads((FIXTURES / fixture).read_text())
    musiccast, menu = _musiccast(host, device_id, model, zones)
    yxc = FakeYxc(spec["features"], spec["responses"], menu)
    renderer = FakeRenderer()
    musiccast.device.request_json = AsyncMock(side_effect=yxc.request_json)
    musiccast.device.dlna_avt_request = AsyncMock(side_effect=renderer.request)
    entry = MockConfigEntry(
        domain="yamaha_musiccast",
        unique_id=device_id,
        title=model,
        data={
            CONF_HOST: host,
            "serial": device_id,
            "upnp_description": f"http://{host}:49154/MediaRenderer/desc.xml",
        },
    )
    return Speaker(host, musiccast, yxc, renderer, entry)


@pytest.fixture(autouse=True)
def silent_ssdp_scanner() -> Generator[None]:
    """Keep the SSDP component of the core integration off the network."""
    with (
        patch("homeassistant.components.ssdp.Scanner._async_start_ssdp_listeners"),
        patch("homeassistant.components.ssdp.Scanner._async_stop_ssdp_listeners"),
        patch("homeassistant.components.ssdp.Scanner.async_scan"),
        patch("homeassistant.components.ssdp.Server._async_start_upnp_servers"),
        patch("homeassistant.components.ssdp.Server._async_stop_upnp_servers"),
    ):
        yield


@pytest.fixture(autouse=True)
def fast_menu() -> Generator[None]:
    """Don't wait between the menu reads of the path navigation."""
    with patch.object(browse, "POLL_INTERVAL", 0):
        yield


@pytest.fixture(autouse=True)
def clean_module_state() -> Generator[None]:
    """Reset what the integration keeps at module level between tests."""
    yield
    browse._cursors.clear()
    browse._names.clear()
    browse._locks.clear()
    browse.click_mode.clear()


@pytest.fixture
def receiver() -> Speaker:
    """An AV receiver with two zones, tuner, scenes and HDMI outputs."""
    inputs = ["tidal", "server", "net_radio", "tuner", "hdmi1"]
    return _speaker(
        "receiver.json",
        RECEIVER_HOST,
        RECEIVER_ID,
        "RX-A8A",
        {
            "main": _zone("Living room", "on", "tidal", inputs),
            "zone2": _zone("Terrace", "standby", "net_radio", inputs),
        },
    )


@pytest.fixture
def cd_player() -> Speaker:
    """A CD player on Wi-Fi with an alarm."""
    return _speaker(
        "cd_player.json",
        CD_HOST,
        CD_ID,
        "CD-NT670",
        {"main": _zone("Kitchen", "on", "cd", ["net_radio", "server", "cd"])},
    )


@pytest.fixture
def speakers(receiver: Speaker, cd_player: Speaker) -> list[Speaker]:
    """The devices set up in a test."""
    return [receiver, cd_player]


@pytest.fixture
def platforms() -> list[str] | None:
    """The extras platforms to set up (None: all)."""
    return None


def core_device(
    device_registry: dr.DeviceRegistry, speaker: Speaker, zone: str = "main"
) -> dr.DeviceEntry:
    """Return the core integration's device of a speaker's zone."""
    device_id = speaker.entry.unique_id
    identifier = device_id if zone == "main" else f"{device_id}_{zone}"
    device = device_registry.async_get_device_by_identifier(
        ("yamaha_musiccast", identifier), speaker.entry.entry_id
    )
    assert device is not None
    return device


async def refresh(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Let the extras poll the devices again (once a minute)."""
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """The MusicCast Extras config entry."""
    return MockConfigEntry(domain=DOMAIN, title="MusicCast Extras", data={})


@pytest.fixture
async def core_setup(
    hass: HomeAssistant, speakers: list[Speaker], aioclient_mock: AiohttpClientMocker
) -> list[Speaker]:
    """Set up the core MusicCast integration with the mocked devices."""
    by_host = {s.host: s.musiccast for s in speakers}
    for speaker in speakers:
        # The receiver serves the advanced web setup, the CD player doesn't.
        status = 200 if speaker.host == RECEIVER_HOST else 404
        aioclient_mock.get(f"http://{speaker.host}/Setup/", status=status)
    with patch(
        "homeassistant.components.yamaha_musiccast.MusicCastDevice",
        side_effect=lambda host, *_: by_host[host],
    ):
        for speaker in speakers:
            speaker.entry.add_to_hass(hass)
            assert await hass.config_entries.async_setup(speaker.entry.entry_id)
        await hass.async_block_till_done()
    return speakers


@pytest.fixture
async def init_integration(
    hass: HomeAssistant,
    core_setup: list[Speaker],
    mock_config_entry: MockConfigEntry,
    platforms: list[str] | None,
) -> AsyncGenerator[MockConfigEntry]:
    """Set up MusicCast Extras on top of the core integration."""
    mock_config_entry.add_to_hass(hass)
    with patch(
        "custom_components.musiccast_extras.PLATFORMS",
        platforms if platforms is not None else PLATFORMS,
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED
    yield mock_config_entry
    # Undo the patch of the core media player for the next test.
    if mock_config_entry.state is ConfigEntryState.LOADED:
        assert await hass.config_entries.async_unload(mock_config_entry.entry_id)


RECEIVER_PLAYER = "media_player.living_room_rx_a8a"
TERRACE_PLAYER = "media_player.terrace_rx_a8a_zone2"
CD_PLAYER = "media_player.kitchen_cd_nt670"


async def browse_media(
    hass_ws_client: WebSocketGenerator,
    entity_id: str,
    media_id: str | None = None,
) -> dict[str, Any]:
    """Browse a player through the websocket API; return the result or the error."""
    client = await hass_ws_client()
    message: dict[str, Any] = {"type": "media_player/browse_media", "entity_id": entity_id}
    if media_id is not None:
        message.update(media_content_id=media_id, media_content_type="music")
    await client.send_json_auto_id(message)
    response = await client.receive_json()
    return response["result"] if response["success"] else response["error"]


def titles(result: dict[str, Any]) -> list[str]:
    """Return the titles of a browse result's children."""
    return [child["title"] for child in result["children"]]


def child(result: dict[str, Any], title: str) -> dict[str, Any]:
    """Return the child of a browse result with a title."""
    return next(c for c in result["children"] if c["title"] == title)


async def play_media(hass: HomeAssistant, entity_id: str, media_id: str, **data: Any) -> None:
    """Play media through the media player action."""
    await hass.services.async_call(
        "media_player",
        "play_media",
        {
            "entity_id": entity_id,
            "media_content_id": media_id,
            "media_content_type": "music",
            **data,
        },
        blocking=True,
    )
