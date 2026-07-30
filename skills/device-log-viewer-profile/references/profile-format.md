# Device Log Viewer Profile Format

Profiles use schema version `1` and are validated again when `server.py` starts.

```json
{
  "$schema": "../schemas/device-log-viewer-profile.schema.json",
  "schemaVersion": 1,
  "id": "sample-game",
  "displayName": "Sample Game Device Logs",
  "defaultPort": 8765,
  "apps": {
    "android": {
      "default": "com.example.game",
      "presets": [{ "id": "com.example.game", "label": "Sample Android" }]
    },
    "ios": {
      "default": "com.example.game",
      "presets": [{ "id": "com.example.game", "label": "Sample iOS" }]
    }
  },
  "analytics": {
    "enabled": true,
    "marker": "[EventLog]:",
    "parser": "gamefoundation-eventlog",
    "platforms": ["Firebase", "Facebook", "Adjust", "AppsFlyer"]
  }
}
```

## Fields

- `id`: Stable lowercase identifier used by `start.command` to detect which Profile is running.
- `displayName`: Browser title and page heading.
- `defaultPort`: First local port attempted by the launcher. The next ten ports are fallbacks.
- `apps.android` / `apps.ios`: Default App identifier and optional labeled presets. Empty defaults are valid.
- `analytics.enabled`: Shows or hides the “只看埋点” filter.
- `analytics.marker`: Case-insensitive log substring used to recognize analytics lines.
- `analytics.parser`: `plain` displays the remaining payload as one card title; `gamefoundation-eventlog` parses platform, event name, and `key=...,value=...` parameters.
- `analytics.platforms`: Accepted platform prefixes for the GameFoundation parser.

The JSON Schema in `schemas/device-log-viewer-profile.schema.json` is the authoritative structural reference.
