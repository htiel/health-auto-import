"""Config flow for Health Auto Import.

Security:
 - Host is validated against a strict hostname/IP pattern (no SSRF via
   crafted URLs, no shell metacharacters).
 - Port is range-checked (1–65535).
 - Probe errors are caught and surfaced generically (no stack trace leak).
"""
from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT

from .api import HaeClient, HaeError
from .const import DEFAULT_PORT, DOMAIN, HOSTNAME_PATTERN, MAX_PORT, MIN_PORT


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


class HaeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Manual host/port + connection test. Zeroconf discovery deferred to v0.2."""

    VERSION = 1

    async def async_step_user(
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
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
