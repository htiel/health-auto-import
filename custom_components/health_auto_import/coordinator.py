"""DataUpdateCoordinator plumbing for Health Auto Import.

Coordinators:
 - ReachabilityCoordinator  — 30 s TCP probe, drives binary_sensor
 - ToolCoordinator          — one per discovered tool, drives sensors
 - DiscoveryCoordinator     — one-shot on setup, nightly refresh

Security:
 - All server-returned strings sanitised before use in entity IDs (safe_slug).
 - Record counts capped at MAX_RECORDS_PER_RESPONSE.
 - Watermark timestamps validated (no future dates injected by a rogue server).
 - Dedup LRU bounded at DEDUP_LRU_SIZE.
"""
from __future__ import annotations

import datetime as dt
from collections import OrderedDict
from datetime import timedelta
import logging
import re
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .api import HaeClient, HaeError, HaeProtocolError, HaeTransportError
from .const import (
    ALL_METRIC_IDS,
    DEDUP_LRU_SIZE,
    DOMAIN,
    HAE_TS_FORMAT,
    INTERVAL_REACHABILITY_S,
    MAX_METRICS,
    MAX_RECORDS_PER_RESPONSE,
    MAX_SLUG_INPUT_LEN,
    MAX_TOOLS,
    OVERLAP_DENSE_S,
    OVERLAP_SPARSE_S,
    RPC_ERR_INVALID_PARAMS,
    SEED_WINDOW_DAYS,
    SPARSE_TOOLS,
    TOOL_HEALTH_METRICS,
    TOOL_INTERVALS,
    TOOL_WORKOUTS,
)

_LOGGER = logging.getLogger(__name__)

# Regex for safe slug characters — only lowercase alphanum + underscore.
_SAFE_SLUG_RE = re.compile(r"[^a-z0-9_]")


def safe_slug(raw: str, *, max_len: int = MAX_SLUG_INPUT_LEN) -> str:
    """Sanitise a server-returned string into a safe entity-ID fragment.

    Prevents injection of path separators, shell metacharacters, or absurdly
    long strings into HA's entity registry.
    """
    truncated = raw[:max_len].strip().lower().replace(" ", "_")
    return _SAFE_SLUG_RE.sub("", truncated) or "unknown"


