"""Base entity for the extras, attached to the core MusicCast devices."""

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import (
    MAIN_ZONE,
    SIGNAL_NEW_DEVICE,
    MusicCastExtrasConfigEntry,
    MusicCastExtrasDevice,
)


@callback
def async_setup_device_entities(
    hass: HomeAssistant,
    entry: MusicCastExtrasConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    build: Callable[[MusicCastExtrasDevice], Iterable[Entity]],
) -> None:
    """Add the entities of every device, now and for devices added later."""

    @callback
    def _add(device: MusicCastExtrasDevice) -> None:
        async_add_entities(build(device))

    for device in entry.runtime_data.values():
        _add(device)
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE, _add))


class MusicCastExtrasEntity(CoordinatorEntity[MusicCastExtrasDevice]):
    """An entity on a MusicCast device (or one of its zones)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        device: MusicCastExtrasDevice,
        key: str,
        *,
        zone_id: str = MAIN_ZONE,
        translation_key: str | None = None,
        placeholders: dict[str, str] | None = None,
    ) -> None:
        """Initialize.

        key makes the unique ID (with the zone); translation_key (default:
        key) names the entity, with placeholders such as a scene number.
        """
        super().__init__(device)
        self.device = device
        self.zone_id = zone_id
        self.key = key
        self._attr_unique_id = device.unique_id(key, zone_id)
        self._attr_translation_key = translation_key or key
        if placeholders:
            self._attr_translation_placeholders = placeholders
        # Put the entity on the core MusicCast device (or zone device) itself.
        # Home Assistant keeps devices per integration, so a device_info with
        # the same identifiers would create a separate, linked device; an
        # existing device_entry is accepted for config-entry entities instead
        # (as PowerCalc does). Without a core device, fall back to a linked one.
        if core_device := device.core_device_entry(zone_id):
            self.device_entry = core_device
        else:
            self._attr_device_info = DeviceInfo(
                identifiers={device.core_device_identifier(zone_id)},
                name=device.zone_name(zone_id),
                manufacturer="Yamaha Corporation",
                model=device.model,
            )

    async def async_added_to_hass(self) -> None:
        """Remove the entity when its device goes away (its MusicCast entry unloaded)."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self.device.removed_signal, self._async_gone)
        )

    @callback
    def _async_gone(self) -> None:
        self.hass.async_create_task(self.async_remove())

    @property
    def zone_status(self) -> dict[str, Any]:
        """Return this zone's last getStatus."""
        return self.device.zone_status(self.zone_id)

    @property
    def data(self) -> dict[str, Any]:
        """Return the device's last data."""
        return self.device.data or {}
