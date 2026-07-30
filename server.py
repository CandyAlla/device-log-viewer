#!/usr/bin/env python3
"""Local-only web server that streams Android and iOS logs to a browser."""

from __future__ import annotations

import argparse
import errno
import html
import ipaddress
import json
import os
import plistlib
import queue
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


APP_DIR = Path(__file__).resolve().parent
INDEX_FILE = APP_DIR / "index.html"
DEFAULT_PROFILE_FILE = APP_DIR / "profiles" / "default.json"
VALID_SOURCES = {"android", "ios-simulator", "ios-device"}
MAX_APK_SIZE_BYTES = 1024 * 1024 * 1024
MAX_IPA_SIZE_BYTES = 2 * 1024 * 1024 * 1024
MAX_IPA_UNCOMPRESSED_BYTES = 5 * 1024 * 1024 * 1024
MAX_REMOTE_METADATA_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
REMOTE_TIMEOUT_SECONDS = 30
IOS_INSTALL_TIMEOUT_SECONDS = 10 * 60
IOS_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1"
)
DNS_PROXY_FAKE_IP_NETWORKS = (ipaddress.ip_network("198.18.0.0/15"),)
ANDROID_THREADTIME_LINE = re.compile(
    r"^\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+\s+(\d+)\s+\d+\s+[VDIWEF]\s+"
)
TOOL_ID = "device-log-viewer"
TOOL_VERSION = "1.0.0"
PROFILE_SCHEMA_VERSION = 1
ANALYTICS_PARSERS = {"plain", "gamefoundation-eventlog"}


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _profile_app_id(value: Any, label: str) -> str:
    app_id = str(value or "").strip()
    if app_id:
        validate_app_id(app_id, label)
    return app_id


