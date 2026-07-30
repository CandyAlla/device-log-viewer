---
name: device-log-viewer-profile
description: Scan a local Unity project and generate a validated Device Log Viewer Profile JSON containing the project name, Android application ID, iOS Bundle ID, analytics marker/parser preset, and default port. Use when configuring DeviceLogViewer for a new project, creating or refreshing a profile, or extracting mobile app identifiers from Unity ProjectSettings.
---

# Device Log Viewer Profile

Generate project-specific configuration for the portable `DeviceLogViewer` without modifying the scanned project.

Run the commands below with the working directory set to the directory containing this `SKILL.md`, so the bundled script and viewer resolve consistently even when the Skill is discovered through a symlink.

## Workflow

1. Resolve the project directory the user placed in scope. The generator accepts either a Unity root or a repository containing one Unity project.
2. Run a read-only preview first:

   ```bash
   python3 scripts/generate_profile.py /absolute/project/path --dry-run
   ```

3. Review the detected `productName`, Android application ID, iOS Bundle ID, analytics marker, parser, and output id. Read [references/profile-format.md](references/profile-format.md) when fields need manual adjustment.
4. Generate the profile. By default it is written to the enclosing `DeviceLogViewer/profiles/<id>.json`:

   ```bash
   python3 scripts/generate_profile.py /absolute/project/path
   ```

   Use `--output /absolute/path/profile.json` when the user requests a specific destination. Use `--unity-root` if the repository contains multiple Unity projects.
5. Validate the generated file with the viewer service:

   ```bash
   python3 ../../server.py --profile /absolute/path/profile.json --print-profile-id
   ```

6. Report the generated path, detected app identifiers, analytics preset, and exact launch command:

   ```bash
   ./start.command profiles/<id>.json
   ```

## Safety

- Treat the target project as read-only. Do not edit Unity `ProjectSettings`, source code, or assets.
- The generator refuses to overwrite an existing profile. Use `--force` only after the user explicitly approves replacing that exact file.
- If identifiers are absent, keep them empty and report that fact; do not invent bundle identifiers.
- If analytics detection is uncertain, use the generated `plain` parser or `--disable-analytics`; do not claim the GameFoundation format without evidence.

## Detection Rules

- Read `ProjectSettings/ProjectSettings.asset` for `productName` and the `applicationIdentifier` values for `Android` and `iPhone`.
- Ignore generated Unity directories such as `Library`, `Temp`, `Logs`, `obj`, and build outputs.
- Recognize `[EventLog]:` plus multiple known platforms (`Firebase`, `Facebook`, `Adjust`, `AppsFlyer`) as the `gamefoundation-eventlog` preset.
- Use `plain` for a custom marker or an unrecognized payload format. Pass `--analytics-marker` to scan for a known custom marker.
