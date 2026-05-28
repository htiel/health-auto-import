"""On-demand HAE query services.

Exposes thin HA services that pass through to the HAE TCP server so the
LCARS dashboard (and any other UI) can fetch historical records on demand
without storing them locally. Apple Health remains the source of truth;
this integration acts purely as a proxy.

Services (all return ``ServiceResponse``):
 - health_auto_import.query           — generic tool query
 - health_auto_import.query_ecg
 - health_auto_import.query_workouts
 - health_auto_import.query_metrics
 - health_auto_import.query_medications
 - health_auto_import.query_heart_notifications

Each service forwards directly to ``HaeClient.call_tool`` with a caller-
specified date window. The latest-record sensors are untouched: their
watermark / dedup state is not advanced by service calls.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv

from .api import HaeClient, HaeProtocolError, HaeTransportError
from .const import (
    ALL_METRIC_IDS,
    DOMAIN,
    TOOL_ECG,
    TOOL_HEALTH_METRICS,
    TOOL_HEART_NOTIFICATIONS,
    TOOL_MEDICATIONS,
    TOOL_WORKOUTS,
)
from .coordinator import hae_ts

_LOGGER = logging.getLogger(__name__)

SERVICE_QUERY = "query"
SERVICE_QUERY_ECG = "query_ecg"
SERVICE_QUERY_WORKOUTS = "query_workouts"
SERVICE_QUERY_METRICS = "query_metrics"
SERVICE_QUERY_MEDICATIONS = "query_medications"
SERVICE_QUERY_HEART_NOTIFICATIONS = "query_heart_notifications"

DEFAULT_LOOKBACK_DAYS = 30
MAX_LOOKBACK_DAYS = 365 * 7
MAX_LIMIT = 500

_BASE_SCHEMA = {
    vol.Optional("start"): cv.datetime,
    vol.Optional("end"): cv.datetime,
    vol.Optional("days"): vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_LOOKBACK_DAYS)),
    vol.Optional("limit"): vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_LIMIT)),
}

QUERY_SCHEMA = vol.Schema({
    vol.Required("tool"): cv.string,
    vol.Optional("arguments"): dict,
    **_BASE_SCHEMA,
})

QUERY_ECG_SCHEMA = vol.Schema(_BASE_SCHEMA)
QUERY_WORKOUTS_SCHEMA = vol.Schema({
    vol.Optional("include_routes", default=True): cv.boolean,
    vol.Optional("include_metadata", default=True): cv.boolean,
    **_BASE_SCHEMA,
})
QUERY_METRICS_SCHEMA = vol.Schema({
    vol.Optional("metrics"): vol.Any(cv.string, [cv.string]),
    **_BASE_SCHEMA,
})
QUERY_MEDICATIONS_SCHEMA = vol.Schema(_BASE_SCHEMA)
QUERY_HRN_SCHEMA = vol.Schema(_BASE_SCHEMA)


def _resolve_window(
    call: ServiceCall,
) -> tuple[dt.datetime, dt.datetime]:
    """Resolve (start, end) tz-aware UTC datetimes from service call fields.

    Precedence: explicit ``start``/``end`` > ``days`` lookback > 30-day default.
    """
    now = dt.datetime.now(dt.timezone.utc)
    end = call.data.get("end")
    start = call.data.get("start")
    days = call.data.get("days")

    if end is None:
        end = now
    if start is None:
        if days is not None:
            start = end - dt.timedelta(days=int(days))
        else:
            start = end - dt.timedelta(days=DEFAULT_LOOKBACK_DAYS)

    if start.tzinfo is None:
        start = start.replace(tzinfo=dt.timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=dt.timezone.utc)
    if start >= end:
        raise HomeAssistantError(f"start ({start}) must be before end ({end})")
    return start, end


def _pick_client(hass: HomeAssistant) -> HaeClient:
    """Return the first registered HaeClient (single-entry integration)."""
    entries = hass.data.get(DOMAIN, {})
    for bundle in entries.values():
        client = bundle.get("client")
        if isinstance(client, HaeClient):
            return client
    raise HomeAssistantError("health_auto_import is not configured")


async def _do_call(
    hass: HomeAssistant,
    tool: str,
    arguments: dict[str, Any],
    limit: int | None,
) -> ServiceResponse:
    client = _pick_client(hass)
    try:
        data = await client.call_tool(tool, arguments)
    except (HaeTransportError, HaeProtocolError) as err:
        raise HomeAssistantError(
            f"HAE query for tool '{tool}' failed: {err}"
        ) from err

    # Locate the records list for trimming / count.
    records: list[Any] | None = None
    record_key: str | None = None
    if isinstance(data, dict):
        for key in (tool, "ecg", "workouts", "metrics", "medications",
                    "heartRateNotifications", "heart_notifications"):
            v = data.get(key)
            if isinstance(v, list):
                records, record_key = v, key
                break

    if records is not None and limit and len(records) > limit:
        # Records are returned oldest→newest by the server; keep most recent.
        records = records[-limit:]
        if isinstance(data, dict) and record_key is not None:
            data = {**data, record_key: records}

    return {
        "tool": tool,
        "count": len(records) if records is not None else 0,
        "arguments": arguments,
        "data": data,
    }


def _normalise_metrics(value: Any) -> str:
    """Accept list or comma string of metric IDs; return CSV the server expects."""
    if value is None or value == "":
        return ",".join(ALL_METRIC_IDS)
    if isinstance(value, list):
        ids = [str(v).strip() for v in value if str(v).strip()]
        return ",".join(ids) if ids else ",".join(ALL_METRIC_IDS)
    return str(value)


async def _handle_query(call: ServiceCall) -> ServiceResponse:
    start, end = _resolve_window(call)
    args: dict[str, Any] = {
        "start": hae_ts(start),
        "end": hae_ts(end),
        **(call.data.get("arguments") or {}),
    }
    return await _do_call(call.hass, call.data["tool"], args, call.data.get("limit"))


async def _handle_query_ecg(call: ServiceCall) -> ServiceResponse:
    start, end = _resolve_window(call)
    return await _do_call(
        call.hass,
        TOOL_ECG,
        {"start": hae_ts(start), "end": hae_ts(end)},
        call.data.get("limit"),
    )


async def _handle_query_workouts(call: ServiceCall) -> ServiceResponse:
    start, end = _resolve_window(call)
    args: dict[str, Any] = {"start": hae_ts(start), "end": hae_ts(end)}
    if call.data.get("include_metadata", True):
        args["includeMetadata"] = True
    if call.data.get("include_routes", True):
        args["includeRoutes"] = True
    return await _do_call(call.hass, TOOL_WORKOUTS, args, call.data.get("limit"))


async def _handle_query_metrics(call: ServiceCall) -> ServiceResponse:
    start, end = _resolve_window(call)
    args = {
        "start": hae_ts(start),
        "end": hae_ts(end),
        "metrics": _normalise_metrics(call.data.get("metrics")),
    }
    return await _do_call(call.hass, TOOL_HEALTH_METRICS, args, call.data.get("limit"))


async def _handle_query_medications(call: ServiceCall) -> ServiceResponse:
    start, end = _resolve_window(call)
    return await _do_call(
        call.hass,
        TOOL_MEDICATIONS,
        {"start": hae_ts(start), "end": hae_ts(end)},
        call.data.get("limit"),
    )


async def _handle_query_hrn(call: ServiceCall) -> ServiceResponse:
    start, end = _resolve_window(call)
    return await _do_call(
        call.hass,
        TOOL_HEART_NOTIFICATIONS,
        {"start": hae_ts(start), "end": hae_ts(end)},
        call.data.get("limit"),
    )


def async_register_services(hass: HomeAssistant) -> None:
    """Register query services. Idempotent."""
    if hass.services.has_service(DOMAIN, SERVICE_QUERY):
        return
    hass.services.async_register(
        DOMAIN, SERVICE_QUERY, _handle_query,
        schema=QUERY_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_QUERY_ECG, _handle_query_ecg,
        schema=QUERY_ECG_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_QUERY_WORKOUTS, _handle_query_workouts,
        schema=QUERY_WORKOUTS_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_QUERY_METRICS, _handle_query_metrics,
        schema=QUERY_METRICS_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_QUERY_MEDICATIONS, _handle_query_medications,
        schema=QUERY_MEDICATIONS_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_QUERY_HEART_NOTIFICATIONS, _handle_query_hrn,
        schema=QUERY_HRN_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
    _LOGGER.debug("Registered HAE query services")


def async_unregister_services(hass: HomeAssistant) -> None:
    """Unregister query services. Safe to call even if not registered."""
    for svc in (
        SERVICE_QUERY,
        SERVICE_QUERY_ECG,
        SERVICE_QUERY_WORKOUTS,
        SERVICE_QUERY_METRICS,
        SERVICE_QUERY_MEDICATIONS,
        SERVICE_QUERY_HEART_NOTIFICATIONS,
    ):
        hass.services.async_remove(DOMAIN, svc)
