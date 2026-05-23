# 04 — Reference Files & Artifacts

## In this `HealthHandoff/` folder

| Path | What it is |
|---|---|
| [`README.md`](README.md) | Overview and read-order. |
| [`01-problem-history.md`](01-problem-history.md) | Why MQTT was abandoned. |
| [`02-hae-tcp-protocol.md`](02-hae-tcp-protocol.md) | Wire-format reference. |
| [`03-integration-spec.md`](03-integration-spec.md) | Functional spec for the new integration. |
| [`04-reference-files.md`](04-reference-files.md) | (This file.) |
| [`samples/query.ps1`](samples/query.ps1) | Working PowerShell probe — proves the TCP path works and demonstrates the legacy `callTool` envelope. |
| [`samples/example-responses.md`](samples/example-responses.md) | Real JSON snippets from `ecg`, `workouts`, `heart_notifications`, `health_metrics`. |

## On the user's local OneDrive (full diagnostic dataset)

Path root: `C:\Users\leithma\OneDrive\LCARS\Health\`

| File | What it is |
|---|---|
| `_query.ps1` | Source of `samples/query.ps1`. |
| `_query.out` | Full output of running the probe — preserves response sizes (ECG = 2.19 MB). |
| `_sub.py` | Python paho-mqtt subscriber used to capture broker-side `$SYS` log proving HAE never sent PUBLISH packets. |
| `_sub.log` | Live capture from above, showing the `New connection ... AppleHealth-Leith-wo` / `disconnected: connection closed by client` pattern with zero `leith/health/workouts` messages between them. |
| `events.jsonl` | HAE event log export, latest. Shows `mqtt_upload_outcome error MQTTPublishError` ~13 ms after `mqtt_upload_foreground`. |
| `events-prev.jsonl` | Larger HAE event log (2 MB). |
| `automations.json` | HAE automation dump showing the 4 MQTT automations and their settings. Source for the metric ID list in `02-hae-tcp-protocol.md`. |
| `manifest.json` | HAE export bundle manifest — useful for confirming HAE app version `9.0.9 / 20260519.2` and iPad model `iPad13,4 / iPadOS 26.5`. |
| `_summary.out` | Tabular summary of HAE event-log triggers per automation, highlighting the failure pattern. |
| `_broker.creds` | Local credentials file for the Mosquitto broker. **Format:** 4 lines — host / port / username / password. (For reference only; the new integration does not need MQTT.) |

> If the new repo lives under version control, **do not** check in `_broker.creds`, `events*.jsonl` (contains personal health data), or `automations.json` (contains the same).

## In the Home Assistant config workspace

Path root (SMB): `\\homeassistant.local\config\`

| Path | What it is |
|---|---|
| `custom_components/beszel_api/` | **Closest analog to model after.** Simple polling integration: `manifest.json`, `api.py`, `coordinator.py`-style logic in `__init__.py`, `sensor.py`, `binary_sensor.py`, `config_flow.py`, `update.py`. ~10 files total. |
| `custom_components/cardata/` | More advanced reference: separate `container.py` for data shaping, `descriptor_titles.py` for friendly names, multi-platform (sensor/binary_sensor/device_tracker), services.yaml. Useful when scaling beyond v1. |
| `custom_components/hacs/` | HACS itself — confirms HACS is installed. |
| `themes/` | LCARS theme location (relevant for the consuming dashboard, not for the integration). |
| `configuration.yaml`, `automations.yaml`, `scripts.yaml` | Existing HA config. The new integration is UI-configured; no YAML required. |

## External references

- Health Auto Export website: https://www.healthyapps.dev/  (developer's product page)
- HAE iOS App Store: https://apps.apple.com/us/app/health-auto-export-json-csv/id1115567069
- HAE GitHub-style docs / community (search for "Health Auto Export MCP server" / "Health Auto Export JSON-RPC") — official docs are sparse; this handoff and the probe script are the most complete public reference.
- Home Assistant integration scaffolding: https://developers.home-assistant.io/docs/creating_integration_file_structure
- `DataUpdateCoordinator` docs: https://developers.home-assistant.io/docs/integration_fetching_data#coordinated-single-api-poll-for-data-for-all-entities
- HACS publishing guide: https://hacs.xyz/docs/publish/start
- zeroconf in HA: https://developers.home-assistant.io/docs/creating_integration_manifest#zeroconf

## Quick-start commands for the new AI

When the new agent picks this up against a fresh repo:

```bash
# 1. Verify you can reach the HAE server from wherever the agent is running
#    (or have the human run this once and paste the output)
$ python -c "import socket; s=socket.socket(); s.settimeout(5); s.connect(('192.168.1.203', 9000)); print('connected'); s.close()"

# 2. Reproduce a known-good call
$ python -c "
import socket, json, datetime as dt
now = dt.datetime.now(dt.timezone(dt.timedelta(hours=-7)))
start = now - dt.timedelta(days=7)
fmt = '%Y-%m-%d %H:%M:%S %z'
req = json.dumps({
    'jsonrpc':'2.0','id':'1','method':'callTool',
    'params':{'name':'health_metrics','arguments':{
        'start': start.strftime(fmt), 'end': now.strftime(fmt),
        'metrics': 'step_count,heart_rate', 'interval': 'days', 'aggregate': True}}
}) + '\n'
s = socket.socket(); s.settimeout(30); s.connect(('192.168.1.203', 9000))
s.sendall(req.encode()); buf = b''
while True:
    chunk = s.recv(65536)
    if not chunk: break
    buf += chunk
    try: json.loads(buf.decode().strip()); break
    except Exception: pass
s.close()
print(buf.decode()[:500])
"
```

If both succeed → start scaffolding the integration. If they fail → ask the human to confirm HAE is foregrounded on the iPad.
