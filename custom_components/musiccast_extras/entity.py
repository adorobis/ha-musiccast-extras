"""Base entity for the extras, attached to the core MusicCast devices."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .device import MAIN_ZONE, MusicCastExtrasDevice


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
        suffix = key if zone_id == MAIN_ZONE else f"{zone_id}_{key}"
        self._attr_unique_id = f"{device.device_id}_{suffix}"
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

    @property
    def zone_status(self) -> dict[str, Any]:
        """Return this zone's last getStatus."""
        return self.device.zone_status(self.zone_id)

    @property
    def data(self) -> dict[str, Any]:
        """Return the device's last data."""
        return self.device.data or {}
