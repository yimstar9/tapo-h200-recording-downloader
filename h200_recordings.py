from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import getpass
import hashlib
import ipaddress
import json
import os
import socket
import subprocess
import sys
import warnings
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from pytapo import Tapo
from pytapo.const import CONNECTION_TIMEOUT
from pytapo.media_stream._utils import (
    generate_nonce,
    parse_http_headers,
    parse_http_response,
)
from pytapo.media_stream.crypto import AESHelper
from pytapo.media_stream.error import HttpStatusCodeException, KeyExchangeMissingException
from pytapo.media_stream.session import HttpMediaSession


ROOT = Path(__file__).resolve().parent


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_dotenv()


DEFAULT_HUB_HOST = os.environ.get("TAPO_HUB_HOST", "")
DEFAULT_DEVICE_ID = os.environ.get("TAPO_DEVICE_ID", "")
DEFAULT_CAMERA_ALIAS = os.environ.get("TAPO_CAMERA_ALIAS", "")
DEFAULT_CONFIG_PATH = ROOT / "tapo_h200.local.json"
DEFAULT_KEY_PATH = ROOT / "tapo_h200.local.key"
DEFAULT_DOTENV_PATH = ROOT / ".env"


class PlainMediaCrypto:
    def decrypt(self, data: bytes) -> bytes:
        return data

    def encrypt(self, data: bytes) -> bytes:
        return data


_ORIGINAL_AES_FROM_KEYEXCHANGE = AESHelper.from_keyexchange_and_password


def h200_media_crypto_from_keyexchange(
    cls,
    key_exchange,
    cloud_password,
    super_secret_key,
    encryptionMethod,
):
    raw = (
        key_exchange
        if isinstance(key_exchange, str)
        else key_exchange.decode("utf-8", errors="ignore")
    )
    if 'nonce=""' in raw:
        return PlainMediaCrypto()
    return _ORIGINAL_AES_FROM_KEYEXCHANGE(
        key_exchange,
        cloud_password,
        super_secret_key,
        encryptionMethod,
    )


AESHelper.from_keyexchange_and_password = classmethod(h200_media_crypto_from_keyexchange)


async def h200_media_start(self: HttpMediaSession) -> None:
    req_line = f"POST /stream{self.query_params_str} HTTP/1.1".encode()
    headers = {
        b"Content-Type": "multipart/mixed;boundary={}".format(
            self.client_boundary.decode(),
        ).encode(),
        b"Connection": b"keep-alive",
        b"Content-Length": b"-1",
    }
    if self.query_params_str and "playerId" in self.query_params:
        headers[b"X-Client-UUID"] = self.query_params["playerId"].encode()

    try:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.ip, self.port),
            timeout=CONNECTION_TIMEOUT,
        )

        await self._send_http_request(req_line, headers)
        data = await self._reader.readuntil(b"\r\n\r\n")
        _res_line, headers_block = data.split(b"\r\n", 1)
        res_headers = parse_http_headers(headers_block)

        content_length = int(res_headers.get("Content-Length", "0"))
        if content_length > 0:
            await self._reader.readexactly(content_length)

        self._auth_data = {
            i[0].strip().replace('"', ""): i[1].strip().replace('"', "")
            for i in (
                j.split("=")
                for j in res_headers["WWW-Authenticate"].split(" ", 1)[1].split(",")
            )
        }
        self._auth_data.update(
            {
                "username": self.username,
                "cnonce": generate_nonce(24).decode(),
                "nc": "00000001",
                "qop": "auth",
            }
        )

        challenge1 = hashlib.md5(
            ":".join(
                (self.username, self._auth_data["realm"], self.hashed_password)
            ).encode(),
        ).hexdigest()
        challenge2 = hashlib.md5(b"POST:/stream").hexdigest()

        self._auth_data["response"] = hashlib.md5(
            b":".join(
                (
                    challenge1.encode(),
                    self._auth_data["nonce"].encode(),
                    self._auth_data["nc"].encode(),
                    self._auth_data["cnonce"].encode(),
                    self._auth_data["qop"].encode(),
                    challenge2.encode(),
                ),
            ),
        ).hexdigest()

        self._authorization = (
            'Digest username="{username}",realm="{realm}"'
            ',uri="/stream",algorithm=MD5,'
            'nonce="{nonce}",nc={nc},cnonce="{cnonce}",qop={qop},'
            'response="{response}",opaque="{opaque}"'.format(
                **self._auth_data,
            ).encode()
        )
        headers[b"Authorization"] = self._authorization
        if "Set-Cookie" in res_headers:
            headers[b"Cookie"] = res_headers["Set-Cookie"].split(";", 1)[0].encode()

        if res_headers.get("Connection", "").lower() == "close":
            self._writer.close()
            await self._writer.wait_closed()
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.ip, self.port),
                timeout=CONNECTION_TIMEOUT,
            )

        await self._send_http_request(req_line, headers)

        data = await self._reader.readuntil(b"\r\n\r\n")
        res_line, headers_block = data.split(b"\r\n", 1)
        _, status_code, _ = parse_http_response(res_line)
        if status_code != 200:
            raise HttpStatusCodeException(status_code)

        res_headers = parse_http_headers(headers_block)
        if "Key-Exchange" not in res_headers:
            raise KeyExchangeMissingException

        boundary = None
        if "Content-Type" in res_headers:
            try:
                boundary = filter(
                    lambda chunk: chunk.startswith("boundary="),
                    res_headers["Content-Type"].split(";"),
                ).__next__()
                boundary = boundary.split("=")[1].encode()
            except Exception:
                boundary = None
        if not boundary:
            warnings.warn(
                "Server did not provide a multipart/mixed boundary. Assuming default.",
            )
        else:
            self._device_boundary = boundary

        self._key_exchange = res_headers["Key-Exchange"]
        self._aes = AESHelper.from_keyexchange_and_password(
            self._key_exchange.encode(),
            self.cloud_password.encode(),
            self.super_secret_key.encode(),
            self.encryptionMethod,
        )

        self._started = True
        self._response_handler_task = asyncio.create_task(
            self._device_response_handler_loop(),
        )
    except Exception:
        try:
            self._writer.close()
        except Exception:
            pass
        self._started = False
        raise


