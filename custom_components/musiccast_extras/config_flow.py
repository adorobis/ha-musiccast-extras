"""Config flow for MusicCast Extras (nothing to configure)."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


class MusicCastExtrasConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add the extras with one click."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm."""
        if user_input is not None:
            return self.async_create_entry(title="MusicCast Extras", data={})
        return self.async_show_form(step_id="user")
