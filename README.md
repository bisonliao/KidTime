# KidTime

KidTime is a lightweight Windows foreground-process monitor with an Alibaba
Cloud Linux upload server. The Windows client records one foreground-process
event per minute and uploads incremental JSON batches. The server verifies an
HMAC-SHA256 signature before appending rows to one CSV per client/day.

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
- `kidtimeSrv.py`: Alibaba Cloud Linux 3 upload server.
- `install_cli.ps1`: Windows client installer.
- `install_srv.sh`: Linux server installer dispatcher.
- `install_srv_rpm.sh`: Alibaba Cloud Linux / RHEL-like systemd installer.
- `install_srv_deb.sh`: Debian / Ubuntu systemd installer.
- `install_srv_ubuntu.sh`: Ubuntu alias that calls `install_srv_deb.sh`.
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

## Install the server on Alibaba Cloud Linux 3

The target server can be Alibaba Cloud Linux 3 / OpenAnolis Edition:

```bash
cat /etc/os-release
```

The RPM installer uses `dnf` or `yum`, creates a Python virtual environment, and
requires Python 3.8 or newer. Copy this project to the server, then run:

```bash
export KIDTIME_SHARED_KEY_HEX="replace_with_64_hex_chars"
export KIDTIME_PORT=8001
export KIDTIME_DATA_DIR=/var/lib/kidtime
chmod +x install_srv.sh install_srv_rpm.sh
./install_srv_rpm.sh
```

`install_srv.sh` can also auto-detect the distribution and call the right
installer.

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

On Alibaba Cloud Linux, also check firewalld if it is enabled:

```bash
sudo firewall-cmd --permanent --add-port=8001/tcp
sudo firewall-cmd --reload
```

For Alibaba Cloud ECS, the cloud security group must allow inbound TCP `8001`
from the monitored Windows machines. If `pip install` is slow or blocked, use a
PyPI mirror while running the installer:

```bash
PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ ./install_srv_rpm.sh
```

## Install the server on Debian or Ubuntu

For Debian or Ubuntu hosts, use the apt-based installer:

```bash
export KIDTIME_SHARED_KEY_HEX="replace_with_64_hex_chars"
export KIDTIME_PORT=8001
export KIDTIME_DATA_DIR=/var/lib/kidtime
chmod +x install_srv.sh install_srv_deb.sh install_srv_ubuntu.sh
./install_srv_deb.sh
```

`install_srv_ubuntu.sh` is an alias for the same Debian-family installer.

## Install the client on Windows 11

Run PowerShell as Administrator in the project directory:

```powershell
Unblock-File .\install_cli.ps1, .\uninstall_cli.ps1
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process
.\install_cli.ps1 `
  -ServerUrl "http://8.148.226.47:8001" `
  -ClientId "$env:COMPUTERNAME" `
  -SharedKeyHex "replace_with_64_hex_chars"
```

The installer copies files to `C:\ProgramData\KidTime`, grants standard users
write access to the client log directory, installs dependencies, sets
machine-level environment variables, and creates two Scheduled Tasks for the
built-in `Users` group:

- `KidTimeMonitor`: starts the client when any standard user logs on, using
  `pythonw.exe`.
- `KidTimeMonitorWatchdog`: starts on any standard user logon and checks every
  five minutes that the client is still running.

The tasks start on user logon rather than machine startup because
foreground-window collection needs the logged-in user's interactive desktop
session. A startup task running as `SYSTEM` would run before that desktop exists
and can report `Unknown` or the wrong session. Registering the tasks for the
`Users` group lets one administrator install cover whichever standard account
logs in later. It is still not impossible for an administrator or
endpoint-management policy to disable scheduled tasks, so operations should
monitor missing uploads on the server.

Client outputs:

```text
C:\ProgramData\KidTime\kidtimeCli.log
```

Useful client commands:

```powershell
python C:\ProgramData\KidTime\kidtimeCli.py --ensure-running
.\uninstall_cli.ps1
Get-ScheduledTask -TaskName KidTimeMonitor, KidTimeMonitorWatchdog
(Get-ScheduledTask -TaskName KidTimeMonitor).Principal
Get-Content C:\ProgramData\KidTime\kidtimeCli.log -Tail 100 -Wait
```

If a short black console window appears every few minutes, reinstall the client
tasks with the latest script. Older task definitions may have used `python.exe`
for the watchdog; the current installer uses `pythonw.exe` for both scheduled
tasks.

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
Alibaba Cloud Linux clocks synchronized with NTP.

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