HttpMediaSession.start = h200_media_start


@dataclass
class Credentials:
    host: str
    user: str
    password: str
    cloud_password: str


def resolve_config_path(path: str | None) -> Path:
    if not path:
        return DEFAULT_CONFIG_PATH
    config_path = Path(path).expanduser()
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    return config_path


def resolve_key_path(path: str | None) -> Path:
    if not path:
        return DEFAULT_KEY_PATH
    key_path = Path(path).expanduser()
    if not key_path.is_absolute():
        key_path = ROOT / key_path
    return key_path


def load_or_create_key(path: Path) -> bytes:
    if path.exists():
        key = path.read_bytes().strip()
        os.chmod(path, 0o600)
        return key

    key = Fernet.generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(key + b"\n")
    os.chmod(path, 0o600)
    return key


def load_key(path: Path) -> bytes:
    if not path.exists():
        raise SystemExit(
            f"Encrypted config needs key file, but it does not exist: {path}"
        )
    key = path.read_bytes().strip()
    os.chmod(path, 0o600)
    return key


def encrypt_config_payload(data: dict[str, Any], key: bytes) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
    return Fernet(key).encrypt(raw).decode("ascii")


def decrypt_config_payload(token: str, key: bytes) -> dict[str, Any]:
    try:
        raw = Fernet(key).decrypt(token.encode("ascii"))
    except (InvalidToken, ValueError) as err:
        raise SystemExit("Failed to decrypt local Tapo config.") from err

    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise SystemExit("Decrypted config must be a JSON object.")
    return data


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as err:
        raise SystemExit(f"Invalid JSON in {path}: {err}") from err
    if not isinstance(data, dict):
        raise SystemExit(f"Config must be a JSON object: {path}")
    if data.get("storage") == "fernet-local-key":
        key_file = data.get("key_file")
        key_path = resolve_key_path(key_file) if key_file else DEFAULT_KEY_PATH
        return decrypt_config_payload(data["payload"], load_key(key_path))
    return data


