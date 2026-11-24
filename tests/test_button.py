"""Tests for the MusicCast Extras buttons."""

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from .conftest import Speaker


@pytest.fixture
def platforms() -> list[Platform]:
    """Set up only the buttons."""
    return [Platform.BUTTON]


async def _press(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )


async def test_entities(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    init_integration: MockConfigEntry,
) -> None:
    """The buttons each device supports are created."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)


@pytest.mark.usefixtures("init_integration")
@pytest.mark.parametrize(
    ("entity_id", "speaker", "path"),
    [
        pytest.param(
            "button.living_room_restart", "receiver", "system/requestSystemReboot", id="restart"
        ),
        pytest.param(
            "button.kitchen_restart_network_module",
            "cd_player",
            "system/requestNetworkReboot",
            id="restart_network",
        ),
        pytest.param(
            "button.living_room_scene_2", "receiver", "main/recallScene?num=2", id="scene"
        ),
        pytest.param(
            "button.terrace_scene_1", "receiver", "zone2/recallScene?num=1", id="zone_scene"
        ),
        pytest.param("button.kitchen_eject_cd", "cd_player", "cd/toggleTray", id="cd_eject"),
    ],
)
async def test_press(
    hass: HomeAssistant,
    request: pytest.FixtureRequest,
    entity_id: str,
    speaker: str,
    path: str,
) -> None:
    """Pressing a button sends its request."""
    device: Speaker = request.getfixturevalue(speaker)
    await _press(hass, entity_id)
    assert device.yxc.paths() == [path]


@pytest.mark.usefixtures("init_integration")
async def test_restart_refused(hass: HomeAssistant, receiver: Speaker) -> None:
    """A refused restart is reported."""
    receiver.yxc.refused.add("system/requestSystemReboot")
    with pytest.raises(HomeAssistantError, match="response code 3"):
        await _press(hass, "button.living_room_restart")


@pytest.mark.usefixtures("init_integration")
async def test_restart_unreachable(hass: HomeAssistant, receiver: Speaker) -> None:
    """A restart the device doesn't answer is reported."""
    receiver.yxc.fail = True
    with pytest.raises(HomeAssistantError, match="Restart request failed"):
        await _press(hass, "button.living_room_restart")


@pytest.mark.usefixtures("init_integration")
async def test_command_unreachable(hass: HomeAssistant, receiver: Speaker) -> None:
    """A command the device doesn't answer is reported with the device."""
    receiver.yxc.fail = True
    with pytest.raises(HomeAssistantError, match=r"RX-A8A \(192.168.1.10\): .* failed"):
        await _press(hass, "button.living_room_scene_1")


@pytest.mark.usefixtures("init_integration")
async def test_command_refused(hass: HomeAssistant, receiver: Speaker) -> None:
    """A command the device refuses is reported with its response code."""
    receiver.yxc.refused.add("main/recallScene")
    with pytest.raises(HomeAssistantError, match=r"main/recallScene\?num=1: response code 3"):
        await _press(hass, "button.living_room_scene_1")


@pytest.mark.usefixtures("init_integration")
async def test_command_unexpected_answer(hass: HomeAssistant, receiver: Speaker) -> None:
    """An answer that isn't a JSON object is reported."""
    receiver.musiccast.device.request_json.side_effect = None
    receiver.musiccast.device.request_json.return_value = ["not", "an", "object"]
    with pytest.raises(HomeAssistantError, match="unexpected answer"):
        await _press(hass, "button.living_room_scene_1")
