# MusicCast Extras for Home Assistant

[![Marketplace][marketplace-badge]][marketplace-url]
[![Validate][validate-badge]][validate-url]

Extra features for the built-in [Yamaha MusicCast](https://www.home-assistant.io/integrations/yamaha_musiccast/)
integration:

- **Tidal in the media browser**: reliable browsing, search, and "continue where you left off".
- **Search box** on MusicCast players.
- **Title, artist, cover, duration and seeking** for media pushed from Home Assistant
  media sources that provide metadata (e.g. [Radio Nowy Świat](https://github.com/adorobis/ha-nowyswiat)).
- **Resume after a Home Assistant restart** for such media.
- **Restart buttons**: restart a device, or just its network module.

It does not replace the MusicCast integration: it adds to it, so you keep getting
Home Assistant's own MusicCast updates. [Po polsku ↓](#po-polsku)

## Features

### Tidal browsing that plays what you clicked

The core integration doesn't list Tidal in the media browser. Even if it did, its browse IDs
only say "item *n* at menu level *m*". The speaker has a single menu cursor, and the media
browser re-opens parent folders for its header and back button, so the cursor can end up on a
different list than the one on screen. Then the wrong album plays.

MusicCast Extras gives every item an ID with its **full path** from the Tidal root menu (and
search text, if any). Before every browse or play it walks the speaker's cursor to exactly that
list and waits for the speaker to get there.

- Browse *My music*, playlists, albums (all sort orders), artists, mixes, …
- **Search** Tidal from the media browser's search box.
- **Continue: <list>**: the list you last played from, at the top of the Tidal folder. The
  player card's *Browse media* opens it directly while the speaker is on Tidal, with
  **Library (all sources)**, **Up one level** and **Previous page** entries.
- Dashboard link to the last list:
  `/media-browser/<entity_id>/music%2Cmctidal%3Atidal%3A%40last%3A0`
- Tidal covers are loaded over HTTPS, so they also show when Home Assistant is served over HTTPS.

### Search

MusicCast players get the *search* feature. Searching in a Tidal folder searches Tidal; searching
inside a media source folder uses that source's search, if it has one.

### Pushed media with title, cover and seeking

When Home Assistant plays a URL on MusicCast, the core integration sends it to the speaker's
DLNA renderer titled "HomeAssistant", without cover or duration, and the speaker can't seek in it.

For media sources that publish metadata (see [For developers](#for-developers)), MusicCast Extras
instead sends:

- **title, artist, album and cover**, shown on the speaker, in the MusicCast app and in Home Assistant;
- **duration**, so Home Assistant shows a progress bar;
- **seeking**: drag the progress bar.

If the speaker rejects the metadata, the item still plays the core way.

### Resume after a Home Assistant restart

A restart cuts the speaker's connection to Home Assistant. It plays on from its buffer (about
1.5 minutes) and then stops, and the Play button can't restart such a stream. MusicCast Extras
remembers what each speaker was playing and where (saved every 30 s), and:

- right after Home Assistant starts, pushes the stream again on speakers that are still playing
  it, at their live position (a gap of a second or two);
- resumes speakers that stopped less than 10 minutes ago, unless you stopped or paused them yourself;
- **Play** in Home Assistant on a stopped speaker resumes at the saved position.

### Restart buttons

The Yamaha Extended Control API can restart a device (`system/requestSystemReboot`) or only its
network module (`system/requestNetworkReboot`), which handles streaming, MusicCast and DLNA. Each
device gets a **Restart** and/or **Restart network module** button, depending on what it reports
in `system/getFeatures`, on its existing MusicCast device page (*Configuration* section).
Restarting the network module helps when streaming hangs while the amplifier itself still works.

## Requirements

- Home Assistant 2026.11 or newer, with the Yamaha MusicCast integration set up.
- Tidal browsing: a MusicCast device with Tidal signed in (in the MusicCast app).

## Installation

### Marketplace (recommended)

Install it from the Home Assistant **Marketplace** as a custom repository:

1. Settings → **Marketplace**. Adding custom repositories needs the Marketplace to be connected
   to GitHub; it asks for that the first time.
2. ⋮ → **Custom repositories** → add `https://github.com/adorobis/ha-musiccast-extras`, type
   **Integration**.
3. Open **MusicCast Extras**, **Download** it and restart Home Assistant.
4. Settings → Devices & services → **Add integration** → **MusicCast Extras** → Submit.

Or open the repository in your Marketplace directly:

[![Open in your Home Assistant Marketplace][my-badge]][my-url]

Updates then show up in Home Assistant like any other update. Restart Home Assistant after
installing or updating, before using the new version.

### Manual

Copy `custom_components/musiccast_extras` into `config/custom_components/`, restart, then add the
integration as above.

Removing the integration undoes all changes without a restart.

## How it works, and what if Home Assistant changes

MusicCast Extras replaces a few methods of the core MusicCast media player at runtime
([`patch.py`](custom_components/musiccast_extras/patch.py)). Before doing so it checks that
everything it relies on exists. If a Home Assistant update changes the MusicCast integration,
the extras **stay off** and a repair issue says what is missing, while MusicCast itself keeps
working. A weekly CI job runs the same check against the newest Home Assistant release.

## For developers

A media source can provide metadata by registering an async function:

```python
hass.data.setdefault("media_info_providers", {})[DOMAIN] = async_media_info

async def async_media_info(hass, media_content_id: str) -> dict | None:
    # None if the id isn't yours or there is no metadata
    return {
        "title": "Episode 12",
        "artist": "Host name",            # optional
        "album": "Show",                  # optional
        "image": "/api/my_source/cover",  # optional; path on HA or absolute URL
        "duration": 3540.9,               # seconds, optional
        "size": 141636480,                # bytes, optional
        "mime": "audio/mpeg",
        "seekable": True,                 # the server honours HTTP Range requests
    }
```

The speaker fetches the image itself, without Home Assistant credentials, so the image URL
must not require authentication.

## Notes

- Zones of one receiver share a single network player (DLNA renderer), so pushed media and its
  resume point are per device, not per zone.
- Tested with Home Assistant 2026.11 and aiomusiccast 0.15.0.

## Troubleshooting

```yaml
logger:
  logs:
    custom_components.musiccast_extras: debug
```

---

## Po polsku

Dodatki do wbudowanej integracji Yamaha MusicCast:

- **Tidal w przeglądarce mediów**: niezawodne przeglądanie (zawsze gra to, co kliknięto),
  wyszukiwanie i pozycja „Continue” z ostatnio odtwarzaną listą.
- **Wyszukiwarka** na odtwarzaczach MusicCast.
- **Tytuł, wykonawca, okładka, czas trwania i przewijanie** dla mediów wysyłanych z Home Assistant
  przez źródła z metadanymi, np. [Radio Nowy Świat](https://github.com/adorobis/ha-nowyswiat).
- **Wznawianie po restarcie Home Assistant**: od razu, od bieżącego miejsca.
- **Przyciski restartu**: restart urządzenia albo samego modułu sieciowego.

Instalacja: Ustawienia → **Marketplace** (przy pierwszym dodawaniu repozytorium trzeba
połączyć Marketplace z GitHubem) → ⋮ → *Custom repositories* →
`https://github.com/adorobis/ha-musiccast-extras` (typ *Integration*) → pobierz **MusicCast Extras**
→ restart HA → Ustawienia → Urządzenia i usługi → *Dodaj integrację* → **MusicCast Extras**.
Po każdej instalacji i aktualizacji zrestartuj HA, zanim zaczniesz używać nowej wersji.

Dodatek nie zastępuje integracji MusicCast, tylko ją rozszerza. Jeśli aktualizacja Home Assistant
zmieni integrację MusicCast, dodatki się wyłączą (z komunikatem w naprawach), a sam MusicCast będzie
działał dalej.

## License

[MIT](LICENSE)

[marketplace-badge]: https://img.shields.io/badge/Marketplace-custom%20repository-41BDF5.svg
[marketplace-url]: https://www.home-assistant.io/integrations/marketplace
[validate-badge]: https://github.com/adorobis/ha-musiccast-extras/actions/workflows/validate.yml/badge.svg
[validate-url]: https://github.com/adorobis/ha-musiccast-extras/actions/workflows/validate.yml
[my-badge]: https://my.home-assistant.io/badges/marketplace_repository.svg
[my-url]: https://my.home-assistant.io/redirect/marketplace_repository/?owner=adorobis&repository=ha-musiccast-extras&category=integration
