"""Shared helpers for the fork's write tools (keywords, ads).

Every ID that ends up in a GAQL query goes through `clean_id`, which keeps
digits only, so user input can never change a query's meaning.
"""

from __future__ import annotations

from typing import Any

from fastmcp.exceptions import ToolError

from ads_mcp import propose_and_confirm as pac
import ads_mcp.utils as utils

# Bidding strategies where Google sets bids per auction (Smart Bidding).
# BROAD match keywords are only allowed with these.
SMART_BIDDING_TYPES = frozenset(
    {
        "MAXIMIZE_CONVERSIONS",
        "TARGET_CPA",
        "MAXIMIZE_CONVERSION_VALUE",
        "TARGET_ROAS",
    }
)
# Bidding strategies where keyword-level CPC bids are used.
MANUAL_BIDDING_TYPES = frozenset({"MANUAL_CPC"})

# Largest number of items one tool call may change.
MAX_ITEMS_PER_CALL = 200


def clean_id(value: str | int | None, label: str) -> str:
    """Returns the digits of an ID or resource name, or raises."""
    digits = utils.clean_customer_id(utils.extract_id(str(value or "")))
    if not digits:
        raise ToolError(f"Error: '{label}' must contain digits.")
    return digits


def clean_id_list(values: list[str | int] | None, label: str) -> list[str]:
    """Cleans and de-duplicates a list of IDs, keeping order."""
    result: list[str] = []
    for value in values or []:
        digits = clean_id(value, label)
        if digits not in result:
            result.append(digits)
    if len(result) > MAX_ITEMS_PER_CALL:
        raise ToolError(
            f"Error: At most {MAX_ITEMS_PER_CALL} {label}s per call; "
            f"got {len(result)}."
        )
    return result


def search(ga_client: Any, customer_id: str, query: str) -> list[Any]:
    ga_service = utils.get_googleads_service_from_client(ga_client, "GoogleAdsService")
    return list(ga_service.search(customer_id=customer_id, query=query))


def fetch_ad_group(
    ga_client: Any, customer_id: str, ad_group_id: str
) -> dict[str, Any]:
    """Reads an ad group and its campaign's status and bidding."""
    rows = search(
        ga_client,
        customer_id,
        "SELECT ad_group.id, ad_group.name, ad_group.status, ad_group.type, "
        "campaign.id, campaign.name, campaign.status, "
        "campaign.bidding_strategy_type, campaign.advertising_channel_type "
        f"FROM ad_group WHERE ad_group.id = {ad_group_id}",
    )
    if not rows:
        raise ToolError(
            f"Error: Ad group {ad_group_id} was not found in customer "
            f"{customer_id}."
        )
    row = rows[0]
    info = {
        "id": ad_group_id,
        "resource_name": f"customers/{customer_id}/adGroups/{ad_group_id}",
        "name": row.ad_group.name,
        "status": row.ad_group.status.name,
        "type": row.ad_group.type_.name,
        "campaign_id": str(row.campaign.id),
        "campaign_name": row.campaign.name,
        "campaign_status": row.campaign.status.name,
        "bidding": row.campaign.bidding_strategy_type.name,
        "channel_type": row.campaign.advertising_channel_type.name,
    }
    if info["status"] == "REMOVED" or info["campaign_status"] == "REMOVED":
        raise ToolError(
            f"Error: Ad group '{info['name']}' or its campaign is removed and "
            "cannot be changed."
        )
    return info


