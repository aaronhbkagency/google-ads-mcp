"""Server-side write guards: customer allowlist and budget limits.

These checks run on the server and do not depend on the model behaving well.

Environment variables:
    GOOGLE_ADS_MCP_WRITE_ALLOWLIST: Comma-separated customer IDs that mutate
        tools may write to (hyphens allowed). If unset or empty, every mutate
        tool call is refused (fail closed).
    GOOGLE_ADS_MCP_MAX_DAILY_BUDGET: Largest daily budget, in the account's
        currency, that a tool may set. If unset, tools that set budgets refuse
        to run (fail closed).
    GOOGLE_ADS_MCP_MAX_BUDGET_INCREASE_PCT: Optional. Largest percentage
        increase allowed on an existing budget in one call (e.g. "50").
    GOOGLE_ADS_MCP_MAX_CPC: Optional. Highest max CPC bid a tool may set, in the
        account's currency. If unset, GOOGLE_ADS_MCP_MAX_DAILY_BUDGET is used as
        the ceiling; with neither set, bid changes are refused.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from mcp import types as mcp_types

from ads_mcp import utils

logger = logging.getLogger(__name__)

ALLOWLIST_ENV = "GOOGLE_ADS_MCP_WRITE_ALLOWLIST"
MAX_DAILY_BUDGET_ENV = "GOOGLE_ADS_MCP_MAX_DAILY_BUDGET"
MAX_BUDGET_INCREASE_PCT_ENV = "GOOGLE_ADS_MCP_MAX_BUDGET_INCREASE_PCT"
MAX_CPC_ENV = "GOOGLE_ADS_MCP_MAX_CPC"

MICROS_PER_UNIT = 1_000_000


def get_write_allowlist() -> frozenset[str]:
    """Returns the set of customer IDs (digits only) that may be written to."""
    raw = os.environ.get(ALLOWLIST_ENV, "")
    ids = {utils.clean_customer_id(part) for part in raw.split(",")}
    return frozenset(i for i in ids if i)


def require_write_allowed(customer_id: str | int | None) -> str:
    """Raises ToolError unless customer_id is on the write allowlist.

    Returns:
        The cleaned (digits-only) customer ID.
    """
    allowlist = get_write_allowlist()
    if not allowlist:
        raise ToolError(
            "Error: Writes are disabled on this server because "
            f"{ALLOWLIST_ENV} is not set. No changes were made."
        )
    cleaned = utils.clean_customer_id(customer_id or "")
    if not cleaned:
        raise ToolError("Error: 'customer_id' must contain digits.")
    if cleaned not in allowlist:
        raise ToolError(
            f"Error: Customer ID {cleaned} is not on this server's write "
            "allowlist, so changes to it are refused. No changes were made. "
            "Ask the server administrator to add it if this is intended."
        )
    return cleaned


def _get_float_env(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ToolError(
            f"Error: Server misconfiguration: {name}={raw!r} is not a number."
        ) from exc
    if value <= 0:
        raise ToolError(
            f"Error: Server misconfiguration: {name} must be greater than 0."
        )
    return value


def to_micros(amount: float) -> int:
    """Converts an amount in account currency to micros, rounded to cents."""
    return int(round(round(float(amount), 2) * MICROS_PER_UNIT))


def from_micros(amount_micros: int) -> float:
    return round(amount_micros / MICROS_PER_UNIT, 2)


def check_daily_budget(amount: float) -> int:
    """Validates a daily budget against the server cap.

    Returns:
        The budget in micros.
    """
    if amount is None or float(amount) <= 0:
        raise ToolError("Error: The daily budget must be greater than 0.")
    cap = _get_float_env(MAX_DAILY_BUDGET_ENV)
    if cap is None:
        raise ToolError(
            "Error: Budget changes are disabled on this server because "
            f"{MAX_DAILY_BUDGET_ENV} is not set. No changes were made."
        )
    if float(amount) > cap:
        raise ToolError(
            f"Error: A daily budget of {float(amount):.2f} is above this "
            f"server's cap of {cap:.2f} (account currency). No changes were "
            "made."
        )
    return to_micros(amount)


def check_cpc_bid(amount: float) -> int:
    """Validates a max CPC bid against GOOGLE_ADS_MCP_MAX_CPC.

    When GOOGLE_ADS_MCP_MAX_CPC is unset, the daily budget cap is the ceiling
    (a bid above a whole day's budget is never intended). With neither set,
    bid changes are refused.

    Returns:
        The bid in micros.
    """
    if amount is None or float(amount) <= 0:
        raise ToolError("Error: A CPC bid must be greater than 0.")
    cap = _get_float_env(MAX_CPC_ENV)
    cap_name = MAX_CPC_ENV
    if cap is None:
        cap = _get_float_env(MAX_DAILY_BUDGET_ENV)
        cap_name = MAX_DAILY_BUDGET_ENV
    if cap is None:
        raise ToolError(
            "Error: Bid changes are disabled on this server because neither "
            f"{MAX_CPC_ENV} nor {MAX_DAILY_BUDGET_ENV} is set. No changes "
            "were made."
        )
    if float(amount) > cap:
        raise ToolError(
            f"Error: A CPC bid of {float(amount):.2f} is above this server's "
            f"cap of {cap:.2f} ({cap_name}). No changes were made."
        )
    return to_micros(amount)


def check_budget_increase(old_micros: int, new_micros: int) -> None:
    """Refuses an increase larger than GOOGLE_ADS_MCP_MAX_BUDGET_INCREASE_PCT."""
    max_pct = _get_float_env(MAX_BUDGET_INCREASE_PCT_ENV)
    if max_pct is None or old_micros <= 0 or new_micros <= old_micros:
        return
    increase_pct = (new_micros - old_micros) / old_micros * 100
    if increase_pct > max_pct + 1e-9:
        raise ToolError(
            f"Error: Raising the budget from {from_micros(old_micros):.2f} to "
            f"{from_micros(new_micros):.2f} is a {increase_pct:.0f}% increase, "
            f"above this server's limit of {max_pct:.0f}% per change. No "
            "changes were made."
        )


def _looks_like_mutation(arguments: Mapping[str, Any]) -> bool:
    return "validate_only" in arguments or "checksum" in arguments


class WriteAllowlistMiddleware(Middleware):
    """Refuses any mutating tool call whose customer_id is not allowlisted.

    A tool counts as mutating when its readOnlyHint is not True (the MCP
    default). If the tool cannot be resolved, a call carrying
    `validate_only` or `checksum` is treated as mutating (fail closed).
    """

    def __init__(self, server: Any | None = None) -> None:
        super().__init__()
        self._fallback_server = server

    async def on_call_tool(
        self,
        context: MiddlewareContext[mcp_types.CallToolRequestParams],
        call_next: CallNext[mcp_types.CallToolRequestParams, Any],
    ) -> Any:
        # Imported lazily to avoid a circular import with ads_mcp.audit.
        from ads_mcp.audit import _is_read_only_tool, _resolve_tool

        arguments: Mapping[str, Any] = context.message.arguments or {}
        tool = await _resolve_tool(context, context.message.name, self._fallback_server)
        read_only = _is_read_only_tool(tool)
        mutating = (
            read_only is False if tool is not None else _looks_like_mutation(arguments)
        )
        if mutating:
            try:
                require_write_allowed(arguments.get("customer_id"))
            except ToolError as exc:
                logger.warning(
                    "Refused write tool call %s for customer_id=%r: %s",
                    context.message.name,
                    arguments.get("customer_id"),
                    exc,
                )
                raise
        return await call_next(context)