def hae_ts(value: dt.datetime) -> str:
    """Format a tz-aware datetime in HAE's ``yyyy-MM-dd HH:mm:ss ±HHMM`` shape."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.strftime(HAE_TS_FORMAT)


def parse_hae_ts(raw: str) -> dt.datetime | None:
    """Parse a HAE timestamp, returning None on garbage."""
    try:
        return dt.datetime.strptime(raw.strip(), HAE_TS_FORMAT)
    except (ValueError, AttributeError):
        return None


def _clamp_ts(ts: dt.datetime | None) -> dt.datetime | None:
    """Reject future timestamps from a rogue server."""
    if ts is None:
        return None
    now = dt_util.utcnow()
    if ts > now + timedelta(hours=1):
        _LOGGER.warning("Rejected future timestamp %s from server", ts)
        return None
    return ts


class _DedupLRU:
    """Bounded ordered-dict used as an LRU dedup cache per tool."""

    def __init__(self, maxsize: int = DEDUP_LRU_SIZE) -> None:
        self._data: OrderedDict[str, None] = OrderedDict()
        self._maxsize = maxsize

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def add(self, key: str) -> None:
        if key in self._data:
            self._data.move_to_end(key)
            return
        self._data[key] = None
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)

    def export(self) -> list[str]:
        return list(self._data.keys())

    def load(self, keys: list[str]) -> None:
        for k in keys[-self._maxsize :]:
            self._data[k] = None


class WatermarkState:
    """Mutable watermark + dedup state for one tool, serialisable to config entry."""

    def __init__(self) -> None:
        self.initial_crawl_done: bool = False
        self.watermark: dt.datetime | None = None
        self.last_success_at: dt.datetime | None = None
        self.dedup = _DedupLRU()

    def to_dict(self) -> dict[str, Any]:
        return {
            "initial_crawl_done": self.initial_crawl_done,
            "watermark": self.watermark.isoformat() if self.watermark else None,
            "last_success_at": (
                self.last_success_at.isoformat() if self.last_success_at else None
            ),
            "seen_keys": self.dedup.export(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WatermarkState:
        ws = cls()
        ws.initial_crawl_done = bool(data.get("initial_crawl_done", False))
        wm_raw = data.get("watermark")
        if isinstance(wm_raw, str):
            try:
                ws.watermark = dt.datetime.fromisoformat(wm_raw)
            except ValueError:
                pass
        ls_raw = data.get("last_success_at")
        if isinstance(ls_raw, str):
            try:
                ws.last_success_at = dt.datetime.fromisoformat(ls_raw)
            except ValueError:
                pass
        keys = data.get("seen_keys")
        if isinstance(keys, list):
            ws.dedup.load([k for k in keys if isinstance(k, str)])
        return ws


# ---------------------------------------------------------------------------
# Reachability
# ---------------------------------------------------------------------------


class ReachabilityCoordinator(DataUpdateCoordinator[bool]):
    """Lightweight TCP-probe coordinator. Drives binary_sensor."""

    def __init__(self, hass: HomeAssistant, client: HaeClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}.reachability",
            update_interval=timedelta(seconds=INTERVAL_REACHABILITY_S),
        )
        self.client = client

    async def _async_update_data(self) -> bool:
        try:
            return await self.client.probe()
        except HaeTransportError as err:
            raise UpdateFailed(str(err)) from err
        except HaeError as err:
            raise UpdateFailed(str(err)) from err


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class DiscoveryResult:
    """Immutable snapshot of a discovery run."""

    def __init__(
        self,
        tools: list[str],
        metrics: list[str],
        discovered_at: dt.datetime,
    ) -> None:
        self.tools = tools
        self.metrics = metrics
        self.discovered_at = discovered_at


async def run_discovery(client: HaeClient) -> DiscoveryResult:
    """Execute tool + metric discovery against a live server.

    Security:
     - Tool count capped at MAX_TOOLS.
     - Metric count capped at MAX_METRICS.
     - Tool names sanitised before storage.
    """
    # 1. Tool catalog.
    raw_tools = await client.list_tools()
    if raw_tools is None:
        raw_tools = [{"name": t} for t in ("ecg", "workouts", "heart_notifications",
                                            "health_metrics", "medications")]
    # Cap and sanitise.
    tool_names: list[str] = []
    for t in raw_tools[:MAX_TOOLS]:
        name = t.get("name", "") if isinstance(t, dict) else str(t)
        name = safe_slug(name)
        if name and name != "listtools" and name != "unknown":
            tool_names.append(name)

    # 2. Probe each tool to confirm reachable.
    confirmed: list[str] = []
    now = dt_util.utcnow()
    probe_end = hae_ts(now)
    probe_start = hae_ts(now - timedelta(hours=1))
    for name in tool_names:
        try:
            if name == TOOL_HEALTH_METRICS:
                args: dict[str, Any] = {
                    "metrics": "heart_rate",
                    "start": probe_start,
                    "end": probe_end,
                }
            elif name == TOOL_WORKOUTS:
                args = {
                    "start": probe_start,
                    "end": probe_end,
                    "includeMetadata": False,
                    "includeRoutes": False,
                }
            else:
                args = {"start": probe_start, "end": probe_end}
            await client.call_tool(name, args)
            confirmed.append(name)
        except HaeProtocolError as exc:
            if exc.code in (RPC_ERR_INVALID_PARAMS, -32601):
                _LOGGER.debug("Tool %s not available: %s", name, exc)
            else:
                _LOGGER.warning("Unexpected error probing tool %s: %s", name, exc)
        except HaeTransportError as exc:
            _LOGGER.warning("Transport error probing tool %s: %s", name, exc)
            break  # Server probably went down — stop probing.

    # 3. Metric discovery.
    metrics: list[str] = []
    if TOOL_HEALTH_METRICS in confirmed:
        try:
            metrics_start = hae_ts(now - timedelta(days=30))
            all_ids = ",".join(ALL_METRIC_IDS)
            data = await client.call_tool(
                TOOL_HEALTH_METRICS,
                {"metrics": all_ids, "start": metrics_start, "end": probe_end},
            )
            raw_metrics = data.get("metrics") or data.get(TOOL_HEALTH_METRICS) or []
            if isinstance(raw_metrics, list):
                for m in raw_metrics[:MAX_METRICS]:
                    if isinstance(m, dict) and isinstance(m.get("name"), str):
                        metrics.append(safe_slug(m["name"]))
        except HaeError as exc:
            _LOGGER.warning("Metric discovery failed: %s", exc)

    return DiscoveryResult(
        tools=confirmed,
        metrics=metrics,
        discovered_at=now,
    )


# ---------------------------------------------------------------------------
# Per-tool coordinator
# ---------------------------------------------------------------------------


def _dedup_key_for(tool: str, record: dict[str, Any]) -> str | None:
    """Compute a dedup key for a record. Returns None if key can't be built."""
    start = record.get("start", "")
    if tool == "ecg":
        return f"ecg:{start}"
    if tool == "heart_notifications":
        kind = record.get("type", record.get("kind", ""))
        return f"hrn:{start}:{kind}"
    if tool == "workouts":
        name = record.get("name", "")
        return f"wk:{start}:{name}"
    if tool == "medications":
        # Prefer RxNorm code for stability.
        codings = record.get("codings") or []
        rxn = ""
        for c in codings:
            if isinstance(c, dict) and "rxnorm" in (c.get("system") or "").lower():
                rxn = c.get("code", "")
                break
        slug = rxn or safe_slug(record.get("displayText", "unknown"))
        sched = record.get("scheduledDate", "")
        return f"med:{slug}:{sched}"
    # health_metrics handled separately per metric.
    return None


def _dedup_key_metric(metric_name: str, point: dict[str, Any]) -> str:
    date = point.get("date", "")
    source = point.get("source", "")
    return f"hm:{metric_name}:{date}:{source}"


class ToolCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for a single HAE tool.

    Manages its own watermark, dedup LRU, and polling interval.
    Emits the latest parsed data for consumption by sensor entities.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: HaeClient,
        *,
        tool_name: str,
        watermark_state: WatermarkState,
    ) -> None:
        interval = TOOL_INTERVALS.get(tool_name, 300)
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}.{tool_name}",
            update_interval=timedelta(seconds=interval),
        )
        self.client = client
        self.tool_name = tool_name
        self.wm = watermark_state
        # Accumulated newest records for sensors to read.
        self.latest_records: list[dict[str, Any]] = []
        self.consecutive_failures: int = 0

    def _build_args(
        self, start: dt.datetime, end: dt.datetime
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "start": hae_ts(start),
            "end": hae_ts(end),
        }
        if self.tool_name == TOOL_WORKOUTS:
            args["includeMetadata"] = False
            args["includeRoutes"] = False
        elif self.tool_name == TOOL_HEALTH_METRICS:
            args["metrics"] = ",".join(ALL_METRIC_IDS)
        return args

    def _overlap(self) -> timedelta:
        if self.tool_name == TOOL_HEALTH_METRICS:
            return timedelta(seconds=OVERLAP_DENSE_S)
        return timedelta(seconds=OVERLAP_SPARSE_S)

    def _extract_records(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Pull the record list out of the tool-specific response shape."""
        if not data:
            return []
        # health_metrics nests under "metrics" key as a list of metric-buckets.
        if self.tool_name == TOOL_HEALTH_METRICS:
            raw = data.get("metrics") or data.get("health_metrics") or []
            if isinstance(raw, list):
                return raw[:MAX_RECORDS_PER_RESPONSE]
            return []
        # Most tools: data itself is the list, or data[tool_name] is.
        tool_data = data.get(self.tool_name)
        if isinstance(tool_data, list):
            return tool_data[:MAX_RECORDS_PER_RESPONSE]
        if isinstance(data, dict) and not tool_data:
            # Might be the legacy shape where data IS the list wrapper.
            for v in data.values():
                if isinstance(v, list):
                    return v[:MAX_RECORDS_PER_RESPONSE]
        return []

    async def _async_update_data(self) -> dict[str, Any]:
        """Poll the tool, apply watermark + dedup, return parsed payload."""
        now = dt_util.utcnow()

        # Compute query window.
        if self.wm.watermark is not None:
            window_start = self.wm.watermark - self._overlap()
        elif self.tool_name in SPARSE_TOOLS:
            # Sparse tool with no watermark yet — seed with 30 days.
            window_start = now - timedelta(days=SEED_WINDOW_DAYS * 4)
        else:
            # Dense tool — seed with SEED_WINDOW_DAYS.
            window_start = now - timedelta(days=SEED_WINDOW_DAYS)

        try:
            data = await self.client.call_tool(
                self.tool_name, self._build_args(window_start, now)
            )
        except HaeTransportError as err:
            self.consecutive_failures += 1
            raise UpdateFailed(str(err)) from err
        except HaeProtocolError as err:
            self.consecutive_failures += 1
            raise UpdateFailed(str(err)) from err

        # Reset failure counter on success.
        self.consecutive_failures = 0
        self.wm.last_success_at = now

        # Dedup and collect new records.
        records = self._extract_records(data)
        new_records: list[dict[str, Any]] = []

        if self.tool_name == TOOL_HEALTH_METRICS:
            # Each record is a metric bucket with "data" array of points.
            for metric_bucket in records:
                metric_name = metric_bucket.get("name", "")
                points = metric_bucket.get("data")
                if not isinstance(points, list):
                    continue
                new_points = []
                for pt in points[:MAX_RECORDS_PER_RESPONSE]:
                    if not isinstance(pt, dict):
                        continue
                    key = _dedup_key_metric(metric_name, pt)
                    if key not in self.wm.dedup:
                        self.wm.dedup.add(key)
                        new_points.append(pt)
                        # Advance watermark from point's date.
                        pt_ts = _clamp_ts(parse_hae_ts(pt.get("date", "")))
                        if pt_ts and (
                            self.wm.watermark is None or pt_ts > self.wm.watermark
                        ):
                            self.wm.watermark = pt_ts
                if new_points:
                    new_records.append({
                        "name": metric_name,
                        "units": metric_bucket.get("units", ""),
                        "data": new_points,
                    })
        else:
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                key = _dedup_key_for(self.tool_name, rec)
                if key is None:
                    continue
                if key in self.wm.dedup:
                    continue
                self.wm.dedup.add(key)
                new_records.append(rec)
                # Advance watermark.
                end_raw = rec.get("end") or rec.get("start") or ""
                rec_ts = _clamp_ts(parse_hae_ts(end_raw))
                if rec_ts and (
                    self.wm.watermark is None or rec_ts > self.wm.watermark
                ):
                    self.wm.watermark = rec_ts

        if new_records:
            self.latest_records = new_records

        return {
            "tool": self.tool_name,
            "new_count": len(new_records),
            "total_ingested": len(self.wm.dedup.export()),
            "watermark": self.wm.watermark.isoformat() if self.wm.watermark else None,
            "records": self.latest_records,
        }
