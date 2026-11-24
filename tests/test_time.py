"""Tests for the MusicCast Extras alarm time."""

from datetime import time

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.time import ATTR_TIME, DOMAIN as TIME_DOMAIN, SERVICE_SET_VALUE
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNKNOWN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from .conftest import Speaker, refresh


@pytest.fixture
def platforms() -> list[Platform]:
    """Set up only the alarm time."""
    return [Platform.TIME]


async def test_entities(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    init_integration: MockConfigEntry,
) -> None:
    """Only the device with an alarm gets an alarm time."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)


@pytest.mark.usefixtures("init_integration")
async def test_set(hass: HomeAssistant, cd_player: Speaker) -> None:
    """Setting the time sends it as hhmm."""
    await hass.services.async_call(
        TIME_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: "time.kitchen_alarm_time", ATTR_TIME: time(6, 5)},
        blocking=True,
    )
    assert cd_player.yxc.commands == [
        ("clock/setAlarmSettings", {"detail": {"day": "oneday", "time": "0605"}})
    ]


@pytest.mark.usefixtures("init_integration")
async def test_invalid_time(
    hass: HomeAssistant, cd_player: Speaker, freezer: FrozenDateTimeFactory
) -> None:
    """A time the device reports in another format is unknown."""
    cd_player.yxc.responses["clock/getSettings"]["alarm"]["oneday"]["time"] = "7:15"
    await refresh(hass, freezer)
    assert hass.states.get("time.kitchen_alarm_time").state == STATE_UNKNOWN
