"""Config flow for Health Auto Import.

Security:
 - Host is validated against a strict hostname/IP pattern (no SSRF via
   crafted URLs, no shell metacharacters).
 - Port is range-checked (1–65535).
 - Probe errors are caught and surfaced generically (no stack trace leak).
 - Subnet scan is limited to /24 with a hard cap and short timeouts.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT

from .api import HaeClient, HaeError
from .const import DEFAULT_PORT, DOMAIN, HOSTNAME_PATTERN, MAX_PORT, MIN_PORT

_LOGGER = logging.getLogger(__name__)

# Scan settings
_SCAN_TIMEOUT_S = 1.0  # per-host TCP connect timeout
_SCAN_CONCURRENCY = 30  # max parallel probes


def _validate_host(value: str) -> str:
    """Strip whitespace, reject empty or dangerous hostnames."""
    value = value.strip()
    if not value:
        raise vol.Invalid("Host must not be empty")
    if len(value) > 253:
        raise vol.Invalid("Host name too long")
    if not re.match(HOSTNAME_PATTERN, value):
        raise vol.Invalid("Invalid characters in host name")
    return value


def _validate_port(value: int) -> int:
    """Ensure port is in the valid TCP range."""
    if not isinstance(value, int) or not MIN_PORT <= value <= MAX_PORT:
        raise vol.Invalid(f"Port must be between {MIN_PORT} and {MAX_PORT}")
    return value


def _get_local_ip() -> str | None:
    """Return the primary local IPv4 address, or None."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return None


async def _probe_host(host: str, port: int, sem: asyncio.Semaphore) -> str | None:
    """Try to TCP-connect to host:port. Returns the host if reachable."""
    async with sem:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=_SCAN_TIMEOUT_S,
            )
            writer.close()
            await writer.wait_closed()
            return host
        except (OSError, asyncio.TimeoutError):
            return None


async def scan_subnet(port: int) -> list[str]:
    """Scan the local /24 subnet for hosts with *port* open."""
    local_ip = await asyncio.get_event_loop().run_in_executor(None, _get_local_ip)
    if local_ip is None:
        return []
    try:
        network = ipaddress.IPv4Network(f"{local_ip}/24", strict=False)
    except ValueError:
        return []

    sem = asyncio.Semaphore(_SCAN_CONCURRENCY)
    tasks = [
        _probe_host(str(addr), port, sem)
        for addr in network.hosts()
        if str(addr) != local_ip
    ]
    results = await asyncio.gather(*tasks)
    return sorted(h for h in results if h is not None)


# Sentinel values for the pick menu.
_MANUAL_ENTRY = "__manual__"
_SCAN_AGAIN = "__scan__"


class HaeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Scan-first config flow with manual fallback."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise mutable state for multi-step flow."""
        self._discovered: list[str] = []

    # ------------------------------------------------------------------
    # Step 1: scan or manual?
    # ------------------------------------------------------------------
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Entry point — scan the subnet, then show results or manual form."""
        self._discovered = await scan_subnet(DEFAULT_PORT)
        if self._discovered:
            return await self.async_step_pick()
        # Nothing found — go straight to manual entry.
        return await self.async_step_manual()

    # ------------------------------------------------------------------
    # Step 2a: pick from discovered servers
    # ------------------------------------------------------------------
    async def async_step_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            chosen = user_input.get(CONF_HOST)
            if chosen == _MANUAL_ENTRY:
                return await self.async_step_manual()
            if chosen == _SCAN_AGAIN:
                return await self.async_step_user()

            host = chosen
            port = DEFAULT_PORT

            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()

            # Verify the chosen server is actually HAE.
            client = HaeClient(host, port)
            try:
                reachable = await client.probe()
            except HaeError:
                reachable = False

            if not reachable:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=f"Health Auto Import ({host})",
                    data={CONF_HOST: host, CONF_PORT: port},
                )

        options = {h: h for h in self._discovered}
        options[_SCAN_AGAIN] = "⟳ Scan again"
        options[_MANUAL_ENTRY] = "Enter IP address manually…"

        schema = vol.Schema(
            {vol.Required(CONF_HOST): vol.In(options)}
        )
        return self.async_show_form(
            step_id="pick", data_schema=schema, errors=errors
        )

    # ------------------------------------------------------------------
    # Step 2b: manual host/port entry (fallback)
    # ------------------------------------------------------------------
    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                host = _validate_host(user_input[CONF_HOST])
            except vol.Invalid:
                errors[CONF_HOST] = "invalid_host"
                host = user_input[CONF_HOST]

            try:
                port = _validate_port(user_input.get(CONF_PORT, DEFAULT_PORT))
            except vol.Invalid:
                errors[CONF_PORT] = "invalid_port"
                port = DEFAULT_PORT

            if not errors:
                await self.async_set_unique_id(f"{host}:{port}")
                self._abort_if_unique_id_configured()

                client = HaeClient(host, port)
                try:
                    reachable = await client.probe()
                except HaeError:
                    reachable = False

                if not reachable:
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_create_entry(
                        title=f"Health Auto Import ({host})",
                        data={CONF_HOST: host, CONF_PORT: port},
                    )

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
            }
        )
        return self.async_show_form(
            step_id="manual", data_schema=schema, errors=errors
        )

    # ------------------------------------------------------------------
    # Reconfigure: edit host/port of an existing entry without removing it.
    # ------------------------------------------------------------------
    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the IP/port of an existing config entry."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        current_host = entry.data.get(CONF_HOST, "")
        current_port = entry.data.get(CONF_PORT, DEFAULT_PORT)

        if user_input is not None:
            try:
                host = _validate_host(user_input[CONF_HOST])
            except vol.Invalid:
                errors[CONF_HOST] = "invalid_host"
                host = user_input[CONF_HOST]

            try:
                port = _validate_port(user_input.get(CONF_PORT, DEFAULT_PORT))
            except vol.Invalid:
                errors[CONF_PORT] = "invalid_port"
                port = DEFAULT_PORT

            if not errors:
                # Allow keeping the same host:port (no-op reconfigure is fine).
                # Only block if the new host:port collides with a *different* entry.
                new_uid = f"{host}:{port}"
                for other in self._async_current_entries():
                    if other.entry_id == entry.entry_id:
                        continue
                    if other.unique_id == new_uid:
                        return self.async_abort(reason="already_configured")

                client = HaeClient(host, port)
                try:
                    reachable = await client.probe()
                except HaeError:
                    reachable = False

                if not reachable:
                    errors["base"] = "cannot_connect"
                else:
                    await self.async_set_unique_id(new_uid)
                    return self.async_update_reload_and_abort(
                        entry,
                        unique_id=new_uid,
                        title=f"Health Auto Import ({host})",
                        data={**entry.data, CONF_HOST: host, CONF_PORT: port},
                    )

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=current_host): str,
                vol.Optional(CONF_PORT, default=current_port): int,
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "current_host": current_host,
                "current_port": str(current_port),
            },
        )