def _profile_apps(value: Any, platform: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Profile apps.{platform} 必须是对象。")
    default_id = _profile_app_id(value.get("default", ""), f"apps.{platform}.default")
    raw_presets = value.get("presets", [])
    if not isinstance(raw_presets, list):
        raise ValueError(f"Profile apps.{platform}.presets 必须是数组。")

    presets: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw_preset in enumerate(raw_presets):
        if isinstance(raw_preset, str):
            app_id = _profile_app_id(raw_preset, f"apps.{platform}.presets[{index}]")
            label = app_id
        elif isinstance(raw_preset, dict):
            app_id = _profile_app_id(raw_preset.get("id", ""), f"apps.{platform}.presets[{index}].id")
            label = str(raw_preset.get("label", app_id)).strip()
        else:
            raise ValueError(f"Profile apps.{platform}.presets[{index}] 必须是字符串或对象。")
        if not app_id or app_id in seen:
            continue
        if not label or len(label) > 120:
            raise ValueError(f"Profile apps.{platform}.presets[{index}].label 无效。")
        seen.add(app_id)
        presets.append({"id": app_id, "label": label})

    if default_id and default_id not in seen:
        presets.insert(0, {"id": default_id, "label": default_id})
    return {"default": default_id, "presets": presets}


def load_profile(value: str | Path) -> dict[str, Any]:
    profile_path = Path(value).expanduser()
    if not profile_path.is_absolute():
        profile_path = Path.cwd() / profile_path
    profile_path = profile_path.resolve()
    try:
        raw = json.loads(profile_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Profile 不存在：{profile_path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 Profile {profile_path}：{exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Profile 根节点必须是 JSON 对象。")
    if raw.get("schemaVersion") != PROFILE_SCHEMA_VERSION:
        raise ValueError(f"Profile schemaVersion 必须为 {PROFILE_SCHEMA_VERSION}。")

    profile_id = str(raw.get("id", "")).strip()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?", profile_id):
        raise ValueError("Profile id 只能使用小写字母、数字、点、下划线和连字符（最多 64 字符）。")
    display_name = str(raw.get("displayName", "")).strip()
    if not display_name or len(display_name) > 80:
        raise ValueError("Profile displayName 必须为 1–80 个字符。")
    default_port = raw.get("defaultPort", 8765)
    if not isinstance(default_port, int) or isinstance(default_port, bool) or not 1024 <= default_port <= 65525:
        raise ValueError("Profile defaultPort 必须是 1024–65525 之间的整数。")

    raw_apps = raw.get("apps", {})
    if not isinstance(raw_apps, dict):
        raise ValueError("Profile apps 必须是对象。")
    apps = {
        "android": _profile_apps(raw_apps.get("android", {}), "android"),
        "ios": _profile_apps(raw_apps.get("ios", {}), "ios"),
    }

    raw_analytics = raw.get("analytics", {})
    if not isinstance(raw_analytics, dict):
        raise ValueError("Profile analytics 必须是对象。")
    analytics_enabled = raw_analytics.get("enabled", False)
    if not isinstance(analytics_enabled, bool):
        raise ValueError("Profile analytics.enabled 必须是布尔值。")
    marker = str(raw_analytics.get("marker", "")).strip()
    if analytics_enabled and (not marker or len(marker) > 120):
        raise ValueError("启用埋点筛选时，analytics.marker 必须为 1–120 个字符。")
    parser_name = str(raw_analytics.get("parser", "plain")).strip()
    if parser_name not in ANALYTICS_PARSERS:
        raise ValueError(f"Profile analytics.parser 仅支持：{', '.join(sorted(ANALYTICS_PARSERS))}。")
    raw_platforms = raw_analytics.get("platforms", [])
    if not isinstance(raw_platforms, list) or not all(isinstance(item, str) for item in raw_platforms):
        raise ValueError("Profile analytics.platforms 必须是字符串数组。")
    platforms = []
    for raw_platform in raw_platforms:
        platform = raw_platform.strip()
        if platform and platform not in platforms:
            if len(platform) > 60:
                raise ValueError("Profile analytics.platforms 中的名称不能超过 60 个字符。")
            platforms.append(platform)

    return {
        "schemaVersion": PROFILE_SCHEMA_VERSION,
        "id": profile_id,
        "displayName": display_name,
        "defaultPort": default_port,
        "apps": apps,
        "analytics": {
            "enabled": analytics_enabled,
            "marker": marker,
            "parser": parser_name,
            "platforms": platforms,
        },
        "_path": str(profile_path),
    }


def public_profile(profile: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in profile.items() if not key.startswith("_")}


def find_adb(explicit_path: str | None = None) -> str | None:
    if explicit_path:
        candidate = Path(explicit_path).expanduser()
        return str(candidate.resolve()) if candidate.is_file() and os.access(candidate, os.X_OK) else None

    path_match = shutil.which("adb")
    if path_match:
        return path_match

    candidates: list[Path] = []
    for variable_name in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        sdk_root = os.environ.get(variable_name)
        if sdk_root:
            candidates.append(Path(sdk_root).expanduser() / "platform-tools" / "adb")

    candidates.extend(
        [
            Path.home() / "Library" / "Android" / "sdk" / "platform-tools" / "adb",
            Path("/opt/homebrew/bin/adb"),
            Path("/usr/local/bin/adb"),
        ]
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    return None


def find_xcrun() -> str | None:
    candidate = shutil.which("xcrun")
    if candidate:
        return candidate
    fallback = Path("/usr/bin/xcrun")
    return str(fallback) if fallback.is_file() and os.access(fallback, os.X_OK) else None


def validate_source(source: str) -> str:
    source = source.strip()
    if source not in VALID_SOURCES:
        raise ApiError(HTTPStatus.BAD_REQUEST, "日志来源无效。")
    return source


def validate_app_id(app_id: str, label: str = "App 标识") -> str:
    app_id = app_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_-]+)+", app_id):
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{label}格式无效。")
    return app_id


def _validate_remote_url(value: str) -> str:
    url = value.strip()
    if not url or len(url) > 4096 or any(ord(char) < 32 for char in url):
        raise ApiError(HTTPStatus.BAD_REQUEST, "iOS 分发链接无效。")
    parsed = urlparse(url)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ApiError(HTTPStatus.BAD_REQUEST, "分发链接必须是公网 HTTP 或 HTTPS 地址。")
    if parsed.username or parsed.password:
        raise ApiError(HTTPStatus.BAD_REQUEST, "分发链接不能包含账号或密码。")
    try:
        port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
    except ValueError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, "分发链接端口无效。") from exc
    if port not in {80, 443}:
        raise ApiError(HTTPStatus.BAD_REQUEST, "分发链接只能使用标准 HTTP/HTTPS 端口。")

    hostname = parsed.hostname.rstrip(".").casefold()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        raise ApiError(HTTPStatus.BAD_REQUEST, "分发链接不能指向本机或局域网地址。")
    try:
        literal_address = ipaddress.ip_address(hostname)
    except ValueError:
        literal_address = None
    if literal_address is not None and not literal_address.is_global:
        raise ApiError(HTTPStatus.BAD_REQUEST, "分发链接不能直接使用本机、局域网或保留 IP。")
    try:
        addresses = {
            record[4][0].split("%", 1)[0]
            for record in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise ApiError(HTTPStatus.BAD_GATEWAY, f"无法解析分发服务器：{hostname}") from exc
    if not addresses:
        raise ApiError(HTTPStatus.BAD_GATEWAY, f"分发服务器没有可用地址：{hostname}")
    for address_text in addresses:
        try:
            address = ipaddress.ip_address(address_text)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_GATEWAY, "分发服务器返回了无效网络地址。") from exc
        allowed_proxy_fake_ip = literal_address is None and any(
            address in network for network in DNS_PROXY_FAKE_IP_NETWORKS
        )
        if not address.is_global and not allowed_proxy_fake_ip:
            raise ApiError(HTTPStatus.BAD_REQUEST, "分发链接不能指向本机、局域网或保留地址。")
    return url


class _SafeRedirectHandler(HTTPRedirectHandler):
    max_redirections = 8

    def redirect_request(
        self,
        request: Request,
        response: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> Request | None:
        safe_url = _validate_remote_url(urljoin(request.full_url, new_url))
        return super().redirect_request(request, response, code, message, headers, safe_url)


def _open_remote_url(url: str, headers: dict[str, str] | None = None) -> Any:
    safe_url = _validate_remote_url(url)
    request_headers = {
        "User-Agent": IOS_USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    }
    if headers:
        request_headers.update(headers)
    request = Request(safe_url, headers=request_headers)
    try:
        return build_opener(_SafeRedirectHandler()).open(request, timeout=REMOTE_TIMEOUT_SECONDS)
    except HTTPError as exc:
        if exc.code in {401, 403}:
            message = "分发链接需要登录、密码或有效的下载权限。"
        elif exc.code == 404:
            message = "分发链接或安装包不存在。"
        else:
            message = f"分发服务器返回 HTTP {exc.code}。"
        raise ApiError(HTTPStatus.BAD_GATEWAY, message) from exc
    except (URLError, TimeoutError, socket.timeout, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ApiError(HTTPStatus.BAD_GATEWAY, f"无法访问分发服务器：{reason}") from exc


def _read_remote_body(response: Any, limit: int, label: str) -> bytes:
    try:
        content_length = int(response.headers.get("Content-Length", "0") or "0")
    except ValueError:
        content_length = 0
    if content_length > limit:
        raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"{label}内容过大。")
    data = response.read(limit + 1)
    if len(data) > limit:
        raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"{label}内容过大。")
    return data


def _manifest_url_from_itms(link: str) -> str:
    parsed = urlparse(link.strip())
    if parsed.scheme.casefold() != "itms-services":
        raise ApiError(HTTPStatus.BAD_REQUEST, "iOS 安装清单链接无效。")
    manifest_url = parse_qs(parsed.query).get("url", [""])[0]
    if not manifest_url:
        raise ApiError(HTTPStatus.BAD_REQUEST, "iOS 安装链接中缺少 manifest 地址。")
    return _validate_remote_url(manifest_url)


def _parse_ios_manifest(data: bytes) -> dict[str, Any]:
    try:
        payload = plistlib.loads(data)
    except Exception as exc:
        raise ApiError(HTTPStatus.BAD_GATEWAY, "分发服务器返回的 iOS 安装清单无法识别。") from exc
    if not isinstance(payload, dict):
        raise ApiError(HTTPStatus.BAD_GATEWAY, "iOS 安装清单格式无效。")

    items = payload.get("items")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        raise ApiError(HTTPStatus.BAD_GATEWAY, "iOS 安装清单中没有 App。")
    item = items[0]
    assets = item.get("assets")
    if not isinstance(assets, list):
        raise ApiError(HTTPStatus.BAD_GATEWAY, "iOS 安装清单中没有 IPA 下载地址。")
    ipa_url = ""
    for asset in assets:
        if isinstance(asset, dict) and asset.get("kind") == "software-package":
            ipa_url = str(asset.get("url", "")).strip()
            break
    if not ipa_url:
        raise ApiError(HTTPStatus.BAD_GATEWAY, "iOS 安装清单中没有 IPA 下载地址。")

    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    bundle_id = str(metadata.get("bundle-identifier", "")).strip()
    if bundle_id:
        validate_app_id(bundle_id, "安装清单 Bundle ID")
    return {
        "ipaUrl": _validate_remote_url(ipa_url),
        "name": str(metadata.get("title", "iOS App")).strip() or "iOS App",
        "bundleId": bundle_id,
        "version": str(metadata.get("bundle-version", "")).strip(),
    }


def _fetch_ios_manifest(manifest_url: str) -> dict[str, Any]:
    response = _open_remote_url(manifest_url, {"Accept": "application/x-plist, application/xml, text/xml"})
    try:
        return _parse_ios_manifest(_read_remote_body(response, MAX_MANIFEST_BYTES, "iOS 安装清单"))
    finally:
        response.close()


def _resolve_fir_release(page_url: str, expected_type: str) -> dict[str, Any]:
    parsed = urlparse(page_url)
    short_name = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{2,80}", short_name):
        raise ApiError(HTTPStatus.BAD_REQUEST, "无法从 FIR 链接中识别应用短地址。")
    release_id = parse_qs(parsed.query).get("release_id", [""])[0].strip()
    if release_id and not re.fullmatch(r"[A-Fa-f0-9]{24}", release_id):
        raise ApiError(HTTPStatus.BAD_REQUEST, "FIR release_id 格式无效。")

    query = {
        "referer": parsed.hostname or "",
        "visit_https": "1" if parsed.scheme.casefold() == "https" else "0",
    }
    if release_id:
        query["release_id"] = release_id
    query_url = f"https://download.appmeta.cn/{short_name}?{urlencode(query)}"
    response = _open_remote_url(
        query_url,
        {
            "Accept": "application/json",
            "Referer": page_url,
        },
    )
    try:
        raw_data = _read_remote_body(response, MAX_REMOTE_METADATA_BYTES, "FIR 应用信息")
    finally:
        response.close()
    try:
        payload = json.loads(raw_data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError(HTTPStatus.BAD_GATEWAY, "FIR 返回的应用信息无法识别。") from exc
    if not isinstance(payload, dict):
        raise ApiError(HTTPStatus.BAD_GATEWAY, "FIR 返回的应用信息格式无效。")
    if payload.get("need_pow"):
        raise ApiError(HTTPStatus.BAD_GATEWAY, "这个 FIR 链接需要浏览器验证，暂时无法自动安装。")

    app = payload.get("app")
    actual_type = str(app.get("type", "")).casefold() if isinstance(app, dict) else ""
    if not isinstance(app, dict) or actual_type != expected_type:
        platform_name = "Android" if expected_type == "android" else "iOS"
        raise ApiError(HTTPStatus.BAD_REQUEST, f"这个分发链接不是 {platform_name} App。")
    app_id = str(app.get("id", "")).strip()
    download_token = str(app.get("token", "")).strip()
    app_short = str(app.get("short", short_name)).strip()
    if not re.fullmatch(r"[A-Za-z0-9]{6,80}", app_id) or not download_token:
        raise ApiError(HTTPStatus.BAD_GATEWAY, "FIR 未提供有效的 App 下载凭证。")
    if not re.fullmatch(r"[A-Za-z0-9_-]{2,80}", app_short):
        raise ApiError(HTTPStatus.BAD_GATEWAY, "FIR 返回的应用短地址无效。")

    release = app.get("releases") if isinstance(app.get("releases"), dict) else app.get("master")
    release = release if isinstance(release, dict) else {}
    selected_release_id = release_id or str(release.get("id", "")).strip()
    if selected_release_id and not re.fullmatch(r"[A-Fa-f0-9]{24}", selected_release_id):
        raise ApiError(HTTPStatus.BAD_GATEWAY, "FIR 返回的发布版本标识无效。")
    if release.get("is_expired"):
        raise ApiError(HTTPStatus.GONE, "这个 FIR 发布版本已经过期。")

    install_query = {
        "short": app_short,
        "download_token": download_token,
    }
    if selected_release_id:
        install_query["release_id"] = selected_release_id
    install_url = f"https://download.appmeta.cn/apps/{app_id}/install?{urlencode(install_query)}"
    return {
        "installUrl": _validate_remote_url(install_url),
        "name": str(app.get("name", f"{expected_type.title()} App")).strip() or f"{expected_type.title()} App",
        "version": str(release.get("version", "")).strip(),
        "build": str(release.get("build", "")).strip(),
        "expectedSize": int(release.get("fsize", 0) or 0),
        "provider": "FIR",
    }


def _resolve_fir_link(page_url: str) -> dict[str, Any]:
    release = _resolve_fir_release(page_url, "ios")
    resolved = _fetch_ios_manifest(str(release["installUrl"]))
    resolved.update(
        {
            "name": release["name"] or resolved.get("name", "iOS App"),
            "version": release["version"] or resolved.get("version", ""),
            "build": release["build"],
            "expectedSize": release["expectedSize"],
            "provider": release["provider"],
        }
    )
    return resolved


def resolve_ios_install_link(value: str) -> dict[str, Any]:
    link = value.strip()
    if urlparse(link).scheme.casefold() == "itms-services":
        resolved = _fetch_ios_manifest(_manifest_url_from_itms(link))
        resolved["provider"] = "iOS manifest"
        return resolved

    page_url = _validate_remote_url(link)
    response = _open_remote_url(page_url, {"Accept": "text/html, application/x-plist, application/octet-stream"})
    try:
        final_url = response.geturl()
        content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].strip().casefold()
        disposition = str(response.headers.get("Content-Disposition", "")).casefold()
        if content_type in {"application/x-plist", "application/xml", "text/xml"}:
            resolved = _parse_ios_manifest(_read_remote_body(response, MAX_MANIFEST_BYTES, "iOS 安装清单"))
            resolved["provider"] = "iOS manifest"
            return resolved
        if urlparse(final_url).path.casefold().endswith(".ipa") or ".ipa" in disposition:
            return {"ipaUrl": _validate_remote_url(final_url), "name": "iOS App", "bundleId": "", "version": "", "provider": "direct"}
        page_data = _read_remote_body(response, MAX_REMOTE_METADATA_BYTES, "分发页面")
        charset = response.headers.get_content_charset() or "utf-8"
    finally:
        response.close()

    page_text = html.unescape(page_data.decode(charset, errors="replace"))
    itms_match = re.search(r"itms-services://[^\s\"'<>]+", page_text, flags=re.IGNORECASE)
    if itms_match:
        resolved = _fetch_ios_manifest(_manifest_url_from_itms(itms_match.group(0)))
        resolved["provider"] = "iOS manifest"
        return resolved
    if not any(marker in page_text for marker in ("static-fir.appmeta.cn", "FIR.install", "fir.im")):
        raise ApiError(HTTPStatus.BAD_REQUEST, "暂时无法从这个页面识别 iOS 安装包，请使用 FIR 分发链接。")
    return _resolve_fir_link(page_url)


def resolve_android_install_link(value: str) -> dict[str, Any]:
    page_url = _validate_remote_url(value)
    response = _open_remote_url(
        page_url,
        {"Accept": "text/html, application/vnd.android.package-archive, application/octet-stream"},
    )
    try:
        final_url = response.geturl()
        content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].strip().casefold()
        disposition = str(response.headers.get("Content-Disposition", "")).casefold()
        try:
            content_length = int(response.headers.get("Content-Length", "0") or "0")
        except ValueError:
            content_length = 0
        if (
            content_type == "application/vnd.android.package-archive"
            or urlparse(final_url).path.casefold().endswith(".apk")
            or ".apk" in disposition
        ):
            return {
                "apkUrl": _validate_remote_url(final_url),
                "name": "Android App",
                "version": "",
                "build": "",
                "expectedSize": content_length,
                "provider": "direct",
            }
        page_data = _read_remote_body(response, MAX_REMOTE_METADATA_BYTES, "分发页面")
        charset = response.headers.get_content_charset() or "utf-8"
    finally:
        response.close()

    page_text = html.unescape(page_data.decode(charset, errors="replace"))
    if not any(marker in page_text for marker in ("static-fir.appmeta.cn", "FIR.install", "fir.im")):
        raise ApiError(HTTPStatus.BAD_REQUEST, "暂时无法从这个页面识别 Android APK，请使用 FIR 分发链接或直接 APK 地址。")
    release = _resolve_fir_release(page_url, "android")
    release["apkUrl"] = release.pop("installUrl")
    return release


def _download_remote_package(
    url: str,
    destination: Path,
    max_size: int,
    package_label: str,
    max_size_label: str,
) -> int:
    response = _open_remote_url(
        url,
        {"Accept": "application/octet-stream, application/zip, application/vnd.android.package-archive"},
    )
    size = 0
    try:
        try:
            content_length = int(response.headers.get("Content-Length", "0") or "0")
        except ValueError:
            content_length = 0
        if content_length > max_size:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"{package_label} 文件超过 {max_size_label}，无法下载。")
        with destination.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_size:
                    raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"{package_label} 文件超过 {max_size_label}，已停止下载。")
                output.write(chunk)
    except (URLError, TimeoutError, socket.timeout, OSError) as exc:
        raise ApiError(HTTPStatus.BAD_GATEWAY, f"{package_label} 下载失败：{getattr(exc, 'reason', exc)}") from exc
    finally:
        response.close()
    if not size:
        raise ApiError(HTTPStatus.BAD_GATEWAY, f"下载到的 {package_label} 文件为空。")
    return size


