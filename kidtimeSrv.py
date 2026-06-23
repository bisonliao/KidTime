#!/usr/bin/env python3
"""
KidTime upload server.

Run behind systemd on Ubuntu. The service accepts signed incremental events,
verifies HMAC-SHA256 authentication, appends rows to one CSV per client/day,
and exposes health and last-seen information for operations.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse


# Must match kidtimeCli.py. Prefer overriding with KIDTIME_SHARED_KEY_HEX.
SHARED_KEY_HEX = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
DATA_DIR = "/var/lib/kidtime"
MAX_UPLOAD_BYTES = 1024 * 1024
MAX_BATCH_EVENTS = 100
SIGNATURE_TTL_SECONDS = 300
NONCE_TTL_SECONDS = 600
CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


app = FastAPI(title="KidTime Server", version="1.0.0")
settings: dict[str, Any] = {}
seen_nonces: dict[str, float] = {}
client_state: dict[str, dict[str, Any]] = {}
seen_event_ids: dict[str, float] = {}


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def shared_key_bytes(key_hex: str) -> bytes:
    try:
        key = bytes.fromhex(key_hex)
    except ValueError as exc:
        raise ValueError("shared key must be a 64-character hex string") from exc
    if len(key) != 32:
        raise ValueError("shared key must decode to 32 bytes / 256 bits")
    return key


def parse_utc_timestamp(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="invalid timestamp") from exc


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


def expected_signature(
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


def cleanup_seen_state(now: float) -> None:
    expired = [nonce for nonce, expires_at in seen_nonces.items() if expires_at <= now]
    for nonce in expired:
        seen_nonces.pop(nonce, None)

    expired_events = [event_id for event_id, expires_at in seen_event_ids.items() if expires_at <= now]
    for event_id in expired_events:
        seen_event_ids.pop(event_id, None)


def validate_headers(
    request: Request,
    *,
    client_id: str,
    timestamp: str,
    nonce: str,
    body_sha256: str,
    signature: str,
    actual_sha256: str,
) -> None:
    if not CLIENT_ID_PATTERN.fullmatch(client_id):
        raise HTTPException(status_code=401, detail="invalid client id")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", body_sha256):
        raise HTTPException(status_code=401, detail="invalid body hash")
    if not nonce or len(nonce) > 128:
        raise HTTPException(status_code=401, detail="invalid nonce")
    if body_sha256.lower() != actual_sha256:
        raise HTTPException(status_code=401, detail="body hash mismatch")

    upload_time = parse_utc_timestamp(timestamp)
    now_dt = datetime.now(timezone.utc)
    skew = abs((now_dt - upload_time).total_seconds())
    if skew > settings["signature_ttl_seconds"]:
        raise HTTPException(status_code=401, detail="expired signature")

    now = time.time()
    cleanup_seen_state(now)
    nonce_key = f"{client_id}:{nonce}"
    if nonce_key in seen_nonces:
        raise HTTPException(status_code=401, detail="replayed nonce")

    expected = expected_signature(
        key=settings["shared_key"],
        method=request.method,
        path=request.url.path,
        client_id=client_id,
        timestamp=timestamp,
        nonce=nonce,
        body_sha256=body_sha256.lower(),
    )
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="bad signature")
    seen_nonces[nonce_key] = now + settings["nonce_ttl_seconds"]


def client_dir(client_id: str) -> Path:
    return settings["data_dir"] / client_id


CSV_COLUMNS = [
    "event_id",
    "recorded_at",
    "recorded_date",
    "recorded_at_utc",
    "received_at_utc",
    "client_id",
    "hostname",
    "username",
    "process_name",
    "pid",
    "window_title",
]


def sanitize_cell(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text.replace("\r", " ").replace("\n", " ")


def normalize_event(client_id: str, event: dict[str, Any], received_at: str) -> dict[str, str]:
    event_id = str(event.get("event_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", event_id):
        raise HTTPException(status_code=400, detail="invalid event_id")
    recorded_date = str(event.get("recorded_date") or "")
    if not DATE_PATTERN.fullmatch(recorded_date):
        raise HTTPException(status_code=400, detail="invalid event recorded_date")
    event_client_id = str(event.get("client_id") or "")
    if event_client_id and event_client_id != client_id:
        raise HTTPException(status_code=400, detail="event client id mismatch")

    row = {
        "event_id": event_id,
        "recorded_at": event.get("recorded_at"),
        "recorded_date": recorded_date,
        "recorded_at_utc": event.get("recorded_at_utc"),
        "received_at_utc": received_at,
        "client_id": client_id,
        "hostname": event.get("hostname"),
        "username": event.get("username"),
        "process_name": event.get("process_name"),
        "pid": event.get("pid"),
        "window_title": event.get("window_title"),
    }
    return {key: sanitize_cell(row.get(key)) for key in CSV_COLUMNS}


def append_events(client_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    target_dir = client_dir(client_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    received_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    written_by_date: dict[str, int] = {}
    cleanup_seen_state(time.time())

    for event in events:
        row = normalize_event(client_id, event, received_at)
        event_key = f"{client_id}:{row['event_id']}"
        if event_key in seen_event_ids:
            continue
        seen_event_ids[event_key] = time.time() + settings["event_id_ttl_seconds"]
        log_date = row["recorded_date"]
        target = target_dir / f"{log_date}.csv"
        file_exists = target.exists()
        with target.open("a", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
        written_by_date[log_date] = written_by_date.get(log_date, 0) + 1

    metadata = {
        "client_id": client_id,
        "last_received_at": received_at,
        "last_batch_count": len(events),
        "last_written_count": sum(written_by_date.values()),
        "written_by_date": written_by_date,
    }
    (target_dir / "state.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    client_state[client_id] = metadata
    return metadata


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": True, "clients": len(client_state)}


@app.get("/api/v1/clients")
def clients(x_kidtime_admin_token: str | None = Header(default=None, alias="X-KidTime-Admin-Token")) -> dict[str, Any]:
    admin_token = settings.get("admin_token")
    if not admin_token:
        raise HTTPException(status_code=403, detail="admin token is not configured")
    if not x_kidtime_admin_token or not hmac.compare_digest(admin_token, x_kidtime_admin_token):
        raise HTTPException(status_code=401, detail="bad admin token")
    return {"clients": client_state}


@app.post("/api/v1/events")
async def events(
    request: Request,
    x_kidtime_client: str = Header(..., alias="X-KidTime-Client"),
    x_kidtime_timestamp: str = Header(..., alias="X-KidTime-Timestamp"),
    x_kidtime_nonce: str = Header(..., alias="X-KidTime-Nonce"),
    x_kidtime_body_sha256: str = Header(..., alias="X-KidTime-Body-SHA256"),
    x_kidtime_signature: str = Header(..., alias="X-KidTime-Signature"),
) -> JSONResponse:
    data = await request.body()
    if len(data) > settings["max_upload_bytes"]:
        raise HTTPException(status_code=413, detail="payload too large")

    actual_sha256 = hashlib.sha256(data).hexdigest()
    validate_headers(
        request,
        client_id=x_kidtime_client,
        timestamp=x_kidtime_timestamp,
        nonce=x_kidtime_nonce,
        body_sha256=x_kidtime_body_sha256,
        signature=x_kidtime_signature,
        actual_sha256=actual_sha256,
    )

    try:
        payload = json.loads(data.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc

    if payload.get("client_id") != x_kidtime_client:
        raise HTTPException(status_code=400, detail="payload client id mismatch")
    events_payload = payload.get("events")
    if not isinstance(events_payload, list) or not events_payload:
        raise HTTPException(status_code=400, detail="events must be a non-empty list")
    if len(events_payload) > settings["max_batch_events"]:
        raise HTTPException(status_code=413, detail="too many events")
    if not all(isinstance(item, dict) for item in events_payload):
        raise HTTPException(status_code=400, detail="each event must be an object")

    metadata = append_events(x_kidtime_client, events_payload)
    logging.info("stored events client=%s count=%s", x_kidtime_client, len(events_payload))
    return JSONResponse({"ok": True, "count": len(events_payload), "sha256": actual_sha256, "state": metadata})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="KidTime upload server")
    parser.add_argument("--host", default=os.environ.get("KIDTIME_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("KIDTIME_PORT", "8001")))
    parser.add_argument("--data-dir", default=os.environ.get("KIDTIME_DATA_DIR", DATA_DIR))
    parser.add_argument("--key-hex", default=os.environ.get("KIDTIME_SHARED_KEY_HEX", SHARED_KEY_HEX))
    parser.add_argument("--max-upload-bytes", type=int, default=int(os.environ.get("KIDTIME_MAX_UPLOAD_BYTES", MAX_UPLOAD_BYTES)))
    parser.add_argument("--max-batch-events", type=int, default=int(os.environ.get("KIDTIME_MAX_BATCH_EVENTS", MAX_BATCH_EVENTS)))
    parser.add_argument("--signature-ttl-seconds", type=int, default=SIGNATURE_TTL_SECONDS)
    parser.add_argument("--nonce-ttl-seconds", type=int, default=NONCE_TTL_SECONDS)
    parser.add_argument("--event-id-ttl-seconds", type=int, default=int(os.environ.get("KIDTIME_EVENT_ID_TTL_SECONDS", "86400")))
    parser.add_argument("--admin-token", default=os.environ.get("KIDTIME_ADMIN_TOKEN", ""))
    parser.add_argument("--verbose", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    setup_logging(args.verbose)
    data_dir = Path(args.data_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    settings.update(
        {
            "data_dir": data_dir,
            "shared_key": shared_key_bytes(args.key_hex),
            "max_upload_bytes": args.max_upload_bytes,
            "max_batch_events": args.max_batch_events,
            "signature_ttl_seconds": args.signature_ttl_seconds,
            "nonce_ttl_seconds": args.nonce_ttl_seconds,
            "event_id_ttl_seconds": args.event_id_ttl_seconds,
            "admin_token": args.admin_token,
        }
    )
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
