"""Tests for the MusicCast Extras switches."""

from typing import Any

from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from .conftest import Speaker


@pytest.fixture
def platforms() -> list[Platform]:
    """Set up only the switches."""
    return [Platform.SWITCH]


async def test_entities(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    init_integration: MockConfigEntry,
) -> None:
    """The switches each device supports are created."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)


@pytest.mark.usefixtures("init_integration")
@pytest.mark.parametrize(
    ("entity_id", "speaker", "on", "off"),
    [
        pytest.param(
            "switch.living_room_surround_ai",
            "receiver",
            ("main/setSurroundAI?enable=true", None),
            ("main/setSurroundAI?enable=false", None),
            id="surround_ai",
        ),
        pytest.param(
            "switch.living_room_hdmi_out_2",
            "receiver",
            ("system/setHdmiOut2?enable=true", None),
            ("system/setHdmiOut2?enable=false", None),
            id="hdmi_out",
        ),
        pytest.param(
            "switch.kitchen_mono",
            "cd_player",
            ("main/setMono?enable=true", None),
            ("main/setMono?enable=false", None),
            id="mono",
        ),
        pytest.param(
            "switch.kitchen_alarm",
            "cd_player",
            ("clock/setAlarmSettings", {"alarm_on": True}),
            ("clock/setAlarmSettings", {"alarm_on": False}),
            id="alarm_on",
        ),
        pytest.param(
            "switch.kitchen_alarm_enabled",
            "cd_player",
            ("clock/setAlarmSettings", {"detail": {"day": "oneday", "enable": True}}),
            ("clock/setAlarmSettings", {"detail": {"day": "oneday", "enable": False}}),
            id="alarm_enabled",
        ),
        pytest.param(
            "switch.kitchen_alarm_beep",
            "cd_player",
            ("clock/setAlarmSettings", {"detail": {"day": "oneday", "beep": True}}),
            ("clock/setAlarmSettings", {"detail": {"day": "oneday", "beep": False}}),
            id="alarm_beep",
        ),
        pytest.param(
            "switch.kitchen_alarm_snooze",
            "cd_player",
            ("clock/setAlarmSettings", {"detail": {"day": "oneday", "snooze": True}}),
            ("clock/setAlarmSettings", {"detail": {"day": "oneday", "snooze": False}}),
            id="alarm_snooze",
        ),
    ],
)
async def test_turn_on_off(
    hass: HomeAssistant,
    request: pytest.FixtureRequest,
    entity_id: str,
    speaker: str,
    on: tuple[str, Any],
    off: tuple[str, Any],
) -> None:
    """Turning a switch on and off sends the device's requests."""
    device: Speaker = request.getfixturevalue(speaker)
    for service in (SERVICE_TURN_ON, SERVICE_TURN_OFF):
        await hass.services.async_call(
            SWITCH_DOMAIN, service, {ATTR_ENTITY_ID: entity_id}, blocking=True
        )
    assert device.yxc.commands == [on, off]
