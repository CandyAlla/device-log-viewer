#!/usr/bin/env python3
"""Read a Unity project and generate a Device Log Viewer Profile."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Iterable


SKIP_DIRECTORIES = {
    ".git",
    ".idea",
    ".vs",
    ".vscode",
    "Build",
    "Builds",
    "Library",
    "Logs",
    "MemoryCaptures",
    "obj",
    "Temp",
    "UserSettings",
}
TEXT_SUFFIXES = {
    ".asmdef",
    ".cs",
    ".java",
    ".js",
    ".json",
    ".kt",
    ".m",
    ".md",
    ".mm",
    ".swift",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
GAMEFOUNDATION_MARKER = "[EventLog]:"
GAMEFOUNDATION_PLATFORMS = ["Firebase", "Facebook", "Adjust", "AppsFlyer"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="只读扫描 Unity ProjectSettings，并生成 Device Log Viewer Profile。"
    )
    parser.add_argument("project_root", nargs="?", default=".", help="项目或 Unity 工程根目录")
    parser.add_argument("--unity-root", help="项目中存在多个 Unity 工程时，明确指定其中一个")
    parser.add_argument("--output", help="输出 JSON；默认写入 DeviceLogViewer/profiles/<id>.json")
    parser.add_argument("--id", dest="profile_id", help="覆盖自动生成的 Profile id")
    parser.add_argument("--display-name", help="覆盖页面显示名称")
    parser.add_argument("--port", type=int, default=8765, help="默认服务端口（默认 8765）")
    parser.add_argument("--analytics-marker", help="覆盖自动识别的埋点日志标记")
    parser.add_argument("--disable-analytics", action="store_true", help="不生成埋点筛选配置")
    parser.add_argument("--dry-run", action="store_true", help="仅在标准输出显示结果，不写文件")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的输出文件")
    return parser.parse_args()


def walk_files(root: Path) -> Iterable[Path]:
    for current_root, directory_names, file_names in os.walk(root):
        directory_names[:] = [name for name in directory_names if name not in SKIP_DIRECTORIES]
        current = Path(current_root)
        for file_name in file_names:
            yield current / file_name


def find_unity_root(project_root: Path, explicit_root: str | None) -> Path:
    if explicit_root:
        candidate = Path(explicit_root).expanduser()
        if not candidate.is_absolute():
            candidate = project_root / candidate
        candidate = candidate.resolve()
        if not (candidate / "ProjectSettings" / "ProjectSettings.asset").is_file():
            raise ValueError(f"指定目录不是 Unity 工程：{candidate}")
        return candidate

    direct_settings = project_root / "ProjectSettings" / "ProjectSettings.asset"
    if direct_settings.is_file():
        return project_root

    candidates = []
    for file_path in walk_files(project_root):
        try:
            relative_parts = file_path.relative_to(project_root).parts
        except ValueError:
            continue
        if len(relative_parts) > 6:
            continue
        if relative_parts[-2:] == ("ProjectSettings", "ProjectSettings.asset"):
            candidates.append(file_path.parent.parent.resolve())
    unique_candidates = sorted(set(candidates))
    if not unique_candidates:
        raise ValueError(f"未在 {project_root} 找到 Unity ProjectSettings/ProjectSettings.asset。")
    if len(unique_candidates) > 1:
        choices = "\n".join(f"  - {candidate}" for candidate in unique_candidates)
        raise ValueError(f"发现多个 Unity 工程，请用 --unity-root 指定：\n{choices}")
    return unique_candidates[0]


def decode_unity_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return str(json.loads(value))
        except json.JSONDecodeError:
            pass
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def parse_unity_settings(settings_file: Path) -> tuple[str, str, str]:
    try:
        text = settings_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"无法读取 {settings_file}：{exc}") from exc

    product_match = re.search(r"^  productName:\s*(.+?)\s*$", text, re.MULTILINE)
    product_name = decode_unity_scalar(product_match.group(1)) if product_match else settings_file.parents[1].name

    identifiers: dict[str, str] = {}
    identifier_block = re.search(r"^  applicationIdentifier:\s*\n((?:    [^\n]*\n?)*)", text, re.MULTILINE)
    if identifier_block:
        for platform, raw_value in re.findall(r"^    ([A-Za-z0-9_]+):\s*(.*?)\s*$", identifier_block.group(1), re.MULTILINE):
            identifiers[platform] = decode_unity_scalar(raw_value)
    android_id = identifiers.get("Android", "").strip()
    ios_id = identifiers.get("iPhone", "").strip()
    return product_name.strip(), android_id, ios_id


def slugify(value: str, fallback: str = "unity-project") -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.casefold()).strip("-")
    return (slug or fallback)[:64].strip("-")


def valid_profile_id(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?", value))


def scan_analytics(unity_root: Path, marker_override: str | None) -> tuple[bool, str, str, list[str], list[str]]:
    marker = marker_override.strip() if marker_override else GAMEFOUNDATION_MARKER
    matched_files = []
    discovered_platforms = set()
    for file_path in walk_files(unity_root):
        if file_path.suffix.casefold() not in TEXT_SUFFIXES:
            continue
        try:
            if file_path.stat().st_size > 2 * 1024 * 1024:
                continue
            text = file_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if marker not in text:
            continue
        matched_files.append(str(file_path.relative_to(unity_root)))
        for platform in GAMEFOUNDATION_PLATFORMS:
            if platform.casefold() in text.casefold():
                discovered_platforms.add(platform)

    if not matched_files:
        return False, "", "plain", [], []
    platforms = [platform for platform in GAMEFOUNDATION_PLATFORMS if platform in discovered_platforms]
    parser_name = "gamefoundation-eventlog" if marker == GAMEFOUNDATION_MARKER and len(platforms) >= 2 else "plain"
    return True, marker, parser_name, platforms if parser_name == "gamefoundation-eventlog" else [], matched_files


def app_platform(app_id: str, label: str) -> dict[str, object]:
    if not app_id:
        return {"default": "", "presets": []}
    return {"default": app_id, "presets": [{"id": app_id, "label": label}]}


def main() -> int:
    args = parse_args()
    project_root = Path(args.project_root).expanduser().resolve()
    if not project_root.is_dir():
        print(f"错误：项目目录不存在：{project_root}", file=sys.stderr)
        return 2
    if not 1024 <= args.port <= 65525:
        print("错误：--port 必须在 1024–65525 之间。", file=sys.stderr)
        return 2

    try:
        unity_root = find_unity_root(project_root, args.unity_root)
        product_name, android_id, ios_id = parse_unity_settings(
            unity_root / "ProjectSettings" / "ProjectSettings.asset"
        )
        profile_id = args.profile_id or slugify(product_name, slugify(unity_root.name))
        if not valid_profile_id(profile_id):
            raise ValueError("Profile id 格式无效；请使用小写字母、数字、点、下划线或连字符。")
        analytics_enabled, marker, parser_name, platforms, matched_files = scan_analytics(
            unity_root, args.analytics_marker
        )
        if args.disable_analytics:
            analytics_enabled, marker, parser_name, platforms = False, "", "plain", []
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    viewer_root = Path(__file__).resolve().parents[3]
    output_path = Path(args.output).expanduser() if args.output else viewer_root / "profiles" / f"{profile_id}.json"
    if not output_path.is_absolute():
        output_path = Path.cwd() / output_path
    output_path = output_path.resolve()
    schema_path = viewer_root / "schemas" / "device-log-viewer-profile.schema.json"
    schema_reference = Path(os.path.relpath(schema_path, output_path.parent)).as_posix()

    profile = {
        "$schema": schema_reference,
        "schemaVersion": 1,
        "id": profile_id,
        "displayName": args.display_name or f"{product_name} Device Logs",
        "defaultPort": args.port,
        "apps": {
            "android": app_platform(android_id, f"{product_name} Android"),
            "ios": app_platform(ios_id, f"{product_name} iOS"),
        },
        "analytics": {
            "enabled": analytics_enabled,
            "marker": marker,
            "parser": parser_name,
            "platforms": platforms,
        },
    }
    rendered = json.dumps(profile, ensure_ascii=False, indent=2) + "\n"

    print(f"Unity 工程：{unity_root}", file=sys.stderr)
    print(f"Android App ID：{android_id or '未配置'}", file=sys.stderr)
    print(f"iOS Bundle ID：{ios_id or '未配置'}", file=sys.stderr)
    if analytics_enabled:
        print(f"埋点：{marker} / {parser_name}（命中 {len(matched_files)} 个文件）", file=sys.stderr)
    else:
        print("埋点：未启用或未识别", file=sys.stderr)

    if args.dry_run:
        sys.stdout.write(rendered)
        return 0
    if output_path.exists() and not args.force:
        print(f"错误：输出文件已存在，不会覆盖：{output_path}\n如已明确确认覆盖，请加 --force。", file=sys.stderr)
        return 3
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")
    print(f"已生成：{output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
