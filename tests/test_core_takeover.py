"""Tests for extras that newer aiomusiccast versions give to the core integration."""

from unittest.mock import AsyncMock

from aiomusiccast.capabilities import BinarySetter, EntityType, OptionSetter
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from custom_components.musiccast_extras.const import DOMAIN

from .conftest import RECEIVER_ID, Speaker, async_setup_core

SWITCH = "switch.living_room_surround_ai"
SELECT = "select.living_room_speaker_pattern"


def _add_core_capabilities(receiver: Speaker) -> None:
    """Give the receiver the capabilities aiomusiccast > 0.15.0 builds."""
    data = receiver.musiccast.data
    data.zones["main"].capabilities = [
        BinarySetter(
            "zone_SURROUND_AI", "Surround AI", EntityType.CONFIG, lambda: True, AsyncMock()
        )
    ]
    data.capabilities = [
        OptionSetter(
            "SPEAKER_PATTERN",
            "Speaker Pattern",
            EntityType.CONFIG,
            lambda: 2,
            AsyncMock(),
            {1: "pattern_1", 2: "pattern_2"},
        )
    ]


async def test_core_takes_over(
    hass: HomeAssistant,
    receiver: Speaker,
    cd_player: Speaker,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """The core entities replace the extras' ones and get their entity IDs."""
    mock_config_entry.add_to_hass(hass)
    for platform, key, object_id in (
        ("switch", "surround_ai", "living_room_surround_ai"),
        ("select", "speaker_pattern", "living_room_speaker_pattern"),
    ):
        old = entity_registry.async_get_or_create(
            platform,
            DOMAIN,
            f"{RECEIVER_ID}_{key}",
            config_entry=mock_config_entry,
            suggested_object_id=object_id,
        )
        entity_registry.async_update_entity(old.entity_id, name="Mine", icon="mdi:star")
    _add_core_capabilities(receiver)
    await async_setup_core(hass, [receiver, cd_player], aioclient_mock)
    # Before the extras run, the core entities have suffixed IDs.
    assert hass.states.get(f"{SWITCH}_2")
    assert hass.states.get(f"{SELECT}_2")

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    for platform, entity_id, state in (("switch", SWITCH, "on"), ("select", SELECT, "pattern_2")):
        key = "surround_ai" if platform == "switch" else "speaker_pattern"
        assert entity_registry.async_get_entity_id(platform, DOMAIN, f"{RECEIVER_ID}_{key}") is None
        entry = entity_registry.async_get(entity_id)
        assert entry.platform == "yamaha_musiccast"
        assert (entry.name, entry.icon) == ("Mine", "mdi:star")
        assert hass.states.get(entity_id).state == state
        assert hass.states.get(f"{entity_id}_2") is None
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)


async def test_core_takes_over_new_install(
    hass: HomeAssistant,
    receiver: Speaker,
    cd_player: Speaker,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Without earlier extras entities, the extras just don't duplicate the core ones."""
    _add_core_capabilities(receiver)
    await async_setup_core(hass, [receiver, cd_player], aioclient_mock)
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert entity_registry.async_get(SWITCH).platform == "yamaha_musiccast"
    assert entity_registry.async_get(SELECT).platform == "yamaha_musiccast"
    assert not [
        e
        for e in er.async_entries_for_config_entry(entity_registry, mock_config_entry.entry_id)
        if e.unique_id.endswith(("_surround_ai", "_speaker_pattern"))
    ]
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