def save_config(path: Path, key_path: Path, creds: Credentials) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "host": creds.host,
        "user": creds.user,
        "password": creds.password,
        "cloud_password": creds.cloud_password,
    }
    data = {
        "version": 2,
        "storage": "fernet-local-key",
        "key_file": str(key_path.relative_to(ROOT))
        if key_path.is_relative_to(ROOT)
        else str(key_path),
        "payload": encrypt_config_payload(payload, load_or_create_key(key_path)),
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    os.chmod(path, 0o600)
    print(f"Saved encrypted local credentials to {path}")
    print(f"Saved local encryption key to {key_path}")


def path_for_env(path: Path) -> str:
    return (
        str(path.relative_to(ROOT))
        if path.is_relative_to(ROOT)
        else str(path)
    )


def encode_env_value(value: str) -> str:
    if value == "":
        return ""
    if any(ch.isspace() or ch in "#'\"" for ch in value):
        return "'" + value.replace("'", "'\"'\"'") + "'"
    return value


def upsert_dotenv(updates: dict[str, str], path: Path = DEFAULT_DOTENV_PATH) -> None:
    exists = path.exists()
    lines = path.read_text(encoding="utf-8").splitlines() if exists else []
    pending = dict(updates)
    output: list[str] = [] if exists else [
        "# Local runtime settings for this machine.",
        "# This file is intentionally ignored by git.",
    ]

    for raw_line in lines:
        stripped = raw_line.strip()
        line = (
            stripped[len("export ") :].lstrip()
            if stripped.startswith("export ")
            else stripped
        )
        if not line or line.startswith("#") or "=" not in line:
            output.append(raw_line)
            continue

        key = line.split("=", 1)[0].strip()
        if key in pending:
            output.append(f"{key}={encode_env_value(pending.pop(key))}")
        else:
            output.append(raw_line)

    if pending and output and output[-1].strip():
        output.append("")
    for key, value in pending.items():
        output.append(f"{key}={encode_env_value(value)}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(output) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def save_local_env(
    creds: Credentials,
    config_path: Path,
    key_path: Path,
    camera: dict[str, Any] | None = None,
) -> None:
    updates = {
        "TAPO_CONFIG": path_for_env(config_path),
        "TAPO_KEY_FILE": path_for_env(key_path),
        "TAPO_HUB_HOST": creds.host,
        "TAPO_USER": creds.user,
    }
    if camera:
        device_id = camera.get("device_id")
        alias = camera.get("alias")
        if device_id:
            updates["TAPO_DEVICE_ID"] = str(device_id)
        if alias:
            updates["TAPO_CAMERA_ALIAS"] = str(alias)

    upsert_dotenv(updates)
    os.environ.update(updates)
    print(f"Saved local runtime settings to {DEFAULT_DOTENV_PATH}")


def host_from_sources(args: argparse.Namespace, config: dict[str, Any]) -> str:
    return (
        args.host
        or os.environ.get("TAPO_HUB_HOST")
        or config.get("host")
        or DEFAULT_HUB_HOST
    )


def require_host(host: str) -> bool:
    if host:
        return True
    print(
        "H200 host is required. Pass --host, set TAPO_HUB_HOST, "
        "or run --setup to save local config.",
        file=sys.stderr,
    )
    return False


def prompt_text(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default or ""


def prompt_credentials(args: argparse.Namespace) -> Credentials:
    config = load_config(resolve_config_path(args.config))
    host = host_from_sources(args, config)
    user = (
        args.user
        or os.environ.get("TAPO_USER")
        or config.get("user")
        or "admin"
    )
    password = (
        os.environ.get("TAPO_HUB_PASSWORD")
        or config.get("password")
        or config.get("hub_password")
    )
    cloud_password = (
        os.environ.get("TAPO_CLOUD_PASSWORD")
        or config.get("cloud_password")
        or config.get("cloudPassword")
    )

    if not password:
        password = getpass.getpass(
            "H200/camera account password (hidden; try Tapo cloud password if unsure): "
        )
    if not cloud_password:
        cloud_password = getpass.getpass(
            "Tapo cloud password (hidden; Enter to reuse previous password): "
        )
        if not cloud_password:
            cloud_password = password

    return Credentials(
        host=host,
        user=user,
        password=password,
        cloud_password=cloud_password,
    )


def go2rtc_password_hash(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest().upper()


def go2rtc_stream_url(creds: Credentials, camera: dict[str, Any]) -> str:
    password_hash = go2rtc_password_hash(creds.password)
    return (
        f"tapo://admin:{password_hash}@{creds.host}/"
        f"?deviceId={camera.get('device_id')}"
    )


def connect_hub(creds: Credentials) -> Tapo:
    return Tapo(creds.host, creds.user, creds.password, creds.cloud_password)


def can_connect(host: str, port: int, timeout: float = 2.0) -> tuple[bool, str | None]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, None
    except OSError as err:
        return False, str(err)


def infer_scan_network(host: str) -> ipaddress.IPv4Network:
    ip = ipaddress.ip_address(host)
    return ipaddress.ip_network(f"{ip}/24", strict=False)


def scan_open_ports(host: str) -> list[int]:
    open_ports = []
    for port in (443, 8800):
        ok, _ = can_connect(host, port, timeout=0.35)
        if ok:
            open_ports.append(port)
    return open_ports


def scan_network(host: str) -> int:
    network = infer_scan_network(host)
    print(f"Scanning {network} for ports 443 and 8800...")

    candidates = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as executor:
        futures = {
            executor.submit(scan_open_ports, str(ip)): str(ip)
            for ip in network.hosts()
        }
        for future in concurrent.futures.as_completed(futures):
            ports = future.result()
            if ports:
                candidates.append((futures[future], ports))

    if not candidates:
        print("No hosts with port 443 or 8800 open.")
        return 1

    for ip, ports in sorted(candidates, key=lambda item: item[0]):
        print(f"{ip}: {','.join(str(port) for port in ports)}")
    return 0


def preflight_hub(host: str) -> bool:
    ok, error = can_connect(host, 443, timeout=3.0)
    if ok:
        return True

    print()
    print(f"Cannot reach H200 control API at {host}:443")
    print(f"Error: {error}")
    print()
    print("Check:")
    print("- H200 is powered on and online in the Tapo app")
    print("- This PC is on the same LAN/VLAN as the H200")
    print("- H200 IP has not changed")
    print("- Guest Wi-Fi / AP isolation is disabled")
    print()
    print("Try finding candidates:")
    print(f"  .venv-h200/bin/python h200_recordings.py --scan --host {host}")
    return False


def unwrap_child_list(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, dict):
        if "child_device_list" in result:
            return result["child_device_list"]
        if "childControl" in result:
            return result["childControl"].get("child_device_list", [])
    return []


def normalize_mac(value: str | None) -> str:
    return (value or "").replace(":", "").replace("-", "").upper()


def list_paired_cameras(hub: Tapo) -> list[dict[str, Any]]:
    cameras: list[dict[str, Any]] = []

    try:
        result = hub.executeFunction(
            "getGeneralDeviceList",
            {"general_camera_manage": {"paired_general_device_list": {}}},
        )
        cameras = (
            result.get("general_camera_manage", {})
            .get("paired_general_device_list", [])
        )
    except Exception:
        cameras = []

    if not cameras:
        cameras = unwrap_child_list(hub.getChildDevices())

    normalized = []
    for cam in cameras:
        device_id = cam.get("device_id") or cam.get("deviceId")
        alias = cam.get("alias") or cam.get("device_name") or cam.get("nickname")
        model = cam.get("device_model") or cam.get("model")
        mac = normalize_mac(cam.get("mac") or cam.get("device_mac"))
        normalized.append({
            **cam,
            "device_id": device_id,
            "alias": alias,
            "device_model": model,
            "mac": mac,
        })

    return normalized


def pick_camera(cameras: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    camera_index = getattr(args, "camera_index", None)
    if camera_index is not None:
        return cameras[camera_index]

    device_id = getattr(args, "device_id", None) or DEFAULT_DEVICE_ID
    if device_id:
        for cam in cameras:
            if cam.get("device_id") == device_id:
                return cam

    camera_alias = getattr(args, "camera_alias", None) or DEFAULT_CAMERA_ALIAS
    hint = camera_alias.lower()
    if hint:
        for cam in cameras:
            text = f"{cam.get('alias') or ''} {cam.get('device_model') or ''}".lower()
            if hint in text:
                return cam

    for cam in cameras:
        text = f"{cam.get('alias') or ''} {cam.get('device_model') or ''}".lower()
        if "d230" in text or "doorbell" in text:
            return cam

    return cameras[0]


def local_timezone():
    return datetime.now().astimezone().tzinfo


def local_today() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d")


def date_to_utc_range(date: str) -> tuple[int, int]:
    day = datetime.strptime(date, "%Y%m%d").replace(tzinfo=local_timezone())
    start = int(day.timestamp())
    return start, start + 86399


def list_recording_dates(
    hub: Tapo,
    camera: dict[str, Any],
    start_date: str,
    end_date: str,
) -> list[str]:
    result = hub.executeFunction(
        "searchDateWithVideo",
        {
            "playback": {
                "search_year_utility": {
                    "channel": [0],
                    "child_device_id": camera["device_id"],
                    "child_device_mac": camera["mac"],
                    "start_date": start_date,
                    "end_date": end_date,
                }
            }
        },
    )

    dates: list[str] = []
    for row in result.get("playback", {}).get("search_results", []):
        for value in row.values():
            if isinstance(value, dict) and "date" in value:
                dates.append(value["date"])
    return sorted(set(dates))


def list_recordings_for_day(
    hub: Tapo,
    camera: dict[str, Any],
    date: str,
) -> list[dict[str, Any]]:
    start_time, end_time = date_to_utc_range(date)
    result = hub.executeFunction(
        "searchVideoWithUTC",
        {
            "playback": {
                "search_video_with_utc": {
                    "channel": 0,
                    "child_device_id": camera["device_id"],
                    "child_device_mac": camera["mac"],
                    "start_time": start_time,
                    "end_time": end_time,
                    "start_index": 0,
                    "end_index": 999,
                    "player_id": uuid.uuid4().hex.upper(),
                }
            }
        },
    )

    clips: list[dict[str, Any]] = []
    for row in result.get("playback", {}).get("search_video_results", []):
        for value in row.values():
            if isinstance(value, dict):
                value = {**value, "date": date}
                clips.append(value)
    return clips


def list_recordings(
    hub: Tapo,
    camera: dict[str, Any],
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    clips: list[dict[str, Any]] = []
    for date in list_recording_dates(hub, camera, start_date, end_date):
        clips.extend(list_recordings_for_day(hub, camera, date))
    return sorted(clips, key=lambda item: int(item.get("startTime", 0)))


def extract_private_alaw_audio(ts_path: Path, raw_path: Path) -> bool:
    def payload_from_packet(packet: bytes) -> bytes:
        adaptation_field_control = (packet[3] >> 4) & 0x03
        offset = 4
        if adaptation_field_control in (2, 3):
            offset += 1 + packet[4]
        if adaptation_field_control not in (1, 3) or offset >= len(packet):
            return b""
        return packet[offset:]

    def write_pes_audio(pes: bytearray, handle: Any) -> int:
        if len(pes) < 14 or pes[:3] != b"\x00\x00\x01":
            return 0
        stream_id = pes[3]
        if not 0xC0 <= stream_id <= 0xDF:
            return 0
        pes_length = int.from_bytes(pes[4:6], "big")
        header_length = pes[8]
        data_start = 9 + header_length
        data_end = 6 + pes_length if pes_length else len(pes)
        data = bytes(pes[data_start:data_end])
        if not data:
            return 0
        handle.write(data)
        return len(data)

    raw_path.parent.mkdir(parents=True, exist_ok=True)
    current: dict[int, bytearray] = {}
    bytes_written = 0
    with ts_path.open("rb") as source, raw_path.open("wb") as audio:
        while True:
            packet = source.read(188)
            if not packet:
                break
            if len(packet) != 188 or packet[0] != 0x47:
                continue
            pid = ((packet[1] & 0x1F) << 8) | packet[2]
            payload_unit_start = bool(packet[1] & 0x40)
            payload = payload_from_packet(packet)
            if not payload:
                continue
            if payload_unit_start:
                previous = current.pop(pid, None)
                if previous is not None:
                    bytes_written += write_pes_audio(previous, audio)
                current[pid] = bytearray(payload)
            elif pid in current:
                current[pid].extend(payload)

        for pes in current.values():
            bytes_written += write_pes_audio(pes, audio)

    if bytes_written == 0:
        raw_path.unlink(missing_ok=True)
        return False

    print(f"Extracted {bytes_written // 1024} KiB A-law audio: {raw_path}")
    return True


def remux_ts_to_mp4(ts_path: Path, mp4_path: Path) -> bool:
    mp4_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = mp4_path.with_suffix(".tmp.mp4")
    audio_path = mp4_path.with_suffix(".alaw")
    has_audio = extract_private_alaw_audio(ts_path, audio_path)
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(ts_path),
    ]
    if has_audio:
        cmd.extend([
            "-f",
            "alaw",
            "-ar",
            "8000",
            "-ac",
            "1",
            "-i",
            str(audio_path),
        ])
    cmd.extend([
        "-map",
        "0:v:0",
    ])
    if has_audio:
        cmd.extend([
            "-map",
            "1:a:0",
        ])
    cmd.extend([
        "-c:v",
        "copy",
    ])
    if has_audio:
        cmd.extend([
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            "-shortest",
        ])
    else:
        cmd.append("-an")
    cmd.extend([
        "-movflags",
        "+faststart",
        str(tmp_path),
    ])
    try:
        subprocess.run(cmd, check=True)
    except (OSError, subprocess.CalledProcessError) as err:
        print(f"MP4 remux failed: {err}")
        tmp_path.unlink(missing_ok=True)
        audio_path.unlink(missing_ok=True)
        return False
    finally:
        audio_path.unlink(missing_ok=True)
    tmp_path.replace(mp4_path)
    return mp4_path.exists() and mp4_path.stat().st_size > 0


async def download_recording_ts(
    creds: Credentials,
    hub: Tapo,
    camera: dict[str, Any],
    start_time: int,
    end_time: int,
    output: Path,
    window_size: int,
    stall_timeout: float,
) -> bool:
    player_id = uuid.uuid4().hex.upper()
    query_params = {
        "camera_mac": camera["mac"],
        "type": "download",
        "playerId": player_id,
        "media_type": 0,
    }
    payload = {
        "type": "request",
        "seq": 1,
        "params": {
            "method": "get",
            "download": {
                "audio_config": {"encode_type": "OPUS", "sample_rate": "16"},
                "dev_id": camera["device_id"],
                "mac": camera["mac"],
                "channels": [0],
                "client_id": 1,
                "end_time": str(end_time),
                "event_type": [],
                "media_type": 0,
                "player_id": player_id,
                "start_time": str(start_time),
            },
        },
    }

    session = HttpMediaSession(
        ip=creds.host,
        cloud_password=creds.cloud_password,
        super_secret_key=hub.superSecretKey,
        encryptionMethod=hub.getEncryptionMethod(),
        port=8800,
        username=creds.user,
        query_params=query_params,
        window_size=window_size,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    chunks = 0
    bytes_written = 0
    with output.open("wb") as handle:
        async with session as media:
            stream = media.transceive(json.dumps(payload, separators=(",", ":")))
            while True:
                try:
                    response = await asyncio.wait_for(
                        stream.__anext__(),
                        timeout=stall_timeout,
                    )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    print(f"Download stalled for {stall_timeout}s; stopping.")
                    break

                if response.mimetype == "application/json":
                    try:
                        body = json.loads(response.plaintext.decode())
                    except json.JSONDecodeError:
                        continue
                    params = body.get("params", {})
                    if body.get("type") == "response" and params.get("error_code", 0) != 0:
                        print(json.dumps(body, ensure_ascii=False, indent=2))
                        return False
                    if (
                        body.get("type") == "notification"
                        and params.get("event_type") == "stream_status"
                        and params.get("status") == "finished"
                    ):
                        break
                elif response.mimetype == "video/mp2t":
                    handle.write(response.plaintext)
                    chunks += 1
                    bytes_written += len(response.plaintext)

    if chunks == 0:
        output.unlink(missing_ok=True)
        return False

    print(f"Wrote {bytes_written // 1024} KiB in {chunks} chunks: {output}")
    return True


async def download_recording(
    creds: Credentials,
    hub: Tapo,
    camera: dict[str, Any],
    start_time: int,
    end_time: int,
    output: Path,
    window_size: int,
    stall_timeout: float,
    output_format: str = "mp4",
    keep_ts: bool = False,
) -> bool:
    if output_format == "ts":
        return await download_recording_ts(
            creds,
            hub,
            camera,
            start_time,
            end_time,
            output.with_suffix(".ts"),
            window_size,
            stall_timeout,
        )

    mp4_output = output.with_suffix(".mp4")
    ts_output = mp4_output.with_suffix(".download.ts")
    ok = await download_recording_ts(
        creds,
        hub,
        camera,
        start_time,
        end_time,
        ts_output,
        window_size,
        stall_timeout,
    )
    if not ok:
        return False

    ok = remux_ts_to_mp4(ts_output, mp4_output)
    if ok:
        print(f"Remuxed MP4: {mp4_output}")
        if not keep_ts:
            ts_output.unlink(missing_ok=True)
    return ok


def print_children(cameras: list[dict[str, Any]]) -> None:
    for idx, cam in enumerate(cameras):
        print(
            f"{idx}: alias={cam.get('alias')} "
            f"model={cam.get('device_model')} "
            f"device_id={cam.get('device_id')} "
            f"mac={cam.get('mac')}"
        )


def print_go2rtc_hint(creds: Credentials, camera: dict[str, Any]) -> None:
    print()
    print("go2rtc stream example:")
    print()
    print("streams:")
    print("  d230:")
    print(f"    - {go2rtc_stream_url(creds, camera)}")
    print()
    print("Notes:")
    print("- go2rtc Tapo URL username is usually admin, not your Tapo email.")
    print("- deviceId must be the D230 child device_id, not the H200 MAC address.")
    print("- The SHA256 hash above is derived from the H200/camera password.")
    print("- Treat the hash like a password; do not commit it to git.")


def run_first_setup(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.config)
    key_path = resolve_key_path(args.key_file)
    config = load_config(config_path)
    default_host = host_from_sources(args, config)

    print("Tapo H200 first setup")
    print()
    print("This will:")
    print("1. Check the H200 IP address")
    print("2. Save local encrypted credentials")
    print("3. Find the paired D230 device_id")
    print("4. Save H200 IP, config/key paths, D230 device_id, and alias to .env")
    print("5. Print a go2rtc stream example")
    print()

    host = prompt_text("H200 IP address", default_host)
    args.host = host
    if not require_host(host):
        return 1
    if not preflight_hub(host):
        print()
        print("Scanning the same /24 network for H200 candidates...")
        scan_network(host)
        print()
        host = prompt_text("H200 IP address to use", host)
        args.host = host
        if not preflight_hub(host):
            return 1

    creds = prompt_credentials(args)

    print()
    print(f"Connecting to H200 at {creds.host} as {creds.user}...")
    try:
        hub = connect_hub(creds)
        cameras = list_paired_cameras(hub)
    except Exception as err:
        if "Invalid authentication data" in str(err):
            print(
                "인증에 실패했습니다. H200/camera account 비밀번호 또는 "
                "Tapo cloud 비밀번호가 올바르지 않습니다.",
                file=sys.stderr,
            )
            print("환경설정 파일은 변경하지 않았습니다.", file=sys.stderr)
            return 2

        print(f"H200 API 요청에 실패했습니다: {err}", file=sys.stderr)
        print("환경설정 파일은 변경하지 않았습니다.", file=sys.stderr)
        return 1

    if not cameras:
        print("No paired cameras returned by H200.")
        print("환경설정 파일은 변경하지 않았습니다.")
        return 1

    print()
    print("Paired child devices:")
    print_children(cameras)

    camera = pick_camera(cameras, args)
    print()
    print(
        f"Selected camera: alias={camera.get('alias')} "
        f"model={camera.get('device_model')} "
        f"device_id={camera.get('device_id')} "
        f"mac={camera.get('mac')}"
    )
    save_config(config_path, key_path, creds)
    save_local_env(creds, config_path, key_path, camera)
    print_go2rtc_hint(creds, camera)
    print()
    print("Setup complete. You can now run:")
    print("  .venv/bin/python h200_recordings.py --list")
    print("  .venv/bin/python h200_web.py --bind 0.0.0.0 --port 8092")
    return 0


def print_recordings(clips: list[dict[str, Any]]) -> None:
    if not clips:
        print("No recordings found.")
        return

    print(f"{'idx':>4} {'start_time':>10} {'end_time':>10} {'duration':>8} local_time")
    for idx, clip in enumerate(clips):
        start_time = int(clip["startTime"])
        end_time = int(clip["endTime"])
        when = datetime.fromtimestamp(start_time).astimezone().isoformat()
        print(
            f"{idx:>4} {start_time:>10} {end_time:>10} "
            f"{end_time - start_time:>7}s {when}"
        )


def output_path_for_clip(
    output_dir: str,
    camera: dict[str, Any],
    start_time: int,
    extension: str = ".mp4",
) -> Path:
    local_start = datetime.fromtimestamp(start_time).astimezone()
    date_dir = local_start.strftime("%Y%m%d")
    timestamp = local_start.strftime("%Y%m%d_%H%M%S")
    alias = (camera.get("alias") or "d230").replace(" ", "_")
    if not extension.startswith("."):
        extension = "." + extension
    return Path(output_dir) / date_dir / f"{alias}_{timestamp}{extension}"


def should_skip_output(path: Path, skip_existing: bool) -> bool:
    return skip_existing and path.exists() and path.stat().st_size > 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List or download Tapo H200 hub recordings for a paired D230.",
    )
    parser.add_argument("--host")
    parser.add_argument("--user")
    parser.add_argument(
        "--config",
        default=os.environ.get("TAPO_CONFIG", str(DEFAULT_CONFIG_PATH)),
    )
    parser.add_argument(
        "--key-file",
        default=os.environ.get("TAPO_KEY_FILE", str(DEFAULT_KEY_PATH)),
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="interactive first-run setup: H200 host, credentials, device_id, go2rtc URL",
    )
    parser.add_argument("--save-config", action="store_true")
    parser.add_argument(
        "--save-env",
        action="store_true",
        help="save H200 host/user and selected D230 device_id/alias to .env",
    )
    parser.add_argument("--scan", action="store_true")
    parser.add_argument("--camera-index", type=int)
    parser.add_argument("--device-id", default=DEFAULT_DEVICE_ID)
    parser.add_argument("--camera-alias", default=DEFAULT_CAMERA_ALIAS)
    parser.add_argument("--children", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--download-all", action="store_true")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--start-date", help="YYYYMMDD")
    parser.add_argument("--end-date", help="YYYYMMDD")
    parser.add_argument("--download-index", type=int)
    parser.add_argument("--output-dir", default="recordings")
    parser.add_argument("--format", choices=["mp4", "ts"], default="mp4")
    parser.add_argument("--keep-ts", action="store_true")
    parser.add_argument("--window-size", type=int, default=50)
    parser.add_argument("--stall-timeout", type=float, default=10.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(resolve_config_path(args.config))
    host = host_from_sources(args, config)

    if args.setup:
        return run_first_setup(args)

    if (
        not args.children
        and not args.list
        and not args.download_all
        and args.download_index is None
        and not args.save_config
        and not args.save_env
    ):
        print(
            "Choose --setup, --scan, --save-config, --children, --list, "
            "--download-all, or --download-index N.",
            file=sys.stderr,
        )
        return 2

    if not require_host(host):
        return 1

    if args.scan:
        return scan_network(host)

    if not preflight_hub(host):
        return 1

    creds = prompt_credentials(args)
    if args.save_config:
        config_path = resolve_config_path(args.config)
        key_path = resolve_key_path(args.key_file)
        save_config(
            config_path,
            key_path,
            creds,
        )
        save_local_env(creds, config_path, key_path)
        if (
            not args.children
            and not args.list
            and not args.download_all
            and args.download_index is None
            and not args.save_env
        ):
            return 0

    print(f"Connecting to H200 at {creds.host} as {creds.user}...")
    hub = connect_hub(creds)

    cameras = list_paired_cameras(hub)
    if not cameras:
        print("No paired cameras returned by H200.")
        return 1

    if args.children:
        print_children(cameras)
        if args.save_env:
            camera = pick_camera(cameras, args)
            save_local_env(
                creds,
                resolve_config_path(args.config),
                resolve_key_path(args.key_file),
                camera,
            )
        return 0

    camera = pick_camera(cameras, args)
    print(
        f"Using camera: alias={camera.get('alias')} "
        f"model={camera.get('device_model')} "
        f"device_id={camera.get('device_id')} "
        f"mac={camera.get('mac')}"
    )

    if args.save_env:
        save_local_env(
            creds,
            resolve_config_path(args.config),
            resolve_key_path(args.key_file),
            camera,
        )
        if not args.list and not args.download_all and args.download_index is None:
            return 0

    today = local_today()
    start_date = args.start_date or today
    end_date = args.end_date or start_date
    clips = list_recordings(hub, camera, start_date, end_date)

    if args.list:
        print_recordings(clips)
        return 0

    if args.download_all:
        if not clips:
            print("No recordings found.")
            return 0

        ok_count = 0
        skip_count = 0
        fail_count = 0
        print(f"Found {len(clips)} clip(s).")
        for idx, clip in enumerate(clips):
            start_time = int(clip["startTime"])
            end_time = int(clip["endTime"])
            output = output_path_for_clip(args.output_dir, camera, start_time, args.format)
            if should_skip_output(output, args.skip_existing):
                print(f"[{idx + 1}/{len(clips)}] skip existing: {output}")
                skip_count += 1
                continue

            print(
                f"[{idx + 1}/{len(clips)}] download "
                f"{end_time - start_time}s -> {output}"
            )
            ok = asyncio.run(
                download_recording(
                    creds,
                    hub,
                    camera,
                    start_time,
                    end_time,
                    output,
                    args.window_size,
                    args.stall_timeout,
                    args.format,
                    args.keep_ts,
                )
            )
            if ok:
                ok_count += 1
            else:
                fail_count += 1

        print(
            f"Done. downloaded={ok_count} skipped={skip_count} failed={fail_count}"
        )
        return 0 if fail_count == 0 else 1

    if args.download_index is not None:
        if args.download_index < 0 or args.download_index >= len(clips):
            print(f"Invalid --download-index. Found {len(clips)} clip(s).")
            return 2

        clip = clips[args.download_index]
        start_time = int(clip["startTime"])
        end_time = int(clip["endTime"])
        output = output_path_for_clip(args.output_dir, camera, start_time, args.format)
        if should_skip_output(output, args.skip_existing):
            print(f"Skip existing: {output}")
            return 0
        ok = asyncio.run(
            download_recording(
                creds,
                hub,
                camera,
                start_time,
                end_time,
                output,
                args.window_size,
                args.stall_timeout,
                args.format,
                args.keep_ts,
            )
        )
        return 0 if ok else 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
