"""Tools for managing keywords and negative keywords.

Safety rules enforced here (in addition to ads_mcp.guards):
- Every tool uses Propose & Confirm (validate_only + checksum), and the
  checksum covers the current state read from Google Ads, so a stale
  proposal cannot be committed.
- BROAD match is refused unless the campaign uses Smart Bidding.
- Keyword CPC bids only apply to Manual CPC campaigns and are capped by
  GOOGLE_ADS_MCP_MAX_CPC (or the daily budget cap).
- Duplicates already in Google Ads are skipped and listed in the preview.
- Negative keywords that would block existing keywords are flagged.
- Nothing is removed: keywords can be paused, not deleted.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from ads_mcp import guards
from ads_mcp import propose_and_confirm as pac
from ads_mcp import write_common as common
from ads_mcp.tools.campaigns import KeywordInput, MatchType, _validate_keywords
import ads_mcp.utils as utils

keywords_mcp = FastMCP("keywords")

_SHARED_SET_TMP_ID = -1
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)


class KeywordBidInput(BaseModel):
    """A keyword to add, with an optional max CPC bid."""

    text: str = Field(description="Keyword text, e.g. 'brass cabinet handles'.")
    match_type: MatchType = Field(
        default=MatchType.PHRASE, description="EXACT, PHRASE or BROAD."
    )
    cpc_bid: float | None = Field(
        default=None,
        description="Optional max CPC in account currency. Manual CPC campaigns only.",
    )


class KeywordBidChange(BaseModel):
    """A new max CPC bid for an existing keyword."""

    keyword_id: str = Field(description="The keyword's criterion ID.")
    cpc_bid: float = Field(description="New max CPC in account currency.")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _validate_negatives(keywords: list[KeywordInput] | None) -> list[dict[str, str]]:
    negs = _validate_keywords(keywords, "negative keyword")
    if not negs:
        raise ToolError("Error: Provide at least one negative keyword.")
    if len(negs) > common.MAX_ITEMS_PER_CALL:
        raise ToolError(
            f"Error: At most {common.MAX_ITEMS_PER_CALL} keywords per call."
        )
    return negs


def _split_new(
    items: list[dict[str, Any]], existing: set[tuple[str, str]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    new, skipped = [], []
    for item in items:
        key = common.kw_key(item["text"], item["match_type"])
        (skipped if key in existing else new).append(item)
    return new, skipped


def _existing_keyword_keys(
    ga_client: Any, cid: str, where: str, prefix: str
) -> set[tuple[str, str]]:
    rows = common.search(
        ga_client,
        cid,
        f"SELECT {prefix}.keyword.text, {prefix}.keyword.match_type "
        f"FROM {prefix} WHERE {where}",
    )
    keys = set()
    for row in rows:
        crit = getattr(row, prefix)
        keys.add(common.kw_key(crit.keyword.text, crit.keyword.match_type.name))
    return keys


def _positive_keyword_texts(ga_client: Any, cid: str, where: str) -> list[str]:
    rows = common.search(
        ga_client,
        cid,
        "SELECT ad_group_criterion.keyword.text FROM ad_group_criterion "
        f"WHERE {where} AND ad_group_criterion.type = KEYWORD "
        "AND ad_group_criterion.negative = FALSE "
        "AND ad_group_criterion.status != 'REMOVED'",
    )
    return [row.ad_group_criterion.keyword.text for row in rows]


def _fetch_negative_list(ga_client: Any, cid: str, list_id: str) -> dict[str, Any]:
    rows = common.search(
        ga_client,
        cid,
        "SELECT shared_set.id, shared_set.name, shared_set.type, "
        f"shared_set.status FROM shared_set WHERE shared_set.id = {list_id}",
    )
    if not rows:
        raise ToolError(
            f"Error: Negative keyword list {list_id} was not found in customer {cid}."
        )
    row = rows[0]
    if row.shared_set.type_.name != "NEGATIVE_KEYWORDS":
        raise ToolError(
            f"Error: Shared set {list_id} is a {row.shared_set.type_.name} list, "
            "not a negative keyword list."
        )
    if row.shared_set.status.name != "ENABLED":
        raise ToolError(f"Error: Negative keyword list {list_id} is removed.")
    return {
        "id": list_id,
        "resource_name": f"customers/{cid}/sharedSets/{list_id}",
        "name": row.shared_set.name,
    }


def _campaigns_using_list(ga_client: Any, cid: str, list_id: str) -> list[str]:
    rows = common.search(
        ga_client,
        cid,
        "SELECT campaign.id FROM campaign_shared_set "
        f"WHERE shared_set.id = {list_id} "
        "AND campaign_shared_set.status = 'ENABLED'",
    )
    return [str(row.campaign.id) for row in rows]


def _keyword_ops(
    ga_client: Any,
    op_type: str,
    parent_field: str,
    parent_rn: str,
    items: list[dict[str, Any]],
    negative: bool,
) -> list[Any]:
    enums = ga_client.enums
    ops = []
    for item in items:
        op = ga_client.get_type(op_type)
        crit = op.create
        setattr(crit, parent_field, parent_rn)
        crit.keyword.text = item["text"]
        crit.keyword.match_type = getattr(
            enums.KeywordMatchTypeEnum, item["match_type"]
        )
        if negative:
            # Shared list entries have no `negative` field; the list type
            # already makes them negative.
            if op_type != "SharedCriterionOperation":
                crit.negative = True
        else:
            crit.status = enums.AdGroupCriterionStatusEnum.ENABLED
            if item.get("cpc_bid_micros"):
                crit.cpc_bid_micros = item["cpc_bid_micros"]
        ops.append(op)
    return ops


def _fetch_keywords(
    ga_client: Any, cid: str, ad_group_id: str, keyword_ids: list[str]
) -> dict[str, dict[str, Any]]:
    rows = common.search(
        ga_client,
        cid,
        "SELECT ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, "
        "ad_group_criterion.keyword.match_type, ad_group_criterion.status, "
        "ad_group_criterion.cpc_bid_micros FROM ad_group_criterion "
        f"WHERE ad_group.id = {ad_group_id} "
        f"AND ad_group_criterion.criterion_id IN ({', '.join(keyword_ids)}) "
        "AND ad_group_criterion.type = KEYWORD "
        "AND ad_group_criterion.negative = FALSE",
    )
    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        crit = row.ad_group_criterion
        kid = str(crit.criterion_id)
        found[kid] = {
            "id": kid,
            "resource_name": f"customers/{cid}/adGroupCriteria/{ad_group_id}~{kid}",
            "text": crit.keyword.text,
            "match_type": crit.keyword.match_type.name,
            "status": crit.status.name,
            "cpc_bid_micros": int(crit.cpc_bid_micros or 0),
        }
    missing = [k for k in keyword_ids if k not in found]
    if missing:
        raise ToolError(
            f"Error: Keyword ID(s) {', '.join(missing)} not found in ad group "
            f"{ad_group_id}."
        )
    removed = [
        f"{found[k]['text']}" for k in keyword_ids if found[k]["status"] == "REMOVED"
    ]
    if removed:
        raise ToolError(
            f"Error: These keywords are removed and cannot be changed: {', '.join(removed)}."
        )
    return {k: found[k] for k in keyword_ids}


# --------------------------------------------------------------------------
# Keywords
# --------------------------------------------------------------------------


@keywords_mcp.tool(annotations=_WRITE)
def add_keywords(
    customer_id: str | int,
    ad_group_id: str | int,
    keywords: list[KeywordBidInput],
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Adds keywords to an existing ad group.

    If the campaign and ad group are enabled, new keywords start serving as
    soon as Google approves them; the preview says so. Keywords already in the
    ad group are skipped and listed. BROAD match is refused unless the
    campaign uses Smart Bidding. `cpc_bid` only applies to Manual CPC campaigns.

    Propose & Confirm Workflow: call with `validate_only=True` (default) to get
    `change_items` and a `checksum`; show them to the user; after explicit
    approval call again with the same parameters, `validate_only=False` and
    the `checksum`.

    Args:
        customer_id: Google Ads customer ID.
        ad_group_id: The ad group to add keywords to.
        keywords: Keywords, each {text, match_type: EXACT|PHRASE|BROAD, cpc_bid?}.
        login_customer_id: Optional manager account ID.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior validate_only=True call.
    """
    cid = guards.require_write_allowed(customer_id)
    ag_id = common.clean_id(ad_group_id, "ad_group_id")
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for kw in keywords or []:
        if isinstance(kw, dict):
            kw = KeywordBidInput(**kw)
        cleaned = _validate_keywords(
            [KeywordInput(text=kw.text, match_type=kw.match_type)], "keyword"
        )
        if not cleaned:
            continue
        item: dict[str, Any] = dict(cleaned[0])
        key = common.kw_key(item["text"], item["match_type"])
        if key in seen:
            continue
        seen.add(key)
        item["cpc_bid_micros"] = (
            guards.check_cpc_bid(kw.cpc_bid) if kw.cpc_bid is not None else None
        )
        items.append(item)
    if not items:
        raise ToolError("Error: Provide at least one keyword.")
    if len(items) > common.MAX_ITEMS_PER_CALL:
        raise ToolError(
            f"Error: At most {common.MAX_ITEMS_PER_CALL} keywords per call."
        )

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    ag = common.fetch_ad_group(ga_client, cid, ag_id)
    broad = [i["text"] for i in items if i["match_type"] == MatchType.BROAD.value]
    if broad and ag["bidding"] not in common.SMART_BIDDING_TYPES:
        raise ToolError(
            "Error: BROAD match keywords are only allowed in campaigns using "
            f"Smart Bidding; '{ag['campaign_name']}' uses {ag['bidding']}. "
            f"Broad keywords: {broad}. Use PHRASE or EXACT."
        )
    if (
        any(i["cpc_bid_micros"] for i in items)
        and ag["bidding"] not in common.MANUAL_BIDDING_TYPES
    ):
        raise ToolError(
            "Error: Keyword CPC bids only apply to Manual CPC campaigns; "
            f"'{ag['campaign_name']}' uses {ag['bidding']}. Leave cpc_bid out."
        )

    existing = _existing_keyword_keys(
        ga_client,
        cid,
        f"ad_group.id = {ag_id} AND ad_group_criterion.type = KEYWORD "
        "AND ad_group_criterion.negative = FALSE "
        "AND ad_group_criterion.status != 'REMOVED'",
        "ad_group_criterion",
    )
    new, skipped = _split_new(items, existing)
    if not new:
        raise ToolError(
            "Error: All of these keywords are already in the ad group. No "
            "changes were made."
        )

    ops = _keyword_ops(
        ga_client,
        "AdGroupCriterionOperation",
        "ad_group",
        ag["resource_name"],
        new,
        False,
    )
    request = common.build_request(ga_client, "MutateAdGroupCriteriaRequest", cid, ops)
    change_items = pac.build_change_items(
        operation="CREATE",
        entity_readable="Keywords",
        primary_key_readable=f"{ag['name']} (ad group {ag_id})",
        fields={
            "Campaign": ag["campaign_name"],
            "Keywords to add": common.kw_list(new),
            "Already in ad group (skipped)": common.kw_list(skipped) or None,
            "Serving": common.serving_note(ag["campaign_status"], ag["status"]),
        },
    )
    return common.run_mutation(
        customer_id=cid,
        validate_only=validate_only,
        checksum=checksum,
        payload={"tool": "add_keywords", "ad_group_id": ag_id, "keywords": new},
        change_items=change_items,
        service=utils.get_googleads_service_from_client(
            ga_client, "AdGroupCriterionService"
        ),
        method="mutate_ad_group_criteria",
        request=request,
        success=f"Successfully added {len(new)} keyword(s) to ad group '{ag['name']}'.",
        error_prefix="Failed to add keywords",
    )


