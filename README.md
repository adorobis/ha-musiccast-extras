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
- **The device's own lists in the media browser**: recently played, MusicCast playlists, tuner presets.
- **CD control**: play, pause, stop, skip and track info when a zone plays the CD.
- **Entities for what each device supports**: signal info, Wi-Fi signal, tuner station and
  RDS/DAB text, alarm, scenes, balance, volume in dB, Surround:AI, Auro-3D, HDMI outputs,
  speaker pattern, restart.

Everything is detected per device from what it reports (`system/getFeatures` and its
status), not from a list of models: a device only gets the entities and folders it supports.

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

### The device's own lists in the media browser

At the top of a MusicCast player's media browser, next to the inputs:

- **Recently played**: the device's playback history across all network sources (radio
  stations, Tidal tracks, …). Click to play it again (`netusb/recallRecentItem`).
- **MusicCast playlists**: the five playlists you build in the MusicCast app. Play a whole
  playlist or start at a track (`netusb/manageMcPlaylist`).
- **Tuner presets**: the stations stored in the device, per band (FM, DAB, AM) or as one list,
  as the device organises them, with RDS/DAB names (`tuner/recallPreset`).

Each folder only appears on devices that report the function (`recent_info`, `mc_playlist`, a
tuner input).

### CD

While a zone plays the **CD** input, its media player gets play, pause, stop, next and previous
(the core integration only offers them for network sources), shows the track title, artist,
album, position and length, and the device gets an **Eject CD** button (`cd/toggleTray`).

### Entities

Added to the existing MusicCast devices (and zone devices) when the device reports the function:

| Entity | Type | Created when the device reports | API |
| --- | --- | --- | --- |
| Audio format, Sample rate, Bit depth, Bitrate, Signal error | sensor (one per parameter, per zone) | `signal_info`; only the parameters the zone returns | `<zone>/getSignalInfo` |
| Wi-Fi signal (%, with SSID and channel attributes) | sensor | a Wi-Fi connection | `system/getNetworkStatus` |
| Tuner band, Tuner station, Tuner text (RDS radio text / DAB DLS; station attributes: ensemble, bit rate, quality) | sensor | a tuner (`fm`/`am`/`dab`, text with `rds`/`dab`) | `tuner/getPlayInfo` |
| Scene 1 … *n* | button (per zone) | `scene`, `scene_num` | `<zone>/recallScene` |
| Balance | number (per zone, device's range) | `balance` | `<zone>/setBalance` |
| Volume (dB) | number (per zone, device's range) | `actual_volume` in dB mode | `<zone>/setActualVolume` |
| Surround:AI, Adaptive DSP level, Mono | switch (per zone) | `surround_ai`, `adaptive_dsp_level`, `mono` | `<zone>/setSurroundAI`, `setAdaptiveDspLevel`, `setMono` |
| Auro-3D listening mode, Auro-Matic preset, Auro-Matic strength | select / number | Auro-3D in the zone status | `<zone>/setAuro3dListeningMode`, `setAuroMaticPreset`, `setAuroMaticStrength` |
| HDMI out 1 … 3 | switch | `hdmi_out_1` … `hdmi_out_3` | `system/setHdmiOut<n>` |
| Speaker pattern | select (1 … *n*) | `speaker_pattern`, `speaker_pattern_num` | `system/setSpeakerPattern` |
| Alarm (on/off) | switch | clock `alarm` or `multi_alarm` | `clock/setAlarmSettings` |
| Alarm time, Alarm source, Alarm volume, Alarm enabled, Alarm beep, Alarm snooze | time / select / number / switch | clock `alarm` (single alarm), `snooze` | `clock/setAlarmSettings` |
| Alarm 1 … *n* (time, days, source, volume as attributes) | sensor, read-only | clock `multi_alarm` | `clock/getSettings` |
| Restart, Restart network module | button | `system_reboot`, `network_reboot` | `system/requestSystemReboot`, `requestNetworkReboot` |

The Yamaha Extended Control API can restart a device or only its network module, which handles
streaming, MusicCast and DLNA. Restarting the network module helps when streaming hangs while
the amplifier itself still works.

The values are read every minute and a few seconds after the device reports a change (the core
integration's push updates), so changes made on the device or in the MusicCast app show up
quickly. Devices that are offline when the integration starts are skipped; reload MusicCast
Extras once they are back (or after adding a MusicCast device).

### Known limits

- **Play queue**: the device's queue can be read, but there is no known command to play an
  entry from it, so it is not in the media browser.
- **Several alarms** (`multi_alarm`, e.g. TSX-N237D): only the overall on/off switch can be
  changed; the individual alarms are shown read-only, because the format to change them is not
  documented.
- **YPAO volume** (`ypao_volume`) is not included: the device does not report its current state.

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

- Some settings use API calls that are newer than the published Yamaha specification (Surround:AI,
  Auro-3D, adaptive DSP level, mono, playlists). Their names were checked against the devices, but
  if a setting does not take on your device, please open an issue.
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
- **Własne listy urządzenia w przeglądarce mediów**: ostatnio odtwarzane, playlisty MusicCast,
  zapisane stacje tunera.
- **Sterowanie CD**: odtwarzanie, pauza, stop, przewijanie utworów i informacje o płycie.
- **Encje dla funkcji obsługiwanych przez urządzenie**: parametry sygnału (osobno format,
  częstotliwość, rozdzielczość, przepływność), sygnał Wi-Fi, stacja tunera i tekst RDS/DAB, budzik,
  sceny, balans, głośność w dB, Surround:AI, Auro-3D, wyjścia HDMI, konfiguracja głośników,
  przyciski restartu.

Wszystko jest wykrywane osobno dla każdego urządzenia, na podstawie tego, co ono zgłasza, a nie z
listy modeli.

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
