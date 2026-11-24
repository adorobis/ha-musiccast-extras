"""Tests for setting up and unloading MusicCast Extras and its device data."""

from datetime import timedelta
from typing import Any
from unittest.mock import patch

from aiomusiccast.exceptions import MusicCastConnectionException
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.yamaha_musiccast.media_player import MusicCastMediaPlayer
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, STATE_UNAVAILABLE, Platform
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
import pytest
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed

from custom_components.musiccast_extras import browse, patch as extras_patch
from custom_components.musiccast_extras.const import DOMAIN

from .conftest import RECEIVER_ID, Speaker, core_device, refresh


@pytest.mark.usefixtures("core_setup")
async def test_setup_and_unload(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    """Setting up patches the core media player, unloading restores it."""
    original = MusicCastMediaPlayer.__dict__["async_browse_media"]
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert MusicCastMediaPlayer.__dict__["async_browse_media"] is not original
    assert hass.states.get("button.living_room_restart")

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    assert MusicCastMediaPlayer.__dict__["async_browse_media"] is original


@pytest.mark.usefixtures("core_setup")
async def test_incompatible(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, issue_registry: ir.IssueRegistry
) -> None:
    """Without what the patch relies on, nothing is patched and a repair issue is raised."""
    original = MusicCastMediaPlayer.__dict__["async_browse_media"]
    mock_config_entry.add_to_hass(hass)
    with patch(
        "custom_components.musiccast_extras.patch.missing_hooks",
        return_value=["MusicCastMediaPlayer.async_browse_media"],
    ):
        assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    assert MusicCastMediaPlayer.__dict__["async_browse_media"] is original
    issue = issue_registry.async_get_issue(DOMAIN, "incompatible")
    assert issue.translation_placeholders == {"missing": "MusicCastMediaPlayer.async_browse_media"}


@pytest.mark.usefixtures("init_integration")
async def test_patch_applies_once() -> None:
    """Patching twice keeps the first originals."""
    patched = MusicCastMediaPlayer.__dict__["async_browse_media"]
    extras_patch.apply()
    assert MusicCastMediaPlayer.__dict__["async_browse_media"] is patched


@pytest.mark.parametrize(
    "broken", [pytest.param("system/getFeatures", id="features"), pytest.param("", id="state")]
)
async def test_device_skipped(
    hass: HomeAssistant,
    core_setup: list[Speaker],
    mock_config_entry: MockConfigEntry,
    receiver: Speaker,
    broken: str,
) -> None:
    """A device that can't be read gets no extras; the others do."""
    original = receiver.yxc.request_json

    async def request_json(request: str | tuple[str, Any]) -> dict[str, Any]:
        url = request[0] if isinstance(request, tuple) else request
        if broken in url:
            raise MusicCastConnectionException
        return await original(request)

    receiver.musiccast.device.request_json.side_effect = request_json
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("button.living_room_restart") is None
    assert hass.states.get("button.kitchen_restart_network_module")
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)


@pytest.mark.usefixtures("core_setup")
async def test_legacy_storage(
    hass: HomeAssistant, hass_storage: dict[str, Any], mock_config_entry: MockConfigEntry
) -> None:
    """Data stored by the earlier, unpublished version is taken over once."""
    last = {"192.168.1.10": {"id": "mctidal:tidal:1:0", "title": "My Music", "thumbnail": None}}
    hass_storage["musiccast_tidal_browse.last_played"] = {
        "version": 1,
        "key": "musiccast_tidal_browse.last_played",
        "data": last,
    }
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert browse.last_played == last
    assert hass_storage["musiccast_extras.last_played"]["data"] == last
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)


@pytest.mark.usefixtures("core_setup")
async def test_registry_migration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    receiver: Speaker,
) -> None:
    """Entities of 1.2.x are renamed and moved onto the MusicCast devices."""
    mock_config_entry.add_to_hass(hass)
    linked = device_registry.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id, identifiers={(DOMAIN, RECEIVER_ID)}
    )
    kept = device_registry.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id, identifiers={(DOMAIN, "renamed")}
    )
    device_registry.async_update_device(kept.id, name_by_user="Mine")
    old = entity_registry.async_get_or_create(
        "button",
        DOMAIN,
        f"{RECEIVER_ID}_restart",
        config_entry=mock_config_entry,
        device_id=linked.id,
        suggested_object_id="musiccast_extras_restart",
        original_name="Restart",
    )
    other = entity_registry.async_get_or_create(
        "button", DOMAIN, "someone_else_restart", config_entry=mock_config_entry
    )
    assert old.entity_id == "button.musiccast_extras_restart"

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    migrated = entity_registry.async_get(
        entity_registry.async_get_entity_id("button", DOMAIN, f"{RECEIVER_ID}_restart")
    )
    assert migrated.entity_id == "button.living_room_restart"
    assert migrated.device_id == core_device(device_registry, receiver).id
    assert device_registry.async_get(linked.id) is None
    assert device_registry.async_get(kept.id) is not None
    assert entity_registry.async_get(other.entity_id) is not None
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)


