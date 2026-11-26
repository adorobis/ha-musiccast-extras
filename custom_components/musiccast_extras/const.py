"""Constants for MusicCast Extras."""

from typing import overload

from homeassistant.exceptions import HomeAssistantError

DOMAIN = "musiccast_extras"
# Storage of the earlier, unpublished version; migrated on first setup.
LEGACY_DOMAIN = "musiccast_tidal_browse"

# Extras that aiomusiccast now offers as capabilities, which the core
# integration turns into entities: (platform, extras key, capability ID,
# per zone). When the core has the capability, the extras skip their own
# entity and hand its entity ID over to the core one.
CORE_TAKEOVERS: tuple[tuple[str, str, str, bool], ...] = (
    ("switch", "surround_ai", "zone_SURROUND_AI", True),
    ("select", "speaker_pattern", "SPEAKER_PATTERN", False),
)


@overload
def error(key: str, /, **placeholders: object) -> HomeAssistantError: ...
@overload
def error[E: HomeAssistantError](key: str, cls: type[E], /, **placeholders: object) -> E: ...
def error(
    key: str, cls: type[HomeAssistantError] = HomeAssistantError, /, **placeholders: object
) -> HomeAssistantError:
    """Return an error whose message is the translation ``exceptions.<key>``."""
    return cls(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders={name: str(value) for name, value in placeholders.items()},
    )
