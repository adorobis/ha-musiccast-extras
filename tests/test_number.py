"""Tests for the MusicCast Extras numbers."""

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.number import (
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from .conftest import Speaker, refresh


@pytest.fixture
def platforms() -> list[Platform]:
    """Set up only the numbers."""
    return [Platform.NUMBER]


async def _set(hass: HomeAssistant, entity_id: str, value: float) -> None:
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: value},
        blocking=True,
    )


async def test_entities(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    init_integration: MockConfigEntry,
) -> None:
    """The numbers each device supports are created with its ranges."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)


@pytest.mark.usefixtures("init_integration")
@pytest.mark.parametrize(
    ("entity_id", "value", "command"),
    [
        pytest.param(
            "number.living_room_balance", 2, ("main/setBalance?value=2", None), id="balance"
        ),
        pytest.param(
            "number.living_room_volume_db",
            -20.5,
            ("main/setActualVolume?mode=db&value=-20.5", None),
            id="volume_db",
        ),
        pytest.param(
            "number.living_room_auro_matic_strength",
            5,
            ("main/setAuroMaticStrength?value=5", None),
            id="auro_matic_strength",
        ),
        pytest.param(
            "number.living_room_preset",
            5,
            ("netusb/recallPreset?zone=main&num=5", None),
            id="netusb_preset",
        ),
        pytest.param(
            "number.terrace_preset",
            2,
            ("netusb/recallPreset?zone=zone2&num=2", None),
            id="netusb_preset_zone",
        ),
    ],
)
async def test_set_receiver(
    hass: HomeAssistant,
    receiver: Speaker,
    entity_id: str,
    value: float,
    command: tuple[str, None],
) -> None:
    """Setting a number sends the device's request."""
    await _set(hass, entity_id, value)
    assert receiver.yxc.commands == [command]


@pytest.mark.usefixtures("init_integration")
async def test_alarm_volume(hass: HomeAssistant, cd_player: Speaker) -> None:
    """The alarm volume is set with the alarm settings."""
    await _set(hass, "number.kitchen_alarm_volume", 30)
    assert cd_player.yxc.commands == [("clock/setAlarmSettings", {"volume": 30})]


@pytest.mark.usefixtures("init_integration")
async def test_preset_empty(hass: HomeAssistant, receiver: Speaker) -> None:
    """An empty preset is refused."""
    with pytest.raises(ServiceValidationError, match="Preset 9 is empty"):
        await _set(hass, "number.living_room_preset", 9)
    assert receiver.yxc.commands == []


@pytest.mark.usefixtures("init_integration")
async def test_preset_zero(hass: HomeAssistant, receiver: Speaker) -> None:
    """Preset 0 does nothing."""
    await _set(hass, "number.living_room_preset", 0)
    assert receiver.yxc.commands == []
    assert hass.states.get("number.living_room_preset").state == "1"


@pytest.mark.usefixtures("init_integration")
async def test_preset_follows_playback(
    hass: HomeAssistant, receiver: Speaker, freezer: FrozenDateTimeFactory
) -> None:
    """The preset is the one playing, 0 when the zone is off or plays something else."""
    state = hass.states.get("number.living_room_preset")
    assert state.state == "1"
    assert state.attributes["preset_name"] == "Album 3"

    receiver.musiccast.data.netusb_album = "Other"
    await refresh(hass, freezer)
    assert hass.states.get("number.living_room_preset").state == "0"

    receiver.musiccast.data.netusb_album = "Album 3"
    receiver.musiccast.data.zones["main"].power = "standby"
    await refresh(hass, freezer)
    assert hass.states.get("number.living_room_preset").state == "0"


@pytest.mark.usefixtures("init_integration")
async def test_tuner_preset(
    hass: HomeAssistant, receiver: Speaker, freezer: FrozenDateTimeFactory
) -> None:
    """On the tuner, the preset is the tuned station's and recalls a tuner preset."""
    receiver.musiccast.data.zones["main"].input = "tuner"
    await refresh(hass, freezer)
    state = hass.states.get("number.living_room_preset")
    assert state.state == "3"
    assert state.attributes["preset_name"] is None

    await _set(hass, "number.living_room_preset", 4)
    assert receiver.yxc.commands == [("tuner/recallPreset?zone=main&band=dab&num=4", None)]