def fetch_campaigns(
    ga_client: Any, customer_id: str, campaign_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Reads several campaigns by ID; raises if any is missing or removed."""
    if not campaign_ids:
        return {}
    rows = search(
        ga_client,
        customer_id,
        "SELECT campaign.id, campaign.name, campaign.status, "
        "campaign.bidding_strategy_type, campaign.advertising_channel_type "
        f"FROM campaign WHERE campaign.id IN ({', '.join(campaign_ids)})",
    )
    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        cid = str(row.campaign.id)
        found[cid] = {
            "id": cid,
            "resource_name": f"customers/{customer_id}/campaigns/{cid}",
            "name": row.campaign.name,
            "status": row.campaign.status.name,
            "bidding": row.campaign.bidding_strategy_type.name,
            "channel_type": row.campaign.advertising_channel_type.name,
        }
    missing = [c for c in campaign_ids if c not in found]
    if missing:
        raise ToolError(
            f"Error: Campaign(s) {', '.join(missing)} not found in customer "
            f"{customer_id}."
        )
    removed = [c["name"] for c in found.values() if c["status"] == "REMOVED"]
    if removed:
        raise ToolError(
            f"Error: These campaigns are removed and cannot be changed: "
            f"{', '.join(removed)}."
        )
    return {c: found[c] for c in campaign_ids}


def serving_note(campaign_status: str, ad_group_status: str | None = None) -> str:
    """Plain-language note on whether a new item will start serving."""
    if campaign_status != "ENABLED":
        return f"Not serving yet: the campaign is {campaign_status}"
    if ad_group_status is not None and ad_group_status != "ENABLED":
        return f"Not serving yet: the ad group is {ad_group_status}"
    return "LIVE: starts serving as soon as Google approves it"


def kw_key(text: str, match_type: str) -> tuple[str, str]:
    return (" ".join(text.lower().split()), match_type)


def kw_list(items: list[dict[str, Any]]) -> str:
    parts = []
    for k in items:
        part = f"{k['text']} [{k['match_type']}]"
        if k.get("cpc_bid_micros"):
            part += f" @ {k['cpc_bid_micros'] / 1_000_000:.2f}"
        parts.append(part)
    return ", ".join(parts)


def negative_blocks(negative_text: str, negative_match: str, keyword_text: str) -> bool:
    """Whether a negative keyword would block an existing positive keyword.

    Approximates Google's matching on the keyword's own text:
    EXACT blocks the identical text, PHRASE blocks text containing the
    phrase in order, BROAD blocks text containing all its words.
    """
    neg = negative_text.lower().split()
    kw = keyword_text.lower().split()
    if not neg:
        return False
    if negative_match == "EXACT":
        return neg == kw
    if negative_match == "PHRASE":
        n = len(neg)
        return any(kw[i : i + n] == neg for i in range(len(kw) - n + 1))
    return set(neg) <= set(kw)


def find_conflicts(
    negatives: list[dict[str, str]], positive_texts: list[str]
) -> list[str]:
    """Describes which positive keywords each negative would block."""
    conflicts: list[str] = []
    for neg in negatives:
        blocked = sorted(
            {
                t
                for t in positive_texts
                if negative_blocks(neg["text"], neg["match_type"], t)
            }
        )
        if blocked:
            shown = ", ".join(f"'{t}'" for t in blocked[:5])
            more = f" and {len(blocked) - 5} more" if len(blocked) > 5 else ""
            conflicts.append(
                f"-{neg['text']} [{neg['match_type']}] blocks {shown}{more}"
            )
    return conflicts


def run_mutation(
    *,
    customer_id: str,
    validate_only: bool,
    checksum: str | None,
    payload: dict[str, Any],
    change_items: list[Any],
    service: Any,
    method: str,
    request: Any,
    success: str,
    error_prefix: str,
) -> str:
    """Propose & Confirm for a single service mutate call.

    Proposing runs Google's dry run (validate_only=True on the request);
    committing re-sends the same request for real.
    """

    def _validate() -> None:
        request.validate_only = True
        getattr(service, method)(request=request)

    def _commit() -> str:
        request.validate_only = False
        getattr(service, method)(request=request)
        return success

    return pac.propose_and_confirm(
        customer_id=customer_id,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload=payload,
        change_items=change_items,
        validate_fn=_validate,
        commit_fn=_commit,
        error_prefix=error_prefix,
    )


def build_request(
    ga_client: Any, request_type: str, customer_id: str, ops: list[Any]
) -> Any:
    request = ga_client.get_type(request_type)
    request.customer_id = customer_id
    request.operations.extend(ops)
    return request
