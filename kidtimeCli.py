#!/usr/bin/env python
"""
Windows foreground-process collector for KidTime.

The client records the active window once per minute and sends incremental
events to the monitoring server. If the network is temporarily unavailable, it
keeps a bounded in-memory queue and retries on the next collection interval.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import logging
import os
import platform
import socket
import subprocess
import sys
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import psutil
import requests
import win32gui
import win32process


SERVER_URL = "http://8.148.226.47:8001"
CLIENT_ID = socket.gethostname()

# Replace this before deployment. Generate with:
# python -c "import secrets; print(secrets.token_hex(32))"
SHARED_KEY_HEX = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

LOG_INTERVAL_SECONDS = 60
SIGNATURE_TTL_SECONDS = 300
QUEUE_LIMIT = 100
REQUEST_TIMEOUT_SECONDS = 10
APP_NAME = "KidTime"
TASK_NAME = "KidTimeMonitor"


def default_base_dir() -> Path:
    root = os.environ.get("PROGRAMDATA") or str(Path.home())
    return Path(root) / APP_NAME


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def setup_logging(base_dir: Path, verbose: bool = False) -> None:
    base_dir.mkdir(parents=True, exist_ok=True)
    log_file = base_dir / "kidtimeCli.log"
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.FileHandler(log_file, encoding="utf-8")]
    if sys.stdout:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def shared_key_bytes(key_hex: str) -> bytes:
    try:
        key = bytes.fromhex(key_hex)
    except ValueError as exc:
        raise ValueError("shared key must be a 64-character hex string") from exc
    if len(key) != 32:
        raise ValueError("shared key must decode to 32 bytes / 256 bits")
    return key


def get_active_window_info() -> tuple[str, str, int]:
    try:
        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        process = psutil.Process(pid)
        return process.name(), title, pid
    except Exception as exc:
        logging.debug("failed to inspect active window: %s", exc)
        return "Unknown", "Unknown", 0


def collect_activity_event(client_id: str) -> dict[str, Any]:
    process_name, window_title, pid = get_active_window_info()
    local_now = datetime.now()
    utc_now = datetime.now(timezone.utc)
    return {
        "event_id": uuid.uuid4().hex,
        "recorded_at": local_now.strftime("%Y-%m-%d %H:%M:%S"),
        "recorded_date": local_now.strftime("%Y-%m-%d"),
        "recorded_at_utc": utc_now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "client_id": client_id,
        "hostname": platform.node(),
        "username": getpass.getuser(),
        "process_name": process_name,
        "pid": pid,
        "window_title": window_title,
    }


def canonical_upload_message(
    *,
    method: str,
    path: str,
    client_id: str,
    timestamp: str,
    nonce: str,
    body_sha256: str,
) -> bytes:
    parts = [method.upper(), path, client_id, timestamp, nonce, body_sha256]
    return "\n".join(parts).encode("utf-8")


def sign_upload(
    *,
    key: bytes,
    method: str,
    path: str,
    client_id: str,
    timestamp: str,
    nonce: str,
    body_sha256: str,
) -> str:
    message = canonical_upload_message(
        method=method,
        path=path,
        client_id=client_id,
        timestamp=timestamp,
        nonce=nonce,
        body_sha256=body_sha256,
    )
    return base64.b64encode(hmac.new(key, message, hashlib.sha256).digest()).decode("ascii")


def encode_event_batch(client_id: str, events: list[dict[str, Any]]) -> bytes:
    payload = {
        "client_id": client_id,
        "sent_at_utc": utc_timestamp(),
        "events": events,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def post_event_batch(
    *,
    session: requests.Session,
    server_url: str,
    client_id: str,
    key: bytes,
    events: list[dict[str, Any]],
    timeout: int,
) -> bool:
    if not events:
        return True

    upload_path = "/api/v1/events"
    timestamp = utc_timestamp()
    nonce = uuid.uuid4().hex
    body = encode_event_batch(client_id, events)
    body_hash = hashlib.sha256(body).hexdigest()
    signature = sign_upload(
        key=key,
        method="POST",
        path=upload_path,
        client_id=client_id,
        timestamp=timestamp,
        nonce=nonce,
        body_sha256=body_hash,
    )

    headers = {
        "X-KidTime-Client": client_id,
        "X-KidTime-Timestamp": timestamp,
        "X-KidTime-Nonce": nonce,
        "X-KidTime-Body-SHA256": body_hash,
        "X-KidTime-Signature": signature,
        "Content-Type": "application/json; charset=utf-8",
    }

    url = server_url.rstrip("/") + upload_path
    try:
        response = session.post(url, headers=headers, data=body, timeout=timeout)
        response.raise_for_status()
        logging.info("uploaded %s event(s) to %s", len(events), url)
        return True
    except Exception as exc:
        logging.warning("failed to upload %s event(s): %s", len(events), exc)
        return False


def upload_with_backoff(
    *,
    session: requests.Session,
    server_url: str,
    client_id: str,
    key: bytes,
    events: list[dict[str, Any]],
    timeout: int,
    retries: int = 3,
) -> bool:
    max_attempts = retries + 1
    for attempt in range(1, max_attempts + 1):
        if post_event_batch(
            session=session,
            server_url=server_url,
            client_id=client_id,
            key=key,
            events=events,
            timeout=timeout,
        ):
            return True
        if attempt < max_attempts:
            delay = 2 ** (attempt - 1)
            logging.info("retry upload in %s second(s), attempt %s/%s", delay, attempt + 1, max_attempts)
            time.sleep(delay)
    return False


def find_pythonw() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        return str(exe)
    candidate = exe.with_name("pythonw.exe")
    if candidate.exists():
        return str(candidate)
    return str(exe)


def install_startup(script_path: Path, base_dir: Path) -> None:
    pythonw = find_pythonw()
    command = [
        "schtasks",
        "/Create",
        "/TN",
        TASK_NAME,
        "/TR",
        f'"{pythonw}" "{script_path}" --base-dir "{base_dir}"',
        "/SC",
        "ONLOGON",
        "/RL",
        "LIMITED",
        "/F",
    ]
    subprocess.run(command, check=True)

    watchdog_command = [
        "schtasks",
        "/Create",
        "/TN",
        f"{TASK_NAME}Watchdog",
        "/TR",
        f'"{pythonw}" "{script_path}" --ensure-running --base-dir "{base_dir}"',
        "/SC",
        "MINUTE",
        "/MO",
        "5",
        "/RL",
        "LIMITED",
        "/F",
    ]
    subprocess.run(watchdog_command, check=True)
    print(f"Installed scheduled tasks: {TASK_NAME}, {TASK_NAME}Watchdog")


def uninstall_startup() -> None:
    for task_name in (TASK_NAME, f"{TASK_NAME}Watchdog"):
        subprocess.run(["schtasks", "/Delete", "/TN", task_name, "/F"], check=False)
    print("Removed KidTime scheduled tasks if they existed.")


def is_client_running(current_pid: int) -> bool:
    script_name = Path(__file__).name.lower()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.info["pid"] == current_pid:
                continue
            cmdline = [str(part).lower() for part in (proc.info.get("cmdline") or [])]
            if script_name in " ".join(cmdline) and "--ensure-running" not in cmdline:
                return True
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return False


def ensure_running(script_path: Path, base_dir: Path) -> None:
    if is_client_running(os.getpid()):
        return
    pythonw = find_pythonw()
    subprocess.Popen(
        [pythonw, str(script_path), "--base-dir", str(base_dir)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def run_collector(args: argparse.Namespace) -> None:
    base_dir = Path(args.base_dir).expanduser().resolve()
    setup_logging(base_dir, verbose=args.verbose)
    key = shared_key_bytes(args.key_hex)
    pending_events: deque[dict[str, Any]] = deque(maxlen=args.queue_limit)
    session = requests.Session()
    session.trust_env = args.use_proxy
    next_log_at = 0.0

    logging.info(
        "KidTime client started. server=%s base_dir=%s queue_limit=%s use_proxy=%s",
        args.server_url,
        base_dir,
        args.queue_limit,
        args.use_proxy,
    )
    while True:
        now = time.monotonic()
        try:
            if now >= next_log_at:
                if len(pending_events) == pending_events.maxlen:
                    logging.warning("pending queue is full; dropping oldest event")
                pending_events.append(collect_activity_event(args.client_id))
                batch = list(pending_events)
                if upload_with_backoff(
                    session=session,
                    server_url=args.server_url,
                    client_id=args.client_id,
                    key=key,
                    events=batch,
                    timeout=args.request_timeout,
                ):
                    pending_events.clear()
                else:
                    logging.warning("kept %s pending event(s) in memory", len(pending_events))
                next_log_at = now + args.log_interval
        except Exception:
            logging.exception("collector loop failed")
        time.sleep(2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="KidTime Windows activity collector")
    parser.add_argument("--server-url", default=os.environ.get("KIDTIME_SERVER_URL", SERVER_URL))
    parser.add_argument("--client-id", default=os.environ.get("KIDTIME_CLIENT_ID", CLIENT_ID))
    parser.add_argument("--key-hex", default=os.environ.get("KIDTIME_SHARED_KEY_HEX", SHARED_KEY_HEX))
    parser.add_argument("--base-dir", default=os.environ.get("KIDTIME_BASE_DIR", str(default_base_dir())))
    parser.add_argument("--log-interval", type=int, default=LOG_INTERVAL_SECONDS)
    parser.add_argument("--queue-limit", type=int, default=int(os.environ.get("KIDTIME_QUEUE_LIMIT", QUEUE_LIMIT)))
    parser.add_argument("--request-timeout", type=int, default=int(os.environ.get("KIDTIME_REQUEST_TIMEOUT", REQUEST_TIMEOUT_SECONDS)))
    parser.add_argument("--use-proxy", action="store_true", help="allow requests to use proxy settings from the environment")
    parser.add_argument("--install-startup", action="store_true")
    parser.add_argument("--uninstall-startup", action="store_true")
    parser.add_argument("--ensure-running", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    script_path = Path(__file__).resolve()
    base_dir = Path(args.base_dir).expanduser().resolve()

    if args.install_startup:
        install_startup(script_path, base_dir)
        return 0
    if args.uninstall_startup:
        uninstall_startup()
        return 0
    if args.ensure_running:
        ensure_running(script_path, base_dir)
        return 0

    run_collector(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
