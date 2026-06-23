# KidTime

KidTime is a lightweight Windows foreground-process monitor with an Ubuntu upload
server. The Windows client records one foreground-process event per minute and
uploads incremental JSON batches. The server verifies an HMAC-SHA256 signature
before appending rows to one CSV per client/day.

## Security model

- Authentication uses a shared 256-bit key represented as a 64-character hex
  string.
- The client signs: HTTP method, request path, client id, UTC timestamp, nonce,
  and request body SHA-256.
- The server validates timestamp skew, rejects replayed nonce values, compares
  file hash, and checks the signature with constant-time comparison.
- The key is never sent over the network.

Important: the default URL is plain HTTP. HMAC prevents unauthorized upload and
tampering, but it does not encrypt window titles or process names. For production
or untrusted networks, expose the service through HTTPS, a VPN, WireGuard, or an
SSH tunnel.

## Files

- `kidtimeCli.py`: Windows 11 client.
- `kidtimeSrv.py`: Ubuntu 24 upload server.
- `install_cli.ps1`: Windows client installer.
- `install_srv.sh`: Ubuntu systemd installer.
- `requirements-cli.txt`: client dependencies.
- `requirements-srv.txt`: server dependencies.

## Generate the shared key

Generate one key and use the same value on every client and the server:

```bash
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

Example format:

```text
0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
```

Do not keep the placeholder key from the source files in production.

## Install the server on Ubuntu 24

Copy this project to the Ubuntu server, then run:

```bash
export KIDTIME_SHARED_KEY_HEX="replace_with_64_hex_chars"
export KIDTIME_PORT=8001
export KIDTIME_DATA_DIR=/var/lib/kidtime
chmod +x install_srv.sh
./install_srv.sh
```

The installer creates:

- `/opt/kidtime`: application and virtual environment.
- `/var/lib/kidtime`: uploaded CSV and metadata files.
- `/etc/kidtime.env`: service environment, including the shared key.
- `kidtime.service`: systemd unit with `Restart=always`.

Useful operations:

```bash
sudo systemctl status kidtime.service --no-pager
sudo journalctl -u kidtime.service -f
curl http://127.0.0.1:8001/healthz
curl -H "X-KidTime-Admin-Token: $(sudo grep KIDTIME_ADMIN_TOKEN /etc/kidtime.env | cut -d= -f2-)" \
  http://127.0.0.1:8001/api/v1/clients
```

Uploaded files are stored as:

```text
/var/lib/kidtime/<client_id>/<yyyy-mm-dd>.csv
/var/lib/kidtime/<client_id>/state.json
```

Open TCP port `8001` on the server firewall or cloud security group if clients
connect directly.

## Install the client on Windows 11

Run PowerShell as Administrator in the project directory:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process
.\install_cli.ps1 `
  -ServerUrl "http://8.148.226.47:8001" `
  -ClientId "$env:COMPUTERNAME" `
  -SharedKeyHex "replace_with_64_hex_chars"
```

The installer copies files to `C:\ProgramData\KidTime`, installs dependencies,
sets machine-level environment variables, and creates two Scheduled Tasks:

- `KidTimeMonitor`: starts the client on user logon with `pythonw.exe`.
- `KidTimeMonitorWatchdog`: runs every five minutes and starts the client if it
  is not already running.

The main task starts on user logon, because foreground-window collection needs an
interactive desktop session. The watchdog task runs every five minutes and
restarts the client if it is missing. This is more reliable than a simple
registry `Run` item because Windows Task Scheduler is designed for managed
background jobs and is less likely to be removed by ordinary startup-app cleanup
tools. It is still not impossible for an administrator or endpoint-management
policy to disable it, so operations should monitor missing uploads on the server.

Client outputs:

```text
C:\ProgramData\KidTime\kidtimeCli.log
```

Useful client commands:

```powershell
python C:\ProgramData\KidTime\kidtimeCli.py --ensure-running
python C:\ProgramData\KidTime\kidtimeCli.py --uninstall-startup
Get-ScheduledTask -TaskName KidTimeMonitor, KidTimeMonitorWatchdog
Get-Content C:\ProgramData\KidTime\kidtimeCli.log -Tail 100 -Wait
```

## Manual run

Server:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-srv.txt
export KIDTIME_SHARED_KEY_HEX="replace_with_64_hex_chars"
python kidtimeSrv.py --host 0.0.0.0 --port 8001 --data-dir ./data
```

Client:

```powershell
python -m pip install -r requirements-cli.txt
$env:KIDTIME_SHARED_KEY_HEX = "replace_with_64_hex_chars"
$env:KIDTIME_SERVER_URL = "http://8.148.226.47:8001"
python .\kidtimeCli.py --base-dir "$env:ProgramData\KidTime" --verbose
```

By default, the client bypasses local proxy settings for monitoring uploads by
setting `requests.Session.trust_env = False`. This avoids common VPN/proxy
setups that route GitHub traffic through a proxy while the monitoring server is
reachable only through the domestic network. If an environment really needs a
proxy for this service, start the client with `--use-proxy`.

## Signature details

Client request:

- `POST /api/v1/events`
- JSON body contains `client_id`, `sent_at_utc`, and an `events` array.
- Every event includes `event_id`, `recorded_at`, `recorded_date`, and
  `recorded_at_utc`.

Client headers:

- `X-KidTime-Client`: client id.
- `X-KidTime-Timestamp`: UTC timestamp in `yyyy-mm-ddTHH:MM:SSZ`.
- `X-KidTime-Nonce`: random per-upload nonce.
- `X-KidTime-Body-SHA256`: SHA-256 of JSON request body bytes.
- `X-KidTime-Signature`: base64 HMAC-SHA256.

Canonical message:

```text
POST
/api/v1/events
<client_id>
<timestamp>
<nonce>
<body_sha256>
```

The server accepts timestamps within 300 seconds by default. Keep Windows and
Ubuntu clocks synchronized with NTP.

## Notes and limitations

- Foreground-window collection requires an interactive Windows user session.
  If no user is logged in, the active window may be reported as `Unknown`.
- The client uploads one new event per minute. If upload fails, it retries three
  additional times with short exponential backoff, then keeps pending events in
  memory and sends them with the next successful batch. The in-memory queue
  defaults to 100 events; oldest pending events are dropped when the queue is
  full.
- The server keeps a short-lived in-memory `event_id` cache to reduce duplicate
  CSV rows when a client times out after the server has already written the
  batch.
- If a single shared key is used for all clients, compromise of one client key
  compromises upload authentication for all clients. For stronger isolation,
  extend the server to keep one key per client id.
- Window titles can contain sensitive data. Limit access to the server data
  directory and use transport encryption where possible.