def _set_keyword_status(
    *,
    tool_name: str,
    target_status: str,
    customer_id: str | int,
    ad_group_id: str | int,
    keyword_ids: list[str],
    login_customer_id: str | int | None,
    validate_only: bool,
    checksum: str | None,
) -> str:
    cid = guards.require_write_allowed(customer_id)
    ag_id = common.clean_id(ad_group_id, "ad_group_id")
    ids = common.clean_id_list(keyword_ids, "keyword_id")
    if not ids:
        raise ToolError("Error: Provide at least one keyword_id.")

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    ag = common.fetch_ad_group(ga_client, cid, ag_id)
    current = _fetch_keywords(ga_client, cid, ag_id, ids)
    to_change = [k for k in current.values() if k["status"] != target_status]
    if not to_change:
        raise ToolError(f"Error: These keywords are already {target_status}.")

    enums = ga_client.enums
    ops = []
    for kw in to_change:
        op = ga_client.get_type("AdGroupCriterionOperation")
        op.update.resource_name = kw["resource_name"]
        op.update.status = getattr(enums.AdGroupCriterionStatusEnum, target_status)
        op.update_mask.paths.append("status")
        ops.append(op)
    request = common.build_request(ga_client, "MutateAdGroupCriteriaRequest", cid, ops)

    change_items = []
    for kw in to_change:
        change_items += pac.build_change_items(
            operation="UPDATE",
            entity_readable="Keyword",
            primary_key_readable=f"{kw['text']} [{kw['match_type']}] in {ag['name']}",
            fields={"Status": target_status},
            original_fields={"Status": kw["status"]},
        )
    if target_status == "ENABLED":
        change_items += pac.build_change_items(
            operation="UPDATE",
            entity_readable="Keyword",
            primary_key_readable=ag["name"],
            fields={
                "Serving": common.serving_note(ag["campaign_status"], ag["status"])
            },
        )
    verb = "enabled" if target_status == "ENABLED" else "paused"
    return common.run_mutation(
        customer_id=cid,
        validate_only=validate_only,
        checksum=checksum,
        payload={
            "tool": tool_name,
            "ad_group_id": ag_id,
            "changes": [
                {"id": k["id"], "old": k["status"], "new": target_status}
                for k in to_change
            ],
        },
        change_items=change_items,
        service=utils.get_googleads_service_from_client(
            ga_client, "AdGroupCriterionService"
        ),
        method="mutate_ad_group_criteria",
        request=request,
        success=f"Successfully {verb} {len(to_change)} keyword(s) in '{ag['name']}'.",
        error_prefix=f"Failed to set keyword status to {target_status}",
    )