def _download_ipa(url: str, destination: Path) -> int:
    return _download_remote_package(url, destination, MAX_IPA_SIZE_BYTES, "IPA", "2 GB")


def _download_apk(url: str, destination: Path) -> int:
    return _download_remote_package(url, destination, MAX_APK_SIZE_BYTES, "APK", "1 GB")


def _validate_apk_file(apk_path: Path, status: int = HTTPStatus.BAD_REQUEST) -> None:
    try:
        with zipfile.ZipFile(apk_path) as apk_archive:
            manifest = apk_archive.getinfo("AndroidManifest.xml")
            if manifest.file_size <= 0:
                raise KeyError("empty AndroidManifest.xml")
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise ApiError(status, "文件不是有效的 Android APK。") from exc


def _extract_ios_app(ipa_path: Path, destination: Path) -> tuple[Path, dict[str, str]]:
    try:
        archive = zipfile.ZipFile(ipa_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ApiError(HTTPStatus.BAD_GATEWAY, "下载内容不是有效的 IPA 文件。") from exc
    with archive:
        top_level_plists = []
        for info in archive.infolist():
            parts = PurePosixPath(info.filename).parts
            if len(parts) == 3 and parts[0] == "Payload" and parts[1].casefold().endswith(".app") and parts[2] == "Info.plist":
                top_level_plists.append(info)
        if len(top_level_plists) != 1:
            raise ApiError(HTTPStatus.BAD_GATEWAY, "IPA 中没有唯一的 Payload/*.app。")

        info_plist_entry = top_level_plists[0]
        app_directory = PurePosixPath(info_plist_entry.filename).parent
        app_prefix = f"{app_directory.as_posix()}/"
        try:
            app_info = plistlib.loads(archive.read(info_plist_entry))
        except Exception as exc:
            raise ApiError(HTTPStatus.BAD_GATEWAY, "IPA 中的 App Info.plist 无法识别。") from exc
        if not isinstance(app_info, dict):
            raise ApiError(HTTPStatus.BAD_GATEWAY, "IPA 中的 App 信息格式无效。")

        bundle_id = validate_app_id(str(app_info.get("CFBundleIdentifier", "")), "IPA Bundle ID")
        metadata = {
            "name": str(app_info.get("CFBundleDisplayName") or app_info.get("CFBundleName") or app_directory.stem).strip(),
            "bundleId": bundle_id,
            "version": str(app_info.get("CFBundleShortVersionString", "")).strip(),
            "build": str(app_info.get("CFBundleVersion", "")).strip(),
        }

        selected_entries: list[zipfile.ZipInfo] = []
        extracted_paths: set[str] = set()
        uncompressed_size = 0
        for info in archive.infolist():
            if not (info.filename == app_directory.as_posix() or info.filename.startswith(app_prefix)):
                continue
            if "\\" in info.filename or info.filename.startswith("/"):
                raise ApiError(HTTPStatus.BAD_GATEWAY, "IPA 内含不安全的文件路径。")
            parts = PurePosixPath(info.filename).parts
            if not parts or any(part in {"", ".", ".."} for part in parts):
                raise ApiError(HTTPStatus.BAD_GATEWAY, "IPA 内含不安全的文件路径。")
            normalized = PurePosixPath(*parts).as_posix()
            if normalized in extracted_paths:
                raise ApiError(HTTPStatus.BAD_GATEWAY, "IPA 内含重复文件路径。")
            extracted_paths.add(normalized)
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise ApiError(HTTPStatus.BAD_GATEWAY, "IPA 内含不受支持的符号链接。")
            uncompressed_size += info.file_size
            if uncompressed_size > MAX_IPA_UNCOMPRESSED_BYTES:
                raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "IPA 解压后超过 5 GB，已停止处理。")
            selected_entries.append(info)

        for info in selected_entries:
            relative_path = PurePosixPath(info.filename)
            target = destination.joinpath(*relative_path.parts)
            if info.is_dir() or info.filename.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            mode = (info.external_attr >> 16) & 0o777
            if mode:
                target.chmod(mode)

    app_path = destination.joinpath(*app_directory.parts)
    if not app_path.is_dir():
        raise ApiError(HTTPStatus.BAD_GATEWAY, "IPA 解压后没有找到 App。")
    return app_path, metadata