@pytest.mark.usefixtures("init_integration")
async def test_configuration_url(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    receiver: Speaker,
    cd_player: Speaker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The devices' "Visit" link opens their web page and follows IP changes."""
    main = core_device(device_registry, receiver)
    zone2 = core_device(device_registry, receiver, "zone2")
    cd = core_device(device_registry, cd_player)
    assert main.configuration_url == "http://192.168.1.10/Setup/"
    assert zone2.configuration_url == "http://192.168.1.10/Setup/"
    assert cd.configuration_url == "http://192.168.1.20/"

    device_registry.async_update_device(zone2.id, configuration_url="http://example.com/")
    receiver.yxc.responses["system/getNetworkStatus"]["ip_address"] = "192.168.1.11"
    await refresh(hass, freezer)
    assert device_registry.async_get(main.id).configuration_url == "http://192.168.1.11/Setup/"
    assert device_registry.async_get(zone2.id).configuration_url == "http://example.com/"


@pytest.mark.parametrize("platforms", [[Platform.BUTTON]])
@pytest.mark.usefixtures("init_integration")
async def test_failed_refreshes(
    hass: HomeAssistant, receiver: Speaker, freezer: FrozenDateTimeFactory
) -> None:
    """A few failed polls in a row keep the data, more make the entities unavailable."""
    receiver.yxc.fail = True
    for _ in range(3):
        await refresh(hass, freezer)
        assert hass.states.get("button.living_room_restart").state != STATE_UNAVAILABLE
    await refresh(hass, freezer)
    assert hass.states.get("button.living_room_restart").state == STATE_UNAVAILABLE

    receiver.yxc.fail = False
    await refresh(hass, freezer)
    assert hass.states.get("button.living_room_restart").state != STATE_UNAVAILABLE


@pytest.mark.parametrize("platforms", [[Platform.SWITCH]])
@pytest.mark.usefixtures("init_integration")
async def test_device_event(
    hass: HomeAssistant, receiver: Speaker, freezer: FrozenDateTimeFactory
) -> None:
    """A change the device reports is picked up shortly after, without waiting for the poll."""
    receiver.yxc.responses["main/getStatus"]["surround_ai"] = False
    on_event = receiver.musiccast.register_callback.call_args_list[-1].args[0]
    on_event()
    freezer.tick(timedelta(seconds=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("switch.living_room_surround_ai").state == "off"


async def test_unload_stops_events(
    hass: HomeAssistant, init_integration: MockConfigEntry, receiver: Speaker
) -> None:
    """Unloading stops listening to the devices."""
    on_event = receiver.musiccast.register_callback.call_args_list[-1].args[0]
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    receiver.musiccast.remove_callback.assert_any_call(on_event)


@pytest.mark.parametrize(
    ("target", "name", "missing"),
    [
        pytest.param(
            "aiomusiccast.musiccast_device.MusicCastDevice",
            "get_list_info",
            "MusicCastDevice.get_list_info",
            id="device_method",
        ),
        pytest.param(
            "aiomusiccast.pyamaha.NetUSB",
            "set_search_string",
            "pyamaha.NetUSB.set_search_string",
            id="netusb_method",
        ),
        pytest.param(
            "aiomusiccast.musiccast_media_content.MusicCastMediaContent",
            "BROWSABLE_INPUTS",
            "MusicCastMediaContent.BROWSABLE_INPUTS",
            id="browsable_inputs",
        ),
        pytest.param(
            "homeassistant.components.yamaha_musiccast.media_player.MusicCastMediaPlayer",
            "media_duration",
            "MusicCastMediaPlayer.media_duration",
            id="entity_property",
        ),
    ],
)
def test_missing_hooks(
    monkeypatch: pytest.MonkeyPatch, target: str, name: str, missing: str
) -> None:
    """What the patch relies on is checked by name."""
    module, _, cls = target.rpartition(".")
    owner = getattr(__import__(module, fromlist=[cls]), cls)
    if callable(owner.__dict__.get(name)) or isinstance(owner.__dict__.get(name), staticmethod):
        monkeypatch.delattr(owner, name)  # a method: checked to exist
    else:
        monkeypatch.setattr(owner, name, None)  # checked to be a tuple or a property
    assert missing in extras_patch.missing_hooks()


def test_missing_media_player(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the core media player class nothing else is checked."""
    from homeassistant.components.yamaha_musiccast import media_player  # noqa: PLC0415

    monkeypatch.delattr(media_player, "MusicCastMediaPlayer")
    assert extras_patch.missing_hooks() == ["MusicCastMediaPlayer"]


def test_remove_without_apply() -> None:
    """Undoing the patch when it isn't applied does nothing."""
    original = MusicCastMediaPlayer.__dict__["async_browse_media"]
    extras_patch.remove()
    assert MusicCastMediaPlayer.__dict__["async_browse_media"] is original


@pytest.mark.parametrize(
    "started", [pytest.param(True, id="started"), pytest.param(False, id="unloaded_first")]
)
async def test_setup_during_startup(
    hass: HomeAssistant,
    core_setup: list[Speaker],
    mock_config_entry: MockConfigEntry,
    started: bool,
) -> None:
    """Set up while Home Assistant starts, the resume check waits for the start."""
    hass.set_state(CoreState.not_running)
    mock_config_entry.add_to_hass(hass)
    with patch(
        "custom_components.musiccast_extras.url_playback.async_resume_after_restart"
    ) as resume:
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        resume.assert_not_called()
        if not started:
            assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()
    assert resume.called is started
    if started:
        assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
