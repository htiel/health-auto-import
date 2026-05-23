# 05 — New AI Bootstrap Prompt

Copy-paste this whole block into the first message you give the new AI instance once it's pointed at the new GitHub repo. It assumes the agent has access to read the files in this `HealthHandoff/` directory (either via a checked-in copy or a shared mount).

---

> **You are inheriting an in-progress project.** The user wants to build a HACS custom integration that pulls Apple Health data from the Health Auto Export (HAE) iOS app's built-in TCP server, replacing an unreliable MQTT-based pipeline. The integration powers a Sickbay panel in an LCARS-themed Home Assistant Lovelace dashboard.
>
> **Step 1.** Read the handoff documents *in this order*, in full:
>
> 1. `HealthHandoff/README.md`
> 2. `HealthHandoff/01-problem-history.md` — why MQTT was abandoned
> 3. `HealthHandoff/02-hae-tcp-protocol.md` — the wire protocol you'll implement against
> 4. `HealthHandoff/03-integration-spec.md` — the functional spec, entity list, config flow
> 5. `HealthHandoff/04-reference-files.md` — pointers to logs, samples, and analog integrations
> 6. `HealthHandoff/samples/query.py` and `samples/query.ps1` — working reference implementations
> 7. `HealthHandoff/samples/example-responses.md` — real JSON shapes you must parse
>
> **Step 2.** Confirm you can reach the HAE server. Ask the user to run `python HealthHandoff/samples/query.py` and paste the output. If it fails (`CONNECT_FAIL`), the user needs to wake the iPad and foreground the HAE app — stop and ask before proceeding.
>
> **Step 3.** Scaffold the integration at `custom_components/health_auto_export/` per the directory layout in spec section "Repository layout (target)". Model conventions on the existing `custom_components/beszel_api/` integration (small, polling, single-API, multi-platform). Do **not** copy from `cardata` — its architecture is more than you need.
>
> **Step 4.** Implement in this order:
> 1. `const.py` (domain, default ports, default intervals, metric IDs)
> 2. `api.py` (async TCP client wrapping `asyncio.open_connection`; one method per HAE tool; `extract_payload` helper per spec)
> 3. `config_flow.py` (manual host/port + connection test against a tiny health_metrics call)
> 4. `coordinator.py` (one coordinator class per data type as listed in spec)
> 5. `entity.py` (shared base — common device_info etc.)
> 6. `binary_sensor.py` (reachability + workout-in-progress)
> 7. `sensor.py` (ECG / workouts / HRN / per-metric)
> 8. `__init__.py` (entry setup/unload, options-flow reload listener)
> 9. `manifest.json`, `strings.json`, `translations/en.json`, `hacs.json`
>
> **Step 5.** Add `tests/` covering the API client against canned responses from `samples/example-responses.md`. Use `pytest-homeassistant-custom-component`.
>
> **Step 6.** Write `README.md` for the public repo: install via HACS Custom Repository, config flow walk-through, dashboard example using the `binary_sensor.hae_reachable` notification pattern from the spec.
>
> **Working agreements:**
> - Implement, don't ask permission for each file — only ask if the spec is ambiguous on a specific decision.
> - Don't add cycle tracking, state of mind, or symptoms entities in v1.
> - Don't include raw ECG voltage in the entity state — it's a config option that defaults OFF.
> - Use `async`/`await` throughout; no synchronous `socket` calls in coordinator code.
> - Per-call timeout: 30 s. Read timeout: 30 s. Connect timeout: 10 s.
> - Mark medication-related entities `unavailable` if the `medications` tool returns a "method not found"-style JSON-RPC error.
> - Target HA Core 2024.11 or newer.

---

## Optional: companion Lovelace cards

After the integration is shipped, the user wants Sickbay panel cards consuming the new entities. That's a separate workstream; suggest scaffolding a small `lcars-sickbay-cards` repo with:
- A "vitals strip" card using `binary_sensor.hae_reachable` for the indicator light.
- An ECG card showing latest classification + count.
- A workout card showing in-progress / last workout.
- A heart-rate-notifications card with high/low/irregular counters.

But hold off until the integration is stable on real data.
