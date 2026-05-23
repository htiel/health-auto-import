# Health Auto Export → Home Assistant: Project Handoff

> **Purpose.** You are taking over a project to build a **HACS custom integration** that pulls Apple Health data into Home Assistant by polling the **Health Auto Export (HAE) iOS app's built-in TCP/MCP server**, replacing an unreliable MQTT-based pipeline. The integration powers a **Sickbay panel** in an LCARS-themed Lovelace dashboard.

---

## Read these documents in order

| # | File | What it is |
|---|------|------------|
| 1 | [`01-problem-history.md`](01-problem-history.md) | Why we abandoned the MQTT path. Reproduces the diagnosis so you know the constraints. **Read this first.** |
| 2 | [`02-hae-tcp-protocol.md`](02-hae-tcp-protocol.md) | Definitive protocol reference for HAE's TCP server — wire format, tool names, arguments, response shape, gotchas. |
| 3 | [`03-integration-spec.md`](03-integration-spec.md) | Functional/technical spec for the new HACS integration. Entity list, config flow, polling cadence, error handling. |
| 4 | [`04-reference-files.md`](04-reference-files.md) | Index of artifacts (logs, sample payloads, working probe scripts, reference HACS integrations in the workspace to model after). |
| 5 | [`samples/`](samples/) | Working probe scripts and example request/response JSON. |

---

## TL;DR for the new AI instance

1. **The MQTT path is broken** and unfixable from outside HAE. Don't try to patch it.
2. **The HAE TCP server at `192.168.1.203:9000` works perfectly** when queried directly. Returns clean JSON. We proved this by pulling **2.19 MB of ECG data** in one call.
3. The user wants a **HACS integration** that polls this server on a configurable schedule, exposes Home Assistant entities (sensors, binary_sensors), and notifies if the iPad becomes unreachable.
4. The dashboard consumer is an **LCARS Sickbay panel** that focuses on **medical/biomedical** data: ECG, heart rate, HR notifications, medications, vitals.
5. iPad must keep HAE in the foreground (HAE limitation). Use Guided Access / dock.

---

## What's already done

- ✅ HAE TCP protocol reverse-engineered (`callTool` envelope, MCP `content[0].text` payload shape).
- ✅ Working PowerShell probe (`samples/query.ps1`) confirms data availability for ECG, Workouts, Heart Notifications, Health Metrics.
- ✅ HealthKit permissions confirmed granted on the iPad (real data is returned).
- ✅ MQTT root cause identified (in-app serialization bug for ECG/Workouts, ~13 ms instant failure). See doc 01.

## What you'll build

- 🔲 A new HACS custom integration repo, domain name suggested: **`health_auto_export`**.
- 🔲 Config flow with manual `host` + `port` and (optional) Bonjour/zeroconf autodiscover.
- 🔲 `DataUpdateCoordinator`-based pull architecture, one coordinator per data type with independent intervals.
- 🔲 Entities listed in [`03-integration-spec.md`](03-integration-spec.md).
- 🔲 Reachability `binary_sensor` (`connectivity` device_class) so the user can wire a notification automation when the iPad goes offline.

---

## Target environment

- **Home Assistant** Core (current), with the user's existing custom_components living in `/config/custom_components/`.
- **HACS** is already installed in the workspace (see `custom_components/hacs/`).
- **Reference integrations to mirror style/conventions:**
  - `custom_components/beszel_api/` — simple `api.py` + coordinator + sensor + config_flow + binary_sensor pattern. Closest analog.
  - `custom_components/cardata/` — more sophisticated polling integration with a separate `container.py` for data shaping.

---

## Working environment notes (carry over)

- The user's HA config is mounted as a Windows SMB share at `\\homeassistant.local\config`.
- The Mosquitto broker addon is `core-mosquitto`, listening on **port 1883** of `homeassistant.local`. Credentials in [`04-reference-files.md`](04-reference-files.md). (Not needed for the integration — included only for context.)
- All staging artifacts (logs, raw exports, probe scripts) live at the user's `OneDrive\LCARS\Health\` folder. Paths are listed in doc 04.
- The user runs PowerShell on Windows; their VS Code workspace root is the HA config share.
- Terminal output truncation is common — when running diagnostic commands, redirect to a file and read it back.
