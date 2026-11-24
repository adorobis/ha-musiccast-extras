"""Tests for the MusicCast Extras selects."""

from homeassistant.components.select import (
    ATTR_OPTION,
    DOMAIN as SELECT_DOMAIN,
    SERVICE_SELECT_OPTION,
)
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from .conftest import Speaker


@pytest.fixture
def platforms() -> list[Platform]:
    """Set up only the selects."""
    return [Platform.SELECT]


async def _select(hass: HomeAssistant, entity_id: str, option: str) -> None:
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: entity_id, ATTR_OPTION: option},
        blocking=True,
    )


async def test_entities(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    init_integration: MockConfigEntry,
) -> None:
    """The selects each device supports are created with its options."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)


@pytest.mark.usefixtures("init_integration")
@pytest.mark.parametrize(
    ("entity_id", "option", "path"),
    [
        pytest.param(
            "select.living_room_auro_3d_listening_mode",
            "auro_matic",
            "main/setAuro3dListeningMode?mode=auro_matic",
            id="auro_listening_mode",
        ),
        pytest.param(
            "select.living_room_auro_matic_preset",
            "large",
            "main/setAuroMaticPreset?preset=large",
            id="auro_matic_preset",
        ),
        pytest.param(
            "select.living_room_speaker_pattern",
            "2",
            "system/setSpeakerPattern?num=2",
            id="speaker_pattern",
        ),
        pytest.param(
            "select.living_room_preset_name",
            "Album 3 [NET RADIO]",
            "netusb/recallPreset?zone=main&num=2",
            id="preset_same_name",
        ),
        pytest.param(
            "select.terrace_preset_name",
            "Mix",
            "netusb/recallPreset?zone=zone2&num=5",
            id="preset_zone",
        ),
    ],
)
async def test_select_receiver(
    hass: HomeAssistant, receiver: Speaker, entity_id: str, option: str, path: str
) -> None:
    """Selecting an option sends the device's request."""
    await _select(hass, entity_id, option)
    assert receiver.yxc.paths() == [path]


@pytest.mark.usefixtures("init_integration")
async def test_alarm_source(hass: HomeAssistant, cd_player: Speaker) -> None:
    """The alarm source is set with the alarm settings."""
    await _select(hass, "select.kitchen_alarm_source", "cd")
    assert cd_player.yxc.commands == [
        (
            "clock/setAlarmSettings",
            {"detail": {"day": "oneday", "playback_type": "resume", "resume": {"input": "cd"}}},
        )
    ]