@keywords_mcp.tool(annotations=_WRITE)
def pause_keywords(
    customer_id: str | int,
    ad_group_id: str | int,
    keyword_ids: list[str],
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Pauses keywords in an ad group (they stop serving; nothing is deleted).

    Find keyword IDs with a search query on `keyword_view` or
    `ad_group_criterion` (ad_group_criterion.criterion_id).

    Propose & Confirm Workflow: `validate_only=True` (default) returns
    `change_items` and a `checksum`; after the user approves, call again with
    the same parameters, `validate_only=False` and the `checksum`.

    Args:
        customer_id: Google Ads customer ID.
        ad_group_id: The keywords' ad group ID.
        keyword_ids: Keyword criterion IDs to pause.
        login_customer_id: Optional manager account ID.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior validate_only=True call.
    """
    return _set_keyword_status(
        tool_name="pause_keywords",
        target_status="PAUSED",
        customer_id=customer_id,
        ad_group_id=ad_group_id,
        keyword_ids=keyword_ids,
        login_customer_id=login_customer_id,
        validate_only=validate_only,
        checksum=checksum,
    )


@keywords_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
def enable_keywords(
    customer_id: str | int,
    ad_group_id: str | int,
    keyword_ids: list[str],
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Enables paused keywords so they can serve again.

    Propose & Confirm Workflow: `validate_only=True` (default) returns
    `change_items` (including whether they will serve straight away) and a
    `checksum`; after the user approves, call again with the same parameters,
    `validate_only=False` and the `checksum`.

    Args:
        customer_id: Google Ads customer ID.
        ad_group_id: The keywords' ad group ID.
        keyword_ids: Keyword criterion IDs to enable.
        login_customer_id: Optional manager account ID.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior validate_only=True call.
    """
    return _set_keyword_status(
        tool_name="enable_keywords",
        target_status="ENABLED",
        customer_id=customer_id,
        ad_group_id=ad_group_id,
        keyword_ids=keyword_ids,
        login_customer_id=login_customer_id,
        validate_only=validate_only,
        checksum=checksum,
    )


@keywords_mcp.tool(annotations=_WRITE)
def update_keyword_bids(
    customer_id: str | int,
    ad_group_id: str | int,
    bids: list[KeywordBidChange],
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Changes keyword max CPC bids (Manual CPC campaigns only).

    Bids are capped by GOOGLE_ADS_MCP_MAX_CPC (or the daily budget cap).
    The preview shows each keyword's old and new bid.

    Propose & Confirm Workflow: `validate_only=True` (default) returns
    `change_items` and a `checksum`; after the user approves, call again with
    the same parameters, `validate_only=False` and the `checksum`.

    Args:
        customer_id: Google Ads customer ID.
        ad_group_id: The keywords' ad group ID.
        bids: Changes, each {keyword_id, cpc_bid} with cpc_bid in account currency.
        login_customer_id: Optional manager account ID.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior validate_only=True call.
    """
    cid = guards.require_write_allowed(customer_id)
    ag_id = common.clean_id(ad_group_id, "ad_group_id")
    new_bids: dict[str, int] = {}
    for change in bids or []:
        if isinstance(change, dict):
            change = KeywordBidChange(**change)
        new_bids[common.clean_id(change.keyword_id, "keyword_id")] = (
            guards.check_cpc_bid(change.cpc_bid)
        )
    if not new_bids:
        raise ToolError("Error: Provide at least one bid change.")
    if len(new_bids) > common.MAX_ITEMS_PER_CALL:
        raise ToolError(
            f"Error: At most {common.MAX_ITEMS_PER_CALL} bid changes per call."
        )

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    ag = common.fetch_ad_group(ga_client, cid, ag_id)
    if ag["bidding"] not in common.MANUAL_BIDDING_TYPES:
        raise ToolError(
            "Error: Keyword bids only apply to Manual CPC campaigns; "
            f"'{ag['campaign_name']}' uses {ag['bidding']}. No changes were made."
        )
    current = _fetch_keywords(ga_client, cid, ag_id, list(new_bids))
    changes = [
        (kw, new_bids[kid])
        for kid, kw in current.items()
        if kw["cpc_bid_micros"] != new_bids[kid]
    ]
    if not changes:
        raise ToolError("Error: These keywords already have those bids.")

    ops = []
    change_items = []
    for kw, micros in changes:
        op = ga_client.get_type("AdGroupCriterionOperation")
        op.update.resource_name = kw["resource_name"]
        op.update.cpc_bid_micros = micros
        op.update_mask.paths.append("cpc_bid_micros")
        ops.append(op)
        change_items += pac.build_change_items(
            operation="UPDATE",
            entity_readable="Keyword max CPC",
            primary_key_readable=f"{kw['text']} [{kw['match_type']}] in {ag['name']}",
            fields={"Max CPC": f"{guards.from_micros(micros):.2f}"},
            original_fields={
                "Max CPC": (
                    f"{guards.from_micros(kw['cpc_bid_micros']):.2f}"
                    if kw["cpc_bid_micros"]
                    else "ad group default"
                )
            },
        )
    request = common.build_request(ga_client, "MutateAdGroupCriteriaRequest", cid, ops)
    return common.run_mutation(
        customer_id=cid,
        validate_only=validate_only,
        checksum=checksum,
        payload={
            "tool": "update_keyword_bids",
            "ad_group_id": ag_id,
            "changes": [
                {"id": kw["id"], "old": kw["cpc_bid_micros"], "new": m}
                for kw, m in changes
            ],
        },
        change_items=change_items,
        service=utils.get_googleads_service_from_client(
            ga_client, "AdGroupCriterionService"
        ),
        method="mutate_ad_group_criteria",
        request=request,
        success=f"Successfully changed {len(changes)} keyword bid(s) in '{ag['name']}'.",
        error_prefix="Failed to update keyword bids",
    )


# --------------------------------------------------------------------------
# Negative keywords
# --------------------------------------------------------------------------


@keywords_mcp.tool(annotations=_WRITE)
def add_negative_keywords(
    customer_id: str | int,
    keywords: list[KeywordInput],
    campaign_id: str | int | None = None,
    ad_group_id: str | int | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Adds negative keywords to one campaign or one ad group.

    Give exactly one of `campaign_id` or `ad_group_id`. Negatives already
    there are skipped. The preview warns when a negative would block one of
    the campaign's (or ad group's) own keywords.

    Propose & Confirm Workflow: `validate_only=True` (default) returns
    `change_items` and a `checksum`; after the user approves, call again with
    the same parameters, `validate_only=False` and the `checksum`.

    Args:
        customer_id: Google Ads customer ID.
        keywords: Negative keywords, each {text, match_type: EXACT|PHRASE|BROAD}.
        campaign_id: Add at campaign level (blocks the whole campaign).
        ad_group_id: Add at ad group level (blocks one ad group).
        login_customer_id: Optional manager account ID.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior validate_only=True call.
    """
    cid = guards.require_write_allowed(customer_id)
    if (campaign_id is None) == (ad_group_id is None):
        raise ToolError("Error: Give exactly one of campaign_id or ad_group_id.")
    negs = _validate_negatives(keywords)
    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)

    if campaign_id is not None:
        camp_id = common.clean_id(campaign_id, "campaign_id")
        camp = common.fetch_campaigns(ga_client, cid, [camp_id])[camp_id]
        level, target_id, target_name = "campaign", camp_id, camp["name"]
        existing = _existing_keyword_keys(
            ga_client,
            cid,
            f"campaign.id = {camp_id} AND campaign_criterion.type = KEYWORD "
            "AND campaign_criterion.negative = TRUE",
            "campaign_criterion",
        )
        positives = _positive_keyword_texts(ga_client, cid, f"campaign.id = {camp_id}")
        op_type, parent_field, parent_rn = (
            "CampaignCriterionOperation",
            "campaign",
            camp["resource_name"],
        )
        service_name, method, request_type = (
            "CampaignCriterionService",
            "mutate_campaign_criteria",
            "MutateCampaignCriteriaRequest",
        )
    else:
        ag_id = common.clean_id(ad_group_id, "ad_group_id")
        ag = common.fetch_ad_group(ga_client, cid, ag_id)
        level, target_id, target_name = "ad group", ag_id, ag["name"]
        existing = _existing_keyword_keys(
            ga_client,
            cid,
            f"ad_group.id = {ag_id} AND ad_group_criterion.type = KEYWORD "
            "AND ad_group_criterion.negative = TRUE "
            "AND ad_group_criterion.status != 'REMOVED'",
            "ad_group_criterion",
        )
        positives = _positive_keyword_texts(ga_client, cid, f"ad_group.id = {ag_id}")
        op_type, parent_field, parent_rn = (
            "AdGroupCriterionOperation",
            "ad_group",
            ag["resource_name"],
        )
        service_name, method, request_type = (
            "AdGroupCriterionService",
            "mutate_ad_group_criteria",
            "MutateAdGroupCriteriaRequest",
        )

    new, skipped = _split_new(negs, existing)
    if not new:
        raise ToolError(
            f"Error: All of these negatives are already on the {level}. No "
            "changes were made."
        )
    conflicts = common.find_conflicts(new, positives)
    ops = _keyword_ops(ga_client, op_type, parent_field, parent_rn, new, True)
    request = common.build_request(ga_client, request_type, cid, ops)
    change_items = pac.build_change_items(
        operation="CREATE",
        entity_readable=f"Negative keywords ({level} level)",
        primary_key_readable=f"{target_name} ({level} {target_id})",
        fields={
            "Negatives to add": common.kw_list(new),
            "Already there (skipped)": common.kw_list(skipped) or None,
            "WARNING: would block existing keywords": (
                "; ".join(conflicts) if conflicts else None
            ),
        },
    )
    return common.run_mutation(
        customer_id=cid,
        validate_only=validate_only,
        checksum=checksum,
        payload={
            "tool": "add_negative_keywords",
            "level": level,
            "target_id": target_id,
            "negatives": new,
        },
        change_items=change_items,
        service=utils.get_googleads_service_from_client(ga_client, service_name),
        method=method,
        request=request,
        success=(
            f"Successfully added {len(new)} negative keyword(s) to {level} "
            f"'{target_name}'."
        ),
        error_prefix="Failed to add negative keywords",
    )


@keywords_mcp.tool(annotations=_WRITE)
def create_negative_keyword_list(
    customer_id: str | int,
    list_name: str,
    keywords: list[KeywordInput],
    campaign_ids: list[str] | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Creates a shared negative keyword list and optionally applies it to campaigns.

    One atomic request: the list, its keywords and the campaign links are all
    created, or nothing is. The preview warns when a negative would block
    keywords in the campaigns it is applied to.

    Propose & Confirm Workflow: `validate_only=True` (default) returns
    `change_items` and a `checksum`; after the user approves, call again with
    the same parameters, `validate_only=False` and the `checksum`.

    Args:
        customer_id: Google Ads customer ID.
        list_name: Name for the new list; must be unique in the account.
        keywords: Negative keywords, each {text, match_type}.
        campaign_ids: Optional campaigns to apply the list to.
        login_customer_id: Optional manager account ID.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior validate_only=True call.
    """
    cid = guards.require_write_allowed(customer_id)
    name = " ".join((list_name or "").split())
    if not name:
        raise ToolError("Error: 'list_name' is required.")
    negs = _validate_negatives(keywords)
    camp_ids = common.clean_id_list(campaign_ids, "campaign_id")

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    camps = common.fetch_campaigns(ga_client, cid, camp_ids)
    positives = (
        _positive_keyword_texts(
            ga_client, cid, f"campaign.id IN ({', '.join(camp_ids)})"
        )
        if camp_ids
        else []
    )
    conflicts = common.find_conflicts(negs, positives)

    enums = ga_client.enums
    set_rn = f"customers/{cid}/sharedSets/{_SHARED_SET_TMP_ID}"
    operations = []
    op = ga_client.get_type("MutateOperation")
    op.shared_set_operation.create.resource_name = set_rn
    op.shared_set_operation.create.name = name
    op.shared_set_operation.create.type_ = enums.SharedSetTypeEnum.NEGATIVE_KEYWORDS
    operations.append(op)
    for neg in negs:
        op = ga_client.get_type("MutateOperation")
        crit = op.shared_criterion_operation.create
        crit.shared_set = set_rn
        crit.keyword.text = neg["text"]
        crit.keyword.match_type = getattr(enums.KeywordMatchTypeEnum, neg["match_type"])
        operations.append(op)
    for camp in camps.values():
        op = ga_client.get_type("MutateOperation")
        op.campaign_shared_set_operation.create.campaign = camp["resource_name"]
        op.campaign_shared_set_operation.create.shared_set = set_rn
        operations.append(op)
    request = ga_client.get_type("MutateGoogleAdsRequest")
    request.customer_id = cid
    request.mutate_operations.extend(operations)

    change_items = pac.build_change_items(
        operation="CREATE",
        entity_readable="Negative keyword list",
        primary_key_readable=name,
        fields={
            "List name": name,
            "Negatives": common.kw_list(negs),
            "Applied to campaigns": (
                ", ".join(c["name"] for c in camps.values()) or "none yet"
            ),
            "WARNING: would block existing keywords": (
                "; ".join(conflicts) if conflicts else None
            ),
        },
    )
    return common.run_mutation(
        customer_id=cid,
        validate_only=validate_only,
        checksum=checksum,
        payload={
            "tool": "create_negative_keyword_list",
            "name": name,
            "negatives": negs,
            "campaign_ids": camp_ids,
        },
        change_items=change_items,
        service=utils.get_googleads_service_from_client(ga_client, "GoogleAdsService"),
        method="mutate",
        request=request,
        success=(
            f"Successfully created negative keyword list '{name}' with "
            f"{len(negs)} keyword(s), applied to {len(camps)} campaign(s)."
        ),
        error_prefix="Failed to create negative keyword list",
    )


@keywords_mcp.tool(annotations=_WRITE)
def add_to_negative_keyword_list(
    customer_id: str | int,
    list_id: str | int,
    keywords: list[KeywordInput],
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Adds keywords to an existing shared negative keyword list.

    Every campaign using the list is affected; the preview names them and
    warns when a negative would block their keywords. Find list IDs with a
    search on `shared_set` where shared_set.type = 'NEGATIVE_KEYWORDS'.

    Propose & Confirm Workflow: `validate_only=True` (default) returns
    `change_items` and a `checksum`; after the user approves, call again with
    the same parameters, `validate_only=False` and the `checksum`.

    Args:
        customer_id: Google Ads customer ID.
        list_id: The negative keyword list's shared set ID.
        keywords: Negative keywords, each {text, match_type}.
        login_customer_id: Optional manager account ID.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior validate_only=True call.
    """
    cid = guards.require_write_allowed(customer_id)
    set_id = common.clean_id(list_id, "list_id")
    negs = _validate_negatives(keywords)

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    shared = _fetch_negative_list(ga_client, cid, set_id)
    existing = _existing_keyword_keys(
        ga_client, cid, f"shared_set.id = {set_id}", "shared_criterion"
    )
    new, skipped = _split_new(negs, existing)
    if not new:
        raise ToolError(
            "Error: All of these negatives are already in the list. No changes "
            "were made."
        )
    using = _campaigns_using_list(ga_client, cid, set_id)
    camps = common.fetch_campaigns(ga_client, cid, using) if using else {}
    positives = (
        _positive_keyword_texts(ga_client, cid, f"campaign.id IN ({', '.join(using)})")
        if using
        else []
    )
    conflicts = common.find_conflicts(new, positives)

    ops = _keyword_ops(
        ga_client,
        "SharedCriterionOperation",
        "shared_set",
        shared["resource_name"],
        new,
        True,
    )
    request = common.build_request(ga_client, "MutateSharedCriteriaRequest", cid, ops)
    change_items = pac.build_change_items(
        operation="CREATE",
        entity_readable="Negative keyword list entries",
        primary_key_readable=f"{shared['name']} (list {set_id})",
        fields={
            "Negatives to add": common.kw_list(new),
            "Already in list (skipped)": common.kw_list(skipped) or None,
            "Campaigns affected": ", ".join(c["name"] for c in camps.values())
            or "none",
            "WARNING: would block existing keywords": (
                "; ".join(conflicts) if conflicts else None
            ),
        },
    )
    return common.run_mutation(
        customer_id=cid,
        validate_only=validate_only,
        checksum=checksum,
        payload={
            "tool": "add_to_negative_keyword_list",
            "list_id": set_id,
            "negatives": new,
        },
        change_items=change_items,
        service=utils.get_googleads_service_from_client(
            ga_client, "SharedCriterionService"
        ),
        method="mutate_shared_criteria",
        request=request,
        success=(
            f"Successfully added {len(new)} negative keyword(s) to list "
            f"'{shared['name']}'."
        ),
        error_prefix="Failed to add to negative keyword list",
    )


@keywords_mcp.tool(annotations=_WRITE)
def apply_negative_keyword_list(
    customer_id: str | int,
    list_id: str | int,
    campaign_ids: list[str],
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Applies an existing shared negative keyword list to campaigns.

    Campaigns already using the list are skipped. The preview warns when a
    negative in the list would block the campaigns' keywords.

    Propose & Confirm Workflow: `validate_only=True` (default) returns
    `change_items` and a `checksum`; after the user approves, call again with
    the same parameters, `validate_only=False` and the `checksum`.

    Args:
        customer_id: Google Ads customer ID.
        list_id: The negative keyword list's shared set ID.
        campaign_ids: Campaigns to apply the list to.
        login_customer_id: Optional manager account ID.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior validate_only=True call.
    """
    cid = guards.require_write_allowed(customer_id)
    set_id = common.clean_id(list_id, "list_id")
    camp_ids = common.clean_id_list(campaign_ids, "campaign_id")
    if not camp_ids:
        raise ToolError("Error: Provide at least one campaign_id.")

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    shared = _fetch_negative_list(ga_client, cid, set_id)
    camps = common.fetch_campaigns(ga_client, cid, camp_ids)
    already = set(_campaigns_using_list(ga_client, cid, set_id))
    new_ids = [c for c in camp_ids if c not in already]
    if not new_ids:
        raise ToolError(
            "Error: These campaigns already use this list. No changes were made."
        )
    rows = common.search(
        ga_client,
        cid,
        "SELECT shared_criterion.keyword.text, shared_criterion.keyword.match_type "
        f"FROM shared_criterion WHERE shared_set.id = {set_id}",
    )
    list_negs = [
        {
            "text": r.shared_criterion.keyword.text,
            "match_type": r.shared_criterion.keyword.match_type.name,
        }
        for r in rows
    ]
    positives = _positive_keyword_texts(
        ga_client, cid, f"campaign.id IN ({', '.join(new_ids)})"
    )
    conflicts = common.find_conflicts(list_negs, positives)

    ops = []
    for camp_id in new_ids:
        op = ga_client.get_type("CampaignSharedSetOperation")
        op.create.campaign = camps[camp_id]["resource_name"]
        op.create.shared_set = shared["resource_name"]
        ops.append(op)
    request = common.build_request(
        ga_client, "MutateCampaignSharedSetsRequest", cid, ops
    )
    change_items = pac.build_change_items(
        operation="CREATE",
        entity_readable="Negative keyword list link",
        primary_key_readable=f"{shared['name']} (list {set_id})",
        fields={
            "Apply to campaigns": ", ".join(camps[c]["name"] for c in new_ids),
            "Already using it (skipped)": (
                ", ".join(camps[c]["name"] for c in camp_ids if c in already) or None
            ),
            "Negatives in list": str(len(list_negs)),
            "WARNING: would block existing keywords": (
                "; ".join(conflicts) if conflicts else None
            ),
        },
    )
    return common.run_mutation(
        customer_id=cid,
        validate_only=validate_only,
        checksum=checksum,
        payload={
            "tool": "apply_negative_keyword_list",
            "list_id": set_id,
            "campaign_ids": new_ids,
        },
        change_items=change_items,
        service=utils.get_googleads_service_from_client(
            ga_client, "CampaignSharedSetService"
        ),
        method="mutate_campaign_shared_sets",
        request=request,
        success=(
            f"Successfully applied list '{shared['name']}' to {len(new_ids)} "
            "campaign(s)."
        ),
        error_prefix="Failed to apply negative keyword list",
    )
