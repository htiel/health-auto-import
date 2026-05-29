"""Integration setup/teardown for Health Auto Import.

Security:
 - No secrets stored in ``hass.data`` (only client/coordinator references).
 - Discovery results sanitised via ``safe_slug`` before storage.
 - Options-update listener tears down cleanly (no orphan coordinators).
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .api import HaeClient
from .const import CONF_HOST, CONF_PORT, DEFAULT_PORT, DOMAIN, OPT_IN_TOOLS, TOOL_HEALTH_METRICS
from .coordinator import (
    ReachabilityCoordinator,
    ToolCoordinator,
    WatermarkState,
    run_discovery,
    safe_slug,
)
from .services import async_register_services, async_unregister_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Health Auto Import from a config entry."""
    host: str = entry.data[CONF_HOST]
    port: int = entry.data.get(CONF_PORT, DEFAULT_PORT)

    client = HaeClient(host, port)

    # 1. Reachability coordinator (always present — but skip first_refresh;
    #    discovery below will prove reachability and populate last_success).
    reachability = ReachabilityCoordinator(hass, client)

    # 2. Discovery — list tools + metric inventory.
    coordinators: dict[str, ToolCoordinator] = {}
    discovered_metrics: list[str] = []
    reachable = False

    try:
        discovery = await run_discovery(client)
        reachable = True
        _LOGGER.info(
            "Discovered %d tools and %d metrics on %s:%d",
            len(discovery.tools),
            len(discovery.metrics),
            host,
            port,
        )
        discovered_metrics = discovery.metrics

        # Fall back to previously persisted metrics if discovery found none
        # (e.g. server was down during the health_metrics enumeration call).
        if not discovered_metrics:
            saved_metrics = entry.options.get("discovered_metrics", [])
            if saved_metrics:
                _LOGGER.info(
                    "Metric discovery returned 0 metrics — using %d persisted metrics",
                    len(saved_metrics),
                )
                discovered_metrics = saved_metrics

        # Load persisted watermarks (if any) from config entry options.
        saved_wm: dict[str, dict] = entry.options.get("watermarks", {})
        saved_records: dict[str, list] = entry.options.get("latest_records", {})

        # 3. Create one coordinator per discovered tool.
        for tool_name in discovery.tools:
            if tool_name in OPT_IN_TOOLS:
                # Skip opt-in tools unless enabled by user.
                if not entry.options.get(f"enable_{tool_name}", False):
                    continue
            wm = WatermarkState.from_dict(saved_wm.get(tool_name, {}))
            coord = ToolCoordinator(
                hass,
                client,
                tool_name=tool_name,
                watermark_state=wm,
            )
            # Restore persisted sensor data so sensors have values on
            # restart even when the HAE server is offline.
            tool_records = saved_records.get(tool_name, [])
            if isinstance(tool_records, list):
                coord.restore_records(tool_records)
            # Wire up new-metric detection for health_metrics.
            if tool_name == "health_metrics":
                coord.known_metrics = {
                    safe_slug(m) for m in discovered_metrics
                }
                coord.config_entry = entry
            coordinators[tool_name] = coord

        # health_metrics: blocking first-refresh (drives metric discovery).
        hm = coordinators.get(TOOL_HEALTH_METRICS)
        if hm:
            try:
                await hm.async_config_entry_first_refresh()
            except Exception:  # noqa: BLE001
                _LOGGER.warning(
                    "Initial refresh failed for health_metrics "
                    "— will retry on next poll",
                )

        # Remaining coordinators: non-blocking background refresh.
        _others = [c for n, c in coordinators.items()
                   if n != TOOL_HEALTH_METRICS]
        if _others:

            async def _bg_first_refresh(
                coords: list[ToolCoordinator],
            ) -> None:
                for c in coords:
                    try:
                        await c.async_refresh()
                    except Exception:  # noqa: BLE001
                        _LOGGER.debug(
                            "Background refresh deferred for %s",
                            c.tool_name,
                        )

            entry.async_create_background_task(
                hass,
                _bg_first_refresh(_others),
                f"hai-deferred-init-{entry.entry_id}",
            )
    except Exception:  # noqa: BLE001
        _LOGGER.warning(
            "Discovery failed for %s:%d — restoring from persisted state",
            host,
            port,
        )
        # Server is unreachable but we may have persisted state from
        # a previous successful session.  Create coordinators from
        # persisted data so sensors keep their last-known values.
        saved_wm = entry.options.get("watermarks", {})
        saved_records = entry.options.get("latest_records", {})
        saved_metrics = entry.options.get("discovered_metrics", [])

        # Reconstruct tool list from persisted watermarks + records keys.
        persisted_tools = set(saved_wm.keys()) | set(saved_records.keys())
        if persisted_tools:
            discovered_metrics = saved_metrics
            for tool_name in persisted_tools:
                if tool_name in OPT_IN_TOOLS:
                    if not entry.options.get(f"enable_{tool_name}", False):
                        continue
                wm = WatermarkState.from_dict(saved_wm.get(tool_name, {}))
                coord = ToolCoordinator(
                    hass,
                    client,
                    tool_name=tool_name,
                    watermark_state=wm,
                )
                tool_records = saved_records.get(tool_name, [])
                if isinstance(tool_records, list):
                    coord.restore_records(tool_records)
                if tool_name == "health_metrics":
                    coord.known_metrics = {
                        safe_slug(m) for m in discovered_metrics
                    }
                    coord.config_entry = entry
                coordinators[tool_name] = coord
            _LOGGER.info(
                "Restored %d coordinators from persisted state",
                len(coordinators),
            )

    # Seed reachability from discovery result to avoid an extra probe.
    reachability.data = reachable
    await reachability.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "reachability": reachability,
        "coordinators": coordinators,
        "discovered_metrics": discovered_metrics,
        # Snapshot the connection params we set up with; the update
        # listener compares against these to decide whether a reload
        # is actually needed (saving latest_records during unload
        # also fires the listener but must NOT trigger a reload).
        "connection": (entry.data.get(CONF_HOST), entry.data.get(CONF_PORT, DEFAULT_PORT)),
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    async_register_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry — persist watermarks and sensor data before teardown."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if data:
        # Save watermarks, discovered metrics, and latest_records before unloading.
        coordinators = data.get("coordinators", {})
        wm_data = {
            name: coord.wm.to_dict() for name, coord in coordinators.items()
        }
        records_data: dict[str, list] = {}
        for name, coord in coordinators.items():
            if coord.latest_records:
                records_data[name] = coord.latest_records
        metrics = data.get("discovered_metrics", [])
        new_options = {
            **entry.options,
            "watermarks": wm_data,
            "discovered_metrics": metrics,
            "latest_records": records_data,
        }
        hass.config_entries.async_update_entry(entry, options=new_options)

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data.get(DOMAIN):
            async_unregister_services(hass)
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload only when the connection (host/port) actually changes.

    The unload path stores 18 MB of ``latest_records`` via
    ``async_update_entry(..., options=...)``, which also fires this
    listener. Reloading on that would deadlock against the in-progress
    unload — so we compare against the connection snapshot taken at
    setup time and bail out for option-only changes.
    """
    bundle = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
    prev = bundle.get("connection")
    curr = (entry.data.get(CONF_HOST), entry.data.get(CONF_PORT, DEFAULT_PORT))
    if prev == curr:
        return
    await hass.config_entries.async_reload(entry.entry_id)