def _check_mobile_provision(app_path: Path, device_ids: set[str]) -> None:
    profile_path = app_path / "embedded.mobileprovision"
    if not profile_path.is_file() or not Path("/usr/bin/security").is_file():
        return
    try:
        result = subprocess.run(
            ["/usr/bin/security", "cms", "-D", "-i", str(profile_path)],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return
    if result.returncode != 0:
        return
    try:
        profile = plistlib.loads(result.stdout)
    except Exception:
        return
    provisioned_devices = profile.get("ProvisionedDevices") if isinstance(profile, dict) else None
    if isinstance(provisioned_devices, list) and provisioned_devices:
        allowed = {str(value).strip() for value in provisioned_devices}
        if device_ids.isdisjoint(allowed):
            raise ApiError(HTTPStatus.CONFLICT, "这个 Ad Hoc 安装包未包含所选 iPhone/iPad 的 UDID。")


def list_devices(adb_path: str | None) -> list[dict[str, str]]:
    if not adb_path:
        raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "未找到 adb，请先安装 Android Platform Tools。")

    try:
        result = subprocess.run(
            [adb_path, "devices", "-l"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, f"无法执行 adb：{exc}") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"退出码 {result.returncode}"
        raise ApiError(HTTPStatus.BAD_GATEWAY, f"adb devices 执行失败：{detail}")

    devices: list[dict[str, str]] = []
    for raw_line in result.stdout.splitlines()[1:]:
        line = raw_line.strip()
        if not line or line.startswith("*"):
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        serial, state = fields[0], fields[1]
        metadata: dict[str, str] = {}
        for field in fields[2:]:
            if ":" in field:
                key, value = field.split(":", 1)
                metadata[key] = value
        devices.append(
            {
                "serial": serial,
                "state": state,
                "model": metadata.get("model", ""),
                "product": metadata.get("product", ""),
                "transportId": metadata.get("transport_id", ""),
                "name": metadata.get("model", "").replace("_", " ") or serial,
                "detail": metadata.get("product", ""),
                "statusLabel": "已连接" if state == "device" else state,
                "available": state == "device",
            }
        )
    return devices


def list_packages(adb_path: str | None, serial: str) -> list[str]:
    if not adb_path:
        raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "未找到 adb，请先安装 Android Platform Tools。")

    serial = serial.strip()
    if not serial:
        raise ApiError(HTTPStatus.BAD_REQUEST, "请先选择设备。")

    try:
        result = subprocess.run(
            [adb_path, "-s", serial, "shell", "pm", "list", "packages", "-3"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ApiError(HTTPStatus.BAD_GATEWAY, f"读取 App 列表失败：{exc}") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"退出码 {result.returncode}"
        raise ApiError(HTTPStatus.BAD_GATEWAY, f"读取 App 列表失败：{detail}")

    packages: set[str] = set()
    for raw_line in result.stdout.splitlines():
        package_name = raw_line.removeprefix("package:").strip()
        if re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", package_name):
            packages.add(package_name)
    return sorted(packages, key=str.casefold)


def list_ios_simulators(xcrun_path: str | None) -> list[dict[str, Any]]:
    if not xcrun_path:
        raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "未找到 xcrun，请先安装并启动 Xcode。")
    try:
        result = subprocess.run(
            [xcrun_path, "simctl", "list", "devices", "available", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ApiError(HTTPStatus.BAD_GATEWAY, f"读取 iOS 模拟器失败：{exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"退出码 {result.returncode}"
        raise ApiError(HTTPStatus.BAD_GATEWAY, f"读取 iOS 模拟器失败：{detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ApiError(HTTPStatus.BAD_GATEWAY, "iOS 模拟器返回了无法识别的数据。") from exc

    devices: list[dict[str, Any]] = []
    for runtime_id, runtime_devices in payload.get("devices", {}).items():
        if "ios-" not in runtime_id.casefold() or not isinstance(runtime_devices, list):
            continue
        runtime_version = runtime_id.rsplit("iOS-", 1)[-1].replace("-", ".")
        for device in runtime_devices:
            if not isinstance(device, dict) or not device.get("isAvailable", True):
                continue
            udid = str(device.get("udid", "")).strip()
            if not udid:
                continue
            state = str(device.get("state", "Unknown"))
            devices.append(
                {
                    "serial": udid,
                    "state": state,
                    "name": str(device.get("name", "iOS Simulator")),
                    "detail": f"iOS {runtime_version}",
                    "statusLabel": "已启动" if state == "Booted" else "未启动",
                    "available": state == "Booted",
                }
            )
    return sorted(devices, key=lambda item: (not item["available"], item["name"].casefold()))


def _run_devicectl_json(
    xcrun_path: str | None,
    arguments: list[str],
    error_prefix: str,
    timeout: int = 20,
) -> dict[str, Any]:
    if not xcrun_path:
        raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "未找到 xcrun，请先安装并启动 Xcode。")
    with tempfile.TemporaryDirectory(prefix="device-log-viewer-") as temp_dir:
        output_path = Path(temp_dir) / "result.json"
        command = [
            xcrun_path,
            "devicectl",
            "--quiet",
            "--timeout",
            str(timeout),
            "--json-output",
            str(output_path),
            *arguments,
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout + 5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ApiError(HTTPStatus.BAD_GATEWAY, f"{error_prefix}：{exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or f"退出码 {result.returncode}"
            raise ApiError(HTTPStatus.BAD_GATEWAY, f"{error_prefix}：{detail}")
        try:
            return json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ApiError(HTTPStatus.BAD_GATEWAY, f"{error_prefix}：返回数据无法识别。") from exc


def _devicectl_records(payload: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    result = payload.get("result", payload)
    if not isinstance(result, dict):
        return []
    for key in keys:
        value = result.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [item for item in value.values() if isinstance(item, dict)]
    return []


def list_ios_devices(xcrun_path: str | None) -> list[dict[str, Any]]:
    payload = _run_devicectl_json(xcrun_path, ["list", "devices"], "读取 iPhone/iPad 失败")
    devices: list[dict[str, Any]] = []
    for device in _devicectl_records(payload, "devices"):
        hardware = device.get("hardwareProperties", {})
        properties = device.get("deviceProperties", {})
        connection = device.get("connectionProperties", {})
        platform = str(hardware.get("platform", ""))
        device_type = str(hardware.get("deviceType", ""))
        if "ios" not in platform.casefold() and device_type.casefold() not in {"iphone", "ipad"}:
            continue
        identifier = str(device.get("identifier") or hardware.get("udid") or "").strip()
        if not identifier:
            continue
        pairing_state = str(connection.get("pairingState", "")).casefold()
        boot_state = str(properties.get("bootState", "")).casefold()
        available = pairing_state != "unpaired" and boot_state not in {"shutdown", "disconnected"}
        os_version = str(properties.get("osVersionNumber") or properties.get("osVersion") or "").strip()
        model = str(hardware.get("marketingName") or hardware.get("productType") or device_type or "iOS Device")
        name = str(properties.get("name") or model)
        devices.append(
            {
                "serial": identifier,
                "udid": str(hardware.get("udid") or "").strip(),
                "state": "device" if available else "disconnected",
                "name": name,
                "detail": f"{model}{f' · iOS {os_version}' if os_version else ''}",
                "statusLabel": "已连接" if available else "未连接",
                "available": available,
            }
        )
    return sorted(devices, key=lambda item: (not item["available"], item["name"].casefold()))


def _app_option(app_id: str, name: str = "") -> dict[str, str]:
    clean_name = name.strip()
    return {
        "id": app_id,
        "name": clean_name,
        "label": f"{clean_name} · {app_id}" if clean_name and clean_name != app_id else app_id,
    }


def list_simulator_apps(xcrun_path: str | None, serial: str) -> list[dict[str, str]]:
    if not xcrun_path:
        raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "未找到 xcrun，请先安装并启动 Xcode。")
    serial = serial.strip()
    if not serial:
        raise ApiError(HTTPStatus.BAD_REQUEST, "请先选择 iOS 模拟器。")
    try:
        result = subprocess.run(
            [xcrun_path, "simctl", "listapps", serial],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ApiError(HTTPStatus.BAD_GATEWAY, f"读取模拟器 App 列表失败：{exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"退出码 {result.returncode}"
        raise ApiError(HTTPStatus.BAD_GATEWAY, f"读取模拟器 App 列表失败：{detail}")
    try:
        converted = subprocess.run(
            ["/usr/bin/plutil", "-convert", "json", "-o", "-", "--", "-"],
            input=result.stdout,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ApiError(HTTPStatus.BAD_GATEWAY, f"解析模拟器 App 列表失败：{exc}") from exc
    if converted.returncode != 0:
        raise ApiError(HTTPStatus.BAD_GATEWAY, "解析模拟器 App 列表失败。")
    try:
        payload = json.loads(converted.stdout)
    except json.JSONDecodeError as exc:
        raise ApiError(HTTPStatus.BAD_GATEWAY, "模拟器 App 列表格式无法识别。") from exc

    apps: list[dict[str, str]] = []
    for key, info in payload.items():
        if not isinstance(info, dict):
            continue
        app_id = str(info.get("CFBundleIdentifier") or key).strip()
        application_type = str(info.get("ApplicationType", "")).casefold()
        bundle_path = str(info.get("Bundle", ""))
        if application_type and application_type != "user" and "/Containers/Bundle/Application/" not in bundle_path:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_-]+)+", app_id):
            continue
        name = str(
            info.get("CFBundleDisplayName")
            or info.get("CFBundleName")
            or info.get("CFBundleExecutable")
            or ""
        )
        apps.append(_app_option(app_id, name))
    return sorted(apps, key=lambda app: (app["name"] or app["id"]).casefold())


def _find_app_records(value: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if any(key in value for key in ("bundleIdentifier", "bundleID", "bundleId")):
            records.append(value)
        else:
            for nested in value.values():
                records.extend(_find_app_records(nested))
    elif isinstance(value, list):
        for nested in value:
            records.extend(_find_app_records(nested))
    return records


def list_ios_device_apps(xcrun_path: str | None, serial: str) -> list[dict[str, str]]:
    serial = serial.strip()
    if not serial:
        raise ApiError(HTTPStatus.BAD_REQUEST, "请先选择 iPhone 或 iPad。")
    payload = _run_devicectl_json(
        xcrun_path,
        ["device", "info", "apps", "--device", serial],
        "读取真机 App 列表失败",
        timeout=30,
    )
    apps_by_id: dict[str, dict[str, str]] = {}
    for app in _find_app_records(payload.get("result", payload)):
        app_id = str(app.get("bundleIdentifier") or app.get("bundleID") or app.get("bundleId") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_-]+)+", app_id):
            continue
        name = str(app.get("name") or app.get("displayName") or app.get("localizedName") or "")
        apps_by_id[app_id] = _app_option(app_id, name)
    return sorted(apps_by_id.values(), key=lambda app: (app["name"] or app["id"]).casefold())


def query_app_pid(adb_path: str, serial: str, package_name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", package_name):
        raise ApiError(HTTPStatus.BAD_REQUEST, "App 包名格式无效。")

    commands = (
        [adb_path, "-s", serial, "shell", "pidof", "-s", package_name],
        [adb_path, "-s", serial, "shell", "pidof", package_name],
    )
    for command in commands:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ApiError(HTTPStatus.BAD_GATEWAY, f"查询 App 进程失败：{exc}") from exc
        pids = [value for value in result.stdout.split() if value.isdigit()]
        if pids:
            return pids[0]
    return ""


def build_logcat_command(adb_path: str, serial: str) -> list[str]:
    return [adb_path, "-s", serial, "logcat", "-v", "threadtime"]


def android_logcat_pid(line: str) -> str:
    match = ANDROID_THREADTIME_LINE.match(line)
    return match.group(1) if match else ""


class LogcatManager:
    def __init__(self, adb_path: str | None, xcrun_path: str | None) -> None:
        self.adb_path = adb_path
        self.xcrun_path = xcrun_path
        self._lock = threading.RLock()
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._app_watcher: threading.Thread | None = None
        self._generation = 0
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()
        self._ios_install_lock = threading.Lock()
        self._state = "idle"
        self._source = "android"
        self._serial = ""
        self._app_id = ""
        self._pid = ""
        self._message = "请选择设备并开始读取"

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self._state,
                "source": self._source,
                "serial": self._serial,
                "appId": self._app_id,
                "packageName": self._app_id,
                "pid": self._pid,
                "message": self._message,
                "adbAvailable": bool(self.adb_path),
                "adbPath": self.adb_path or "",
                "xcrunAvailable": bool(self.xcrun_path),
                "xcrunPath": self.xcrun_path or "",
            }

    def refresh_tools(self, source: str = "android") -> dict[str, Any]:
        source = validate_source(source)
        adb_path = find_adb()
        xcrun_path = find_xcrun()
        with self._lock:
            self.adb_path = adb_path
            self.xcrun_path = xcrun_path
        self._publish_status()
        if source == "android" and not adb_path:
            raise ApiError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "仍未找到 adb，请确认 Android Platform Tools 已安装。",
            )
        if source != "android" and not xcrun_path:
            raise ApiError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "仍未找到 xcrun，请确认 Xcode 已安装并至少启动过一次。",
            )
        return self.status()

    def refresh_adb(self) -> dict[str, Any]:
        return self.refresh_tools("android")

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=4000)
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def _publish(self, item: dict[str, Any]) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(item)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                    subscriber.put_nowait(item)
                except (queue.Empty, queue.Full):
                    pass

    def _publish_status(self) -> None:
        self._publish({"type": "status", "status": self.status()})

    def start(self, source: str, serial: str, app_id: str = "") -> dict[str, Any]:
        source = validate_source(source)
        serial = serial.strip()
        if not serial or len(serial) > 200 or any(char.isspace() for char in serial):
            raise ApiError(HTTPStatus.BAD_REQUEST, "设备标识无效。")

        app_id = app_id.strip()
        pid = ""
        if source == "android":
            if not self.adb_path:
                raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "未找到 adb，请先安装 Android Platform Tools。")
            available = {device["serial"]: device for device in list_devices(self.adb_path)}
            device = available.get(serial)
            if not device:
                raise ApiError(HTTPStatus.NOT_FOUND, "Android 设备已断开，请刷新设备列表。")
            if not device.get("available"):
                raise ApiError(HTTPStatus.CONFLICT, f"设备当前状态为 {device['state']}，请先解锁并授权 USB 调试。")
            pid = query_app_pid(self.adb_path, serial, app_id) if app_id else ""
            command = build_logcat_command(self.adb_path, serial)
            if app_id and pid:
                message = f"仅采集 {app_id}（自动跟踪当前 PID {pid}）"
            elif app_id:
                message = f"{app_id} 尚未运行，等待 App 启动（日志连接保持中）"
            else:
                message = "正在读取全部 Android 日志"
        elif source == "ios-simulator":
            if not self.xcrun_path:
                raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "未找到 xcrun，请先安装并启动 Xcode。")
            available = {device["serial"]: device for device in list_ios_simulators(self.xcrun_path)}
            device = available.get(serial)
            if not device:
                raise ApiError(HTTPStatus.NOT_FOUND, "iOS 模拟器不存在，请刷新设备列表。")
            if not device.get("available"):
                raise ApiError(HTTPStatus.CONFLICT, "请先在 Simulator 中启动这个模拟器。")
            if app_id:
                validate_app_id(app_id, "Bundle ID")
                command = [
                    self.xcrun_path,
                    "simctl",
                    "launch",
                    "--console",
                    "--terminate-running-process",
                    serial,
                    app_id,
                ]
                message = f"正在读取 {app_id} 的模拟器控制台（已启动或重启 App）"
            else:
                command = [
                    self.xcrun_path,
                    "simctl",
                    "spawn",
                    serial,
                    "log",
                    "stream",
                    "--style",
                    "compact",
                    "--level",
                    "debug",
                ]
                message = "正在读取 iOS 模拟器全部系统日志"
        else:
            if not self.xcrun_path:
                raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "未找到 xcrun，请先安装并启动 Xcode。")
            if not app_id:
                raise ApiError(HTTPStatus.BAD_REQUEST, "iPhone/iPad 真机日志必须选择 App。")
            validate_app_id(app_id, "Bundle ID")
            available = {device["serial"]: device for device in list_ios_devices(self.xcrun_path)}
            device = available.get(serial)
            if not device:
                raise ApiError(HTTPStatus.NOT_FOUND, "iPhone/iPad 已断开，请刷新设备列表。")
            if not device.get("available"):
                raise ApiError(HTTPStatus.CONFLICT, "请解锁设备，并确认已开启开发者模式和信任这台 Mac。")
            command = [
                self.xcrun_path,
                "devicectl",
                "device",
                "process",
                "launch",
                "--console",
                "--terminate-existing",
                "--device",
                serial,
                app_id,
            ]
            message = f"正在读取 {app_id} 的真机控制台（已启动或重启 App）"

        self.stop(publish=False)
        with self._lock:
            self._generation += 1
            generation = self._generation
            try:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
            except OSError as exc:
                self._state = "error"
                self._source = source
                self._serial = serial
                self._message = f"启动日志读取失败：{exc}"
                self._publish_status()
                raise ApiError(HTTPStatus.BAD_GATEWAY, self._message) from exc

            self._process = process
            self._state = "running"
            self._source = source
            self._serial = serial
            self._app_id = app_id
            self._pid = pid
            self._message = message
            reader = threading.Thread(
                target=self._read_process,
                args=(process, generation, serial, source, app_id),
                name="device-log-reader",
                daemon=True,
            )
            self._reader = reader
            reader.start()
            if source == "android" and app_id:
                watcher = threading.Thread(
                    target=self._watch_android_app,
                    args=(generation, serial, app_id),
                    name="android-app-pid-watcher",
                    daemon=True,
                )
                self._app_watcher = watcher
                watcher.start()
            else:
                self._app_watcher = None
        self._publish_status()
        return self.status()

    def _watch_android_app(self, generation: int, serial: str, app_id: str) -> None:
        while True:
            with self._lock:
                if (
                    generation != self._generation
                    or self._state != "running"
                    or self._source != "android"
                    or self._app_id != app_id
                ):
                    return
                adb_path = self.adb_path
            if not adb_path:
                return

            try:
                current_pid = query_app_pid(adb_path, serial, app_id)
            except ApiError:
                should_publish = False
                with self._lock:
                    if generation != self._generation or self._state != "running":
                        return
                    retry_message = f"暂时无法查询 {app_id} 进程，保持连接并继续重试"
                    if self._message != retry_message:
                        self._message = retry_message
                        should_publish = True
                if should_publish:
                    self._publish_status()
                time.sleep(1)
                continue

            should_publish = False
            with self._lock:
                if generation != self._generation or self._state != "running":
                    return
                if current_pid != self._pid:
                    self._pid = current_pid
                    self._message = (
                        f"仅采集 {app_id}（已自动连接新 PID {current_pid}）"
                        if current_pid
                        else f"{app_id} 已退出，等待重新启动（日志连接保持中）"
                    )
                    should_publish = True
            if should_publish:
                self._publish_status()
            time.sleep(0.75)

    def _read_process(
        self,
        process: subprocess.Popen[str],
        generation: int,
        serial: str,
        source: str,
        app_id: str,
    ) -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                clean_line = line.rstrip("\r\n")
                if source == "android" and app_id:
                    line_pid = android_logcat_pid(clean_line)
                    if not line_pid:
                        continue
                    with self._lock:
                        if generation != self._generation:
                            return
                        current_pid = self._pid
                    if not current_pid or line_pid != current_pid:
                        continue
                self._publish({"type": "log", "line": clean_line})
        finally:
            return_code = process.wait()
            should_publish = False
            with self._lock:
                if generation == self._generation and process is self._process:
                    self._process = None
                    self._reader = None
                    self._app_watcher = None
                    self._state = "error" if return_code else "idle"
                    self._serial = serial
                    self._message = (
                        f"日志进程已退出（退出码 {return_code}）" if return_code else "日志读取已停止"
                    )
                    should_publish = True
            if should_publish:
                self._publish_status()

    def stop(self, publish: bool = True) -> dict[str, Any]:
        with self._lock:
            process = self._process
            source = self._source
            self._generation += 1
            self._process = None
            self._reader = None
            self._app_watcher = None
            self._state = "idle"
            self._pid = ""
            self._message = "日志读取已停止"

        if process and process.poll() is None:
            if source == "android":
                process.terminate()
            else:
                process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if publish:
            self._publish_status()
        return self.status()

    def clear_device(self, serial: str) -> None:
        if not self.adb_path:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "未找到 adb。")
        serial = serial.strip()
        if not serial:
            raise ApiError(HTTPStatus.BAD_REQUEST, "请先选择设备。")
        try:
            result = subprocess.run(
                [self.adb_path, "-s", serial, "logcat", "-c"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ApiError(HTTPStatus.BAD_GATEWAY, f"清空设备日志失败：{exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or f"退出码 {result.returncode}"
            raise ApiError(HTTPStatus.BAD_GATEWAY, f"清空设备日志失败：{detail}")

    def install_apk(self, serial: str, apk_path: Path) -> dict[str, str]:
        if not self.adb_path:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "未找到 adb。")
        with self._lock:
            if self._state == "running":
                raise ApiError(HTTPStatus.CONFLICT, "请先停止日志采集，再安装 APK。")
        serial = serial.strip()
        if not serial or len(serial) > 200 or any(char.isspace() for char in serial):
            raise ApiError(HTTPStatus.BAD_REQUEST, "Android 设备标识无效。")

        available = {device["serial"]: device for device in list_devices(self.adb_path)}
        device = available.get(serial)
        if not device:
            raise ApiError(HTTPStatus.NOT_FOUND, "Android 设备已断开，请刷新设备列表。")
        if not device.get("available"):
            raise ApiError(
                HTTPStatus.CONFLICT,
                f"设备当前状态为 {device['state']}，请先解锁并授权 USB 调试。",
            )

        try:
            result = subprocess.run(
                [self.adb_path, "-s", serial, "install", "-r", str(apk_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10 * 60,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ApiError(HTTPStatus.GATEWAY_TIMEOUT, "安装 APK 超时，请检查 USB 连接后重试。") from exc
        except OSError as exc:
            raise ApiError(HTTPStatus.BAD_GATEWAY, f"无法执行 adb install：{exc}") from exc

        output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
        if result.returncode != 0:
            detail = output or f"退出码 {result.returncode}"
            raise ApiError(HTTPStatus.BAD_GATEWAY, f"APK 安装失败：{detail}")
        return {"message": "APK 安装成功", "output": output or "Success"}

    def install_apk_link(self, serial: str, link: str) -> dict[str, Any]:
        if not self.adb_path:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "未找到 adb，请先安装 Android Platform Tools。")
        with self._lock:
            if self._state == "running":
                raise ApiError(HTTPStatus.CONFLICT, "请先停止日志采集，再通过链接安装 APK。")
        serial = serial.strip()
        if not serial or len(serial) > 200 or any(char.isspace() for char in serial):
            raise ApiError(HTTPStatus.BAD_REQUEST, "Android 设备标识无效。")
        link = link.strip()
        if not link:
            raise ApiError(HTTPStatus.BAD_REQUEST, "请粘贴 Android 分发链接。")

        available = {device["serial"]: device for device in list_devices(self.adb_path)}
        device = available.get(serial)
        if not device:
            raise ApiError(HTTPStatus.NOT_FOUND, "Android 设备已断开，请刷新设备列表。")
        if not device.get("available"):
            raise ApiError(
                HTTPStatus.CONFLICT,
                f"设备当前状态为 {device['state']}，请先解锁并授权 USB 调试。",
            )

        with tempfile.TemporaryDirectory(prefix="device-log-viewer-android-install-") as temp_dir:
            resolved = resolve_android_install_link(link)
            expected_size = int(resolved.get("expectedSize", 0) or 0)
            if expected_size > MAX_APK_SIZE_BYTES:
                raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "APK 文件超过 1 GB，无法下载。")
            apk_path = Path(temp_dir) / "package.apk"
            downloaded_size = _download_apk(str(resolved["apkUrl"]), apk_path)
            if expected_size and downloaded_size != expected_size:
                raise ApiError(HTTPStatus.BAD_GATEWAY, "APK 下载大小与 FIR 发布信息不一致，请重新尝试。")
            _validate_apk_file(apk_path, HTTPStatus.BAD_GATEWAY)
            result = self.install_apk(serial, apk_path)
            return {
                **result,
                "message": "APK 下载并安装成功",
                "app": {
                    "name": str(resolved.get("name", "Android App")),
                    "version": str(resolved.get("version", "")),
                    "build": str(resolved.get("build", "")),
                },
                "fileSize": downloaded_size,
                "provider": str(resolved.get("provider", "Android distribution")),
            }

    def install_ios_link(self, serial: str, link: str) -> dict[str, Any]:
        if not self.xcrun_path:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "未找到 xcrun，请先安装并启动 Xcode。")
        with self._lock:
            if self._state == "running":
                raise ApiError(HTTPStatus.CONFLICT, "请先停止日志采集，再安装 iOS App。")
        serial = serial.strip()
        if not serial or len(serial) > 200 or any(char.isspace() for char in serial):
            raise ApiError(HTTPStatus.BAD_REQUEST, "iPhone/iPad 设备标识无效。")
        link = link.strip()
        if not link:
            raise ApiError(HTTPStatus.BAD_REQUEST, "请粘贴 iOS 分发链接。")
        if not self._ios_install_lock.acquire(blocking=False):
            raise ApiError(HTTPStatus.CONFLICT, "另一个 iOS 安装任务正在进行，请稍后再试。")

        try:
            available = {device["serial"]: device for device in list_ios_devices(self.xcrun_path)}
            device = available.get(serial)
            if not device:
                raise ApiError(HTTPStatus.NOT_FOUND, "iPhone/iPad 已断开，请刷新设备列表。")
            if not device.get("available"):
                raise ApiError(HTTPStatus.CONFLICT, "请解锁设备，并确认已开启开发者模式和信任这台 Mac。")

            with tempfile.TemporaryDirectory(prefix="device-log-viewer-ios-install-") as temp_dir:
                temp_path = Path(temp_dir)
                resolved = resolve_ios_install_link(link)
                expected_size = int(resolved.get("expectedSize", 0) or 0)
                if expected_size > MAX_IPA_SIZE_BYTES:
                    raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "IPA 文件超过 2 GB，无法下载。")
                ipa_path = temp_path / "package.ipa"
                downloaded_size = _download_ipa(str(resolved["ipaUrl"]), ipa_path)
                if expected_size and downloaded_size != expected_size:
                    raise ApiError(HTTPStatus.BAD_GATEWAY, "IPA 下载大小与 FIR 发布信息不一致，请重新尝试。")

                extract_path = temp_path / "extracted"
                app_path, app = _extract_ios_app(ipa_path, extract_path)
                expected_bundle_id = str(resolved.get("bundleId", "")).strip()
                if expected_bundle_id and expected_bundle_id != app["bundleId"]:
                    raise ApiError(HTTPStatus.BAD_GATEWAY, "IPA Bundle ID 与安装清单不一致，已停止安装。")
                _check_mobile_provision(
                    app_path,
                    {value for value in (serial, str(device.get("udid", "")).strip()) if value},
                )

                _run_devicectl_json(
                    self.xcrun_path,
                    ["device", "install", "app", "--device", serial, str(app_path)],
                    "iOS App 安装失败",
                    timeout=IOS_INSTALL_TIMEOUT_SECONDS,
                )
                return {
                    "message": "iOS App 安装成功",
                    "app": app,
                    "fileSize": downloaded_size,
                    "provider": str(resolved.get("provider", "iOS distribution")),
                }
        finally:
            self._ios_install_lock.release()


class DeviceLogRequestHandler(BaseHTTPRequestHandler):
    server_version = f"DeviceLogViewer/{TOOL_VERSION}"

    @property
    def manager(self) -> LogcatManager:
        return self.server.manager  # type: ignore[attr-defined]

    @property
    def profile(self) -> dict[str, Any]:
        return self.server.profile  # type: ignore[attr-defined]

    def log_message(self, message_format: str, *args: Any) -> None:
        if args and str(args[1]).startswith(("4", "5")):
            sys.stderr.write(f"[HTTP] {message_format % args}\n")

    def _send_json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "请求长度无效。") from exc
        if length > 64 * 1024:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "请求内容过大。")
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "请求 JSON 无效。") from exc
        if not isinstance(value, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "请求必须是 JSON 对象。")
        return value

    def _receive_apk(self) -> Path:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "APK 文件长度无效。") from exc
        if length <= 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "请选择要安装的 APK 文件。")
        if length > MAX_APK_SIZE_BYTES:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "APK 文件超过 1 GB，无法上传。")

        upload_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix="device-log-viewer-", suffix=".apk", delete=False) as upload:
                upload_path = Path(upload.name)
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ApiError(HTTPStatus.BAD_REQUEST, "APK 上传中断，请重新拖入文件。")
                    upload.write(chunk)
                    remaining -= len(chunk)

            _validate_apk_file(upload_path)
            return upload_path
        except Exception:
            if upload_path:
                upload_path.unlink(missing_ok=True)
            raise

    def _handle_api_error(self, exc: Exception) -> None:
        if isinstance(exc, ApiError):
            self._send_json({"ok": False, "error": exc.message}, exc.status)
        else:
            sys.stderr.write(f"[Server] {type(exc).__name__}: {exc}\n")
            self._send_json({"ok": False, "error": "本地服务发生未知错误。"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_GET(self) -> None:  # noqa: N802
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        try:
            if path == "/":
                body = INDEX_FILE.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
            elif path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif path == "/api/devices":
                source = validate_source(parse_qs(parsed_url.query).get("source", ["android"])[0])
                if source == "android":
                    if not self.manager.adb_path:
                        self.manager.refresh_tools(source)
                    devices = list_devices(self.manager.adb_path)
                elif source == "ios-simulator":
                    if not self.manager.xcrun_path:
                        self.manager.refresh_tools(source)
                    devices = list_ios_simulators(self.manager.xcrun_path)
                else:
                    if not self.manager.xcrun_path:
                        self.manager.refresh_tools(source)
                    devices = list_ios_devices(self.manager.xcrun_path)
                self._send_json({"ok": True, "devices": devices})
            elif path in {"/api/apps", "/api/packages"}:
                source = validate_source(parse_qs(parsed_url.query).get("source", ["android"])[0])
                serial = parse_qs(parsed_url.query).get("serial", [""])[0]
                if source == "android":
                    packages = list_packages(self.manager.adb_path, serial)
                    apps = [_app_option(package_name) for package_name in packages]
                elif source == "ios-simulator":
                    apps = list_simulator_apps(self.manager.xcrun_path, serial)
                else:
                    apps = list_ios_device_apps(self.manager.xcrun_path, serial)
                self._send_json({"ok": True, "apps": apps, "packages": [app["id"] for app in apps]})
            elif path == "/api/status":
                self._send_json(
                    {
                        "ok": True,
                        "toolId": TOOL_ID,
                        "version": TOOL_VERSION,
                        "profileId": self.profile["id"],
                        "status": self.manager.status(),
                    }
                )
            elif path == "/api/config":
                self._send_json(
                    {
                        "ok": True,
                        "toolId": TOOL_ID,
                        "version": TOOL_VERSION,
                        "config": public_profile(self.profile),
                    }
                )
            elif path == "/api/stream":
                self._stream_events()
            else:
                self._send_json({"ok": False, "error": "接口不存在。"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._handle_api_error(exc)

    def do_POST(self) -> None:  # noqa: N802
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        try:
            if path == "/api/install-apk":
                serial = parse_qs(parsed_url.query).get("serial", [""])[0]
                apk_path: Path | None = None
                try:
                    apk_path = self._receive_apk()
                    result = self.manager.install_apk(serial, apk_path)
                finally:
                    if apk_path:
                        apk_path.unlink(missing_ok=True)
                self._send_json({"ok": True, **result})
                return

            payload = self._read_json()
            if path == "/api/start":
                status = self.manager.start(
                    str(payload.get("source", "android")),
                    str(payload.get("serial", "")),
                    str(payload.get("appId", payload.get("packageName", ""))),
                )
                self._send_json({"ok": True, "status": status})
            elif path == "/api/install-ios-link":
                result = self.manager.install_ios_link(
                    str(payload.get("serial", "")),
                    str(payload.get("url", "")),
                )
                self._send_json({"ok": True, **result})
            elif path == "/api/install-apk-link":
                result = self.manager.install_apk_link(
                    str(payload.get("serial", "")),
                    str(payload.get("url", "")),
                )
                self._send_json({"ok": True, **result})
            elif path in {"/api/reconnect-tools", "/api/reconnect-adb"}:
                source = str(payload.get("source", "android"))
                self._send_json({"ok": True, "status": self.manager.refresh_tools(source)})
            elif path == "/api/stop":
                self._send_json({"ok": True, "status": self.manager.stop()})
            elif path == "/api/clear-device":
                if validate_source(str(payload.get("source", "android"))) != "android":
                    raise ApiError(HTTPStatus.BAD_REQUEST, "清设备日志仅支持 Android Logcat。")
                self.manager.clear_device(str(payload.get("serial", "")))
                self._send_json({"ok": True})
            else:
                self._send_json({"ok": False, "error": "接口不存在。"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._handle_api_error(exc)

    def _stream_events(self) -> None:
        subscriber = self.manager.subscribe()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def send_event(event_type: str, data: dict[str, Any]) -> None:
            encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            self.wfile.write(f"event: {event_type}\ndata: {encoded}\n\n".encode("utf-8"))
            self.wfile.flush()

        try:
            send_event("status", self.manager.status())
            while True:
                try:
                    item = subscriber.get(timeout=12)
                    event_type = str(item.get("type", "message"))
                    send_event(event_type, item.get("status", item))
                except queue.Empty:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        finally:
            self.manager.unsubscribe(subscriber)


class DeviceLogHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], manager: LogcatManager, profile: dict[str, Any]) -> None:
        self.manager = manager
        self.profile = profile
        super().__init__(address, DeviceLogRequestHandler)

    def handle_error(self, request: Any, client_address: Any) -> None:
        _error_type, error, _traceback = sys.exc_info()
        if isinstance(error, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="在浏览器中查看并下载 Android 与 iOS 日志。")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    parser.add_argument("--port", type=int, help="监听端口；默认读取 Profile 的 defaultPort")
    parser.add_argument("--profile", default=str(DEFAULT_PROFILE_FILE), help="Profile JSON 路径")
    parser.add_argument("--adb", help="adb 可执行文件路径；默认自动查找 PATH")
    parser.add_argument("--no-open", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--print-port", action="store_true", help="输出最终默认端口并退出")
    parser.add_argument("--print-profile-id", action="store_true", help="输出 Profile id 并退出")
    parser.add_argument("--version", action="version", version=TOOL_VERSION)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        profile = load_profile(args.profile)
    except ValueError as exc:
        print(f"Profile 配置错误：{exc}", file=sys.stderr)
        return 2
    port = args.port if args.port is not None else profile["defaultPort"]
    if not 1 <= port <= 65535:
        print("端口必须是 1–65535 之间的整数。", file=sys.stderr)
        return 2
    if args.print_port:
        print(port)
        return 0
    if args.print_profile_id:
        print(profile["id"])
        return 0
    adb_path = find_adb(args.adb)
    xcrun_path = find_xcrun()
    manager = LogcatManager(adb_path, xcrun_path)
    try:
        server = DeviceLogHttpServer((args.host, port), manager, profile)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print(
                f"端口 {port} 已被占用。请关闭旧服务，或执行 python3 server.py --port {port + 1}",
                file=sys.stderr,
            )
            return 2
        raise
    url = f"http://{args.host}:{port}"

    def shutdown(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print(f"Device Log Viewer {TOOL_VERSION}: {url}")
    print(f"Profile: {profile['displayName']} ({profile['id']})")
    print(f"ADB: {adb_path or '未找到（页面会显示安装提示）'}")
    print(f"Xcode: {xcrun_path or '未找到（iOS 功能不可用）'}")
    print("按 Ctrl+C 停止服务。")
    if not args.no_open:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()

    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        manager.stop(publish=False)
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
