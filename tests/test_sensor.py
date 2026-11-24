"""Tests for the MusicCast Extras sensors."""

from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_UNKNOWN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from .conftest import Speaker, refresh


@pytest.fixture
def platforms() -> list[Platform]:
    """Set up only the sensors."""
    return [Platform.SENSOR]


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_entities(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    init_integration: MockConfigEntry,
) -> None:
    """The sensors each device supports are created."""
    await snapshot_platform(hass, entity_registry, snapshot, init_integration.entry_id)


@pytest.mark.usefixtures("init_integration")
@pytest.mark.parametrize(
    ("path", "value", "entity_id", "state"),
    [
        pytest.param(
            "main/getSignalInfo",
            {"audio": {"format": "", "fs": "---", "bit": "24 bit", "bitrate": 320, "error": 2}},
            "sensor.living_room_signal_error",
            "check_cable",
            id="signal_error",
        ),
        pytest.param(
            "main/getSignalInfo",
            {"audio": {"format": "", "fs": "---", "bit": "24 bit", "bitrate": 320, "error": 9}},
            "sensor.living_room_signal_error",
            "unknown",
            id="signal_error_unlisted",
        ),
        pytest.param(
            "main/getSignalInfo",
            {"audio": {"format": "---", "fs": "", "bit": "", "bitrate": 320, "error": 0}},
            "sensor.living_room_audio_format",
            STATE_UNKNOWN,
            id="signal_no_format",
        ),
        pytest.param(
            "main/getSignalInfo",
            {"audio": {"format": "", "fs": "", "bit": "", "bitrate": 320, "error": None}},
            "sensor.living_room_bitrate",
            "320",
            id="signal_bitrate",
        ),
        pytest.param(
            "tuner/getPlayInfo",
            {
                "band": "fm",
                "fm": {"freq": 98400},
                "rds": {"program_service": " ", "radio_text_b": "B"},
            },
            "sensor.living_room_tuner_station",
            "98.40 MHz",
            id="fm_frequency",
        ),
        pytest.param(
            "tuner/getPlayInfo",
            {"band": "fm", "fm": {"freq": 98400}, "rds": {"program_service": "FM ONE "}},
            "sensor.living_room_tuner_station",
            "FM ONE",
            id="fm_rds_name",
        ),
        pytest.param(
            "tuner/getPlayInfo",
            {"band": "fm", "fm": {"freq": 98400}, "rds": {"radio_text_b": "Text B "}},
            "sensor.living_room_tuner_text",
            "Text B",
            id="fm_rds_text",
        ),
        pytest.param(
            "tuner/getPlayInfo",
            {"band": "am", "am": {"freq": 531}},
            "sensor.living_room_tuner_station",
            "531 kHz",
            id="am_frequency",
        ),
        pytest.param(
            "tuner/getPlayInfo",
            {"band": "am", "am": {"freq": 531}},
            "sensor.living_room_tuner_text",
            STATE_UNKNOWN,
            id="am_no_text",
        ),
        pytest.param(
            "tuner/getPlayInfo",
            {"band": "", "fm": {}},
            "sensor.living_room_tuner_band",
            STATE_UNKNOWN,
            id="no_band",
        ),
        pytest.param(
            "tuner/getPlayInfo",
            {"band": "am", "am": {"freq": 0}},
            "sensor.living_room_tuner_station",
            STATE_UNKNOWN,
            id="no_frequency",
        ),
    ],
)
async def test_receiver_values(
    hass: HomeAssistant,
    receiver: Speaker,
    freezer: FrozenDateTimeFactory,
    path: str,
    value: dict[str, Any],
    entity_id: str,
    state: str,
) -> None:
    """The sensors follow what the device reports."""
    receiver.yxc.responses[path] = value
    await refresh(hass, freezer)
    assert hass.states.get(entity_id).state == state


@pytest.mark.usefixtures("entity_registry_enabled_by_default", "init_integration")
@pytest.mark.parametrize(
    ("network", "wifi", "mac"),
    [
        pytest.param(
            {"connection": "wireless_lan", "wireless_lan": {"strength": -1}},
            STATE_UNKNOWN,
            STATE_UNKNOWN,
            id="link_down",
        ),
        pytest.param(
            {"connection": "extend_1", "mac_address": {"wired_lan": "aabbccddee11"}},
            STATE_UNKNOWN,
            "aa:bb:cc:dd:ee:11",
            id="extender_uses_wired_mac",
        ),
        pytest.param(
            {"connection": "wired_lan", "mac_address": {"wired_lan": "short"}},
            STATE_UNKNOWN,
            "short",
            id="unusual_mac",
        ),
    ],
)
async def test_network_values(
    hass: HomeAssistant,
    cd_player: Speaker,
    freezer: FrozenDateTimeFactory,
    network: dict[str, Any],
    wifi: str,
    mac: str,
) -> None:
    """Wi-Fi signal and MAC address follow the connection in use."""
    cd_player.yxc.responses["system/getNetworkStatus"] = {"ip_address": "192.168.1.20", **network}
    await refresh(hass, freezer)
    assert hass.states.get("sensor.kitchen_wi_fi_signal").state == wifi
    assert hass.states.get("sensor.kitchen_mac_address").state == mac


@pytest.fixture
def multi_alarm(cd_player: Speaker) -> None:
    """Give the CD player three alarms instead of one."""
    cd_player.yxc.responses["clock/getSettings"] = {
        "multi_alarm": {
            "alarm_on": True,
            "settings": [
                {
                    "enable": True,
                    "time": "0630",
                    "day_of_week": 0b0111110,
                    "volume": 15,
                    "beep": False,
                    "snooze": True,
                    "resume": {"input": "net_radio"},
                },
                {"enable": False, "time": "0900"},
                {"enable": True, "time": "12"},
            ],
        }
    }


@pytest.mark.usefixtures("multi_alarm", "init_integration")
async def test_multi_alarm(
    hass: HomeAssistant, cd_player: Speaker, freezer: FrozenDateTimeFactory
) -> None:
    """A device with several alarms gets one sensor per alarm."""
    first = hass.states.get("sensor.kitchen_alarm_1")
    assert first.state == "06:30"
    assert first.attributes["days"] == ["monday", "tuesday", "wednesday", "thursday", "friday"]
    assert first.attributes["input"] == "net_radio"
    assert hass.states.get("sensor.kitchen_alarm_2").state == "off"
    assert hass.states.get("sensor.kitchen_alarm_3").state == "12"

    cd_player.yxc.responses["clock/getSettings"]["multi_alarm"]["settings"] = []
    await refresh(hass, freezer)
    assert hass.states.get("sensor.kitchen_alarm_1").state == STATE_UNKNOWN
