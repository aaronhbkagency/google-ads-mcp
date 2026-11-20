"""Tools for creating and managing Search campaigns.

Safety rules enforced here (in addition to ads_mcp.guards):
- New campaigns are always created PAUSED; there is no status parameter.
- Going live only happens through `enable_campaign`.
- No tool in this namespace removes anything.
- Budgets are checked against GOOGLE_ADS_MCP_MAX_DAILY_BUDGET.
- BROAD match keywords are refused unless the campaign uses Maximize
  Conversions (broad match without Smart Bidding wastes spend).
- Every tool uses the Propose & Confirm workflow (validate_only + checksum).
"""

from __future__ import annotations

from enum import Enum
from typing import Any
from urllib.parse import urlparse

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from ads_mcp import guards
from ads_mcp import propose_and_confirm as pac
import ads_mcp.utils as utils

campaigns_mcp = FastMCP("campaigns")

# Temporary resource IDs used to link entities created in one atomic request.
_BUDGET_TMP_ID = -1
_CAMPAIGN_TMP_ID = -2
_AD_GROUP_TMP_ID = -3

_MAX_HEADLINE_LEN = 30
_MAX_DESCRIPTION_LEN = 90
_MAX_PATH_LEN = 15
_MAX_KEYWORD_LEN = 80

ENGLISH_LANGUAGE_ID = "1000"


class MatchType(str, Enum):
    EXACT = "EXACT"
    PHRASE = "PHRASE"
    BROAD = "BROAD"


class BiddingStrategy(str, Enum):
    MAXIMIZE_CLICKS = "MAXIMIZE_CLICKS"
    MANUAL_CPC = "MANUAL_CPC"
    MAXIMIZE_CONVERSIONS = "MAXIMIZE_CONVERSIONS"


class GeoTargetType(str, Enum):
    PRESENCE = "PRESENCE"
    PRESENCE_OR_INTEREST = "PRESENCE_OR_INTEREST"


class KeywordInput(BaseModel):
    """A keyword with its match type."""

    text: str = Field(description="Keyword text, e.g. 'brass cabinet handles'.")
    match_type: MatchType = Field(
        default=MatchType.PHRASE, description="EXACT, PHRASE or BROAD."
    )


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------


def _clean_ids(values: list[str] | None, label: str) -> list[str]:
    cleaned: list[str] = []
    for value in values or []:
        digits = utils.clean_customer_id(utils.extract_id(str(value)))
        if not digits:
            raise ToolError(f"Error: Invalid {label} '{value}'.")
        if digits not in cleaned:
            cleaned.append(digits)
    return cleaned


def _validate_url(url: str) -> str:
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ToolError(
            f"Error: final_url '{url}' must be a full URL starting with https://."
        )
    return url


def _validate_texts(
    texts: list[str], label: str, min_count: int, max_count: int, max_len: int
) -> list[str]:
    cleaned = [t.strip() for t in texts or [] if t and t.strip()]
    if not min_count <= len(cleaned) <= max_count:
        raise ToolError(
            f"Error: Provide {min_count}-{max_count} {label}; got {len(cleaned)}."
        )
    too_long = [t for t in cleaned if len(t) > max_len]
    if too_long:
        raise ToolError(
            f"Error: These {label} are over {max_len} characters: "
            + "; ".join(f"'{t}' ({len(t)})" for t in too_long)
        )
    if len(set(t.lower() for t in cleaned)) != len(cleaned):
        raise ToolError(f"Error: {label.capitalize()} must not repeat.")
    return cleaned


def _validate_path(path: str | None, label: str) -> str | None:
    if path is None or path == "":
        return None
    path = path.strip()
    if len(path) > _MAX_PATH_LEN or "/" in path:
        raise ToolError(
            f"Error: {label} must be at most {_MAX_PATH_LEN} characters with no '/'."
        )
    return path


def _validate_keywords(
    keywords: list[KeywordInput] | None, label: str
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for kw in keywords or []:
        if isinstance(kw, dict):
            kw = KeywordInput(**kw)
        text = " ".join(kw.text.split())
        if not text:
            continue
        if len(text) > _MAX_KEYWORD_LEN:
            raise ToolError(
                f"Error: {label} '{text}' is over {_MAX_KEYWORD_LEN} characters."
            )
        match = MatchType(kw.match_type).value
        key = (text.lower(), match)
        if key not in seen:
            seen.add(key)
            result.append({"text": text, "match_type": match})
    return result


# --------------------------------------------------------------------------
# Read helpers
# --------------------------------------------------------------------------


def _fetch_campaign(
    ga_client: Any, customer_id: str, campaign_id: str
) -> dict[str, Any]:
    """Reads the fields needed to describe and guard a campaign change."""
    ga_service = utils.get_googleads_service_from_client(
        ga_client, "GoogleAdsService"
    )
    query = (
        "SELECT campaign.id, campaign.name, campaign.status, "
        "campaign.advertising_channel_type, campaign_budget.resource_name, "
        "campaign_budget.amount_micros, campaign_budget.explicitly_shared "
        f"FROM campaign WHERE campaign.id = {campaign_id}"
    )
    rows = list(ga_service.search(customer_id=customer_id, query=query))
    if not rows:
        raise ToolError(
            f"Error: Campaign {campaign_id} was not found in customer {customer_id}."
        )
    row = rows[0]
    return {
        "resource_name": f"customers/{customer_id}/campaigns/{campaign_id}",
        "name": row.campaign.name,
        "status": row.campaign.status.name,
        "channel_type": row.campaign.advertising_channel_type.name,
        "budget_resource_name": row.campaign_budget.resource_name,
        "budget_micros": int(row.campaign_budget.amount_micros),
        "budget_shared": bool(row.campaign_budget.explicitly_shared),
    }


def _clean_campaign_id(campaign_id: str | int) -> str:
    cleaned = utils.clean_customer_id(utils.extract_id(str(campaign_id)))
    if not cleaned:
        raise ToolError("Error: 'campaign_id' must contain digits.")
    return cleaned


def _mutate_result_names(response: Any) -> list[str]:
    names: list[str] = []
    for op_response in response.mutate_operation_responses:
        which = type(op_response).pb(op_response).WhichOneof("response")
        if which:
            names.append(getattr(op_response, which).resource_name)
    return names


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


@campaigns_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
)
def create_search_campaign(
    customer_id: str | int,
    campaign_name: str,
    daily_budget: float,
    final_url: str,
    headlines: list[str],
    descriptions: list[str],
    keywords: list[KeywordInput],
    location_ids: list[str],
    contains_eu_political_advertising: bool,
    bidding_strategy: BiddingStrategy = BiddingStrategy.MAXIMIZE_CLICKS,
    max_cpc: float | None = None,
    target_cpa: float | None = None,
    language_ids: list[str] | None = None,
    negative_keywords: list[KeywordInput] | None = None,
    ad_group_name: str | None = None,
    path1: str | None = None,
    path2: str | None = None,
    include_search_partners: bool = False,
    geo_target_type: GeoTargetType = GeoTargetType.PRESENCE,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Creates a complete Search campaign in one atomic request. The campaign is always created PAUSED.

    Creates: daily budget, Search campaign (PAUSED, Google Search only unless
    include_search_partners, Display Network off), location and language
    targeting, campaign negative keywords, one ad group, its keywords, and one
    responsive search ad. Either everything is created or nothing is. Use
    `campaigns_enable_campaign` afterwards to go live.

    Propose & Confirm Workflow (`validate_only` and `checksum`):
    1. Call with `validate_only=True` (the default). Google validates the whole
       request and the tool returns `change_items` and a `checksum`. Nothing is created.
    2. Show the user the change_items and wait for explicit approval.
    3. Only after approval, call again with `validate_only=False`, the exact
       same parameters, and the `checksum`.

    Before calling, ASK the user whether the campaign contains EU political
    advertising; never choose this value for them.

    Args:
        customer_id: Google Ads customer ID (digits or hyphenated).
        campaign_name: Campaign name; must be unique in the account.
        daily_budget: Average daily budget in the account's currency (e.g. 50 = $50.00/day).
        final_url: Landing page URL for the ad, starting with https://.
        headlines: 3-15 responsive search ad headlines, each at most 30 characters.
        descriptions: 2-4 descriptions, each at most 90 characters.
        keywords: Ad group keywords, each {text, match_type: EXACT|PHRASE|BROAD}. BROAD is only allowed with MAXIMIZE_CONVERSIONS.
        location_ids: Google geo target constant IDs to target (e.g. '2036' = Australia, '1000286' = Sydney). Required so ads never serve worldwide.
        contains_eu_political_advertising: The advertiser's own declaration; ask the user.
        bidding_strategy: MAXIMIZE_CLICKS (default), MANUAL_CPC, or MAXIMIZE_CONVERSIONS (needs working conversion tracking).
        max_cpc: MAXIMIZE_CLICKS: max CPC bid ceiling. MANUAL_CPC: the ad group's default max CPC (required). Account currency.
        target_cpa: Optional target CPA for MAXIMIZE_CONVERSIONS, account currency.
        language_ids: Language constant IDs (default ['1000'] = English).
        negative_keywords: Campaign-level negative keywords, each {text, match_type}.
        ad_group_name: Ad group name (default: '<campaign_name> - Ad group 1').
        path1: Optional display URL path 1 (max 15 characters).
        path2: Optional display URL path 2 (max 15 characters; needs path1).
        include_search_partners: Also serve on Google search partner sites (default False).
        geo_target_type: PRESENCE (default; people in the locations) or PRESENCE_OR_INTEREST.
        login_customer_id: Optional manager account ID for the login-customer-id header.
        validate_only: True (default) validates and proposes; False commits (needs checksum).
        checksum: Checksum from the prior validate_only=True call.
    """
    cid = guards.require_write_allowed(customer_id)

    name = " ".join((campaign_name or "").split())
    if not name:
        raise ToolError("Error: 'campaign_name' is required.")
    group_name = (
        " ".join((ad_group_name or "").split()) or f"{name} - Ad group 1"
    )
    budget_micros = guards.check_daily_budget(daily_budget)
    url = _validate_url(final_url)
    heads = _validate_texts(headlines, "headlines", 3, 15, _MAX_HEADLINE_LEN)
    descs = _validate_texts(
        descriptions, "descriptions", 2, 4, _MAX_DESCRIPTION_LEN
    )
    p1 = _validate_path(path1, "path1")
    p2 = _validate_path(path2, "path2")
    if p2 and not p1:
        raise ToolError("Error: path2 needs path1.")

    strategy = BiddingStrategy(bidding_strategy)
    kws = _validate_keywords(keywords, "keyword")
    if not kws:
        raise ToolError("Error: Provide at least one keyword.")
    negs = _validate_keywords(negative_keywords, "negative keyword")
    broad = [k["text"] for k in kws if k["match_type"] == MatchType.BROAD.value]
    if broad and strategy is not BiddingStrategy.MAXIMIZE_CONVERSIONS:
        raise ToolError(
            "Error: BROAD match keywords are only allowed with "
            "MAXIMIZE_CONVERSIONS bidding, because broad match without Smart "
            f"Bidding tends to waste budget. Broad keywords: {broad}. Use "
            "PHRASE or EXACT, or switch bidding strategy."
        )

    locations = _clean_ids(location_ids, "location ID")
    if not locations:
        raise ToolError(
            "Error: Provide at least one location ID so the campaign does not "
            "serve worldwide (e.g. '2036' for Australia)."
        )
    languages = _clean_ids(
        language_ids if language_ids else [ENGLISH_LANGUAGE_ID], "language ID"
    )

    max_cpc_micros: int | None = None
    if max_cpc is not None:
        if max_cpc <= 0:
            raise ToolError("Error: max_cpc must be greater than 0.")
        max_cpc_micros = guards.to_micros(max_cpc)
    if strategy is BiddingStrategy.MANUAL_CPC and max_cpc_micros is None:
        raise ToolError("Error: MANUAL_CPC needs max_cpc (the default bid).")
    target_cpa_micros: int | None = None
    if target_cpa is not None:
        if strategy is not BiddingStrategy.MAXIMIZE_CONVERSIONS:
            raise ToolError(
                "Error: target_cpa only applies to MAXIMIZE_CONVERSIONS."
            )
        if target_cpa <= 0:
            raise ToolError("Error: target_cpa must be greater than 0.")
        target_cpa_micros = guards.to_micros(target_cpa)

    # ---- Build the atomic request -------------------------------------
    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    ga_service = utils.get_googleads_service_from_client(
        ga_client, "GoogleAdsService"
    )
    enums = ga_client.enums
    budget_rn = f"customers/{cid}/campaignBudgets/{_BUDGET_TMP_ID}"
    campaign_rn = f"customers/{cid}/campaigns/{_CAMPAIGN_TMP_ID}"
    ad_group_rn = f"customers/{cid}/adGroups/{_AD_GROUP_TMP_ID}"
    operations: list[Any] = []

    op = ga_client.get_type("MutateOperation")
    budget = op.campaign_budget_operation.create
    budget.resource_name = budget_rn
    budget.name = f"{name} budget"
    budget.amount_micros = budget_micros
    budget.delivery_method = enums.BudgetDeliveryMethodEnum.STANDARD
    budget.explicitly_shared = False
    operations.append(op)

    op = ga_client.get_type("MutateOperation")
    campaign = op.campaign_operation.create
    campaign.resource_name = campaign_rn
    campaign.name = name
    campaign.advertising_channel_type = enums.AdvertisingChannelTypeEnum.SEARCH
    campaign.status = enums.CampaignStatusEnum.PAUSED
    campaign.campaign_budget = budget_rn
    campaign.network_settings.target_google_search = True
    campaign.network_settings.target_search_network = include_search_partners
    campaign.network_settings.target_content_network = False
    campaign.network_settings.target_partner_search_network = False
    campaign.geo_target_type_setting.positive_geo_target_type = getattr(
        enums.PositiveGeoTargetTypeEnum, GeoTargetType(geo_target_type).value
    )
    campaign.contains_eu_political_advertising = (
        enums.EuPoliticalAdvertisingStatusEnum.CONTAINS_EU_POLITICAL_ADVERTISING
        if contains_eu_political_advertising
        else enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
    )
    if strategy is BiddingStrategy.MANUAL_CPC:
        campaign.manual_cpc.enhanced_cpc_enabled = False
    elif strategy is BiddingStrategy.MAXIMIZE_CLICKS:
        if max_cpc_micros is not None:
            campaign.target_spend.cpc_bid_ceiling_micros = max_cpc_micros
        else:
            ga_client.copy_from(
                campaign.target_spend, ga_client.get_type("TargetSpend")
            )
    else:
        if target_cpa_micros is not None:
            campaign.maximize_conversions.target_cpa_micros = target_cpa_micros
        else:
            ga_client.copy_from(
                campaign.maximize_conversions,
                ga_client.get_type("MaximizeConversions"),
            )
    operations.append(op)

    for loc in locations:
        op = ga_client.get_type("MutateOperation")
        crit = op.campaign_criterion_operation.create
        crit.campaign = campaign_rn
        crit.location.geo_target_constant = f"geoTargetConstants/{loc}"
        operations.append(op)
    for lang in languages:
        op = ga_client.get_type("MutateOperation")
        crit = op.campaign_criterion_operation.create
        crit.campaign = campaign_rn
        crit.language.language_constant = f"languageConstants/{lang}"
        operations.append(op)
    for neg in negs:
        op = ga_client.get_type("MutateOperation")
        crit = op.campaign_criterion_operation.create
        crit.campaign = campaign_rn
        crit.negative = True
        crit.keyword.text = neg["text"]
        crit.keyword.match_type = getattr(
            enums.KeywordMatchTypeEnum, neg["match_type"]
        )
        operations.append(op)

    op = ga_client.get_type("MutateOperation")
    ad_group = op.ad_group_operation.create
    ad_group.resource_name = ad_group_rn
    ad_group.name = group_name
    ad_group.campaign = campaign_rn
    ad_group.type_ = enums.AdGroupTypeEnum.SEARCH_STANDARD
    ad_group.status = enums.AdGroupStatusEnum.ENABLED
    if strategy is BiddingStrategy.MANUAL_CPC and max_cpc_micros is not None:
        ad_group.cpc_bid_micros = max_cpc_micros
    operations.append(op)

    for kw in kws:
        op = ga_client.get_type("MutateOperation")
        crit = op.ad_group_criterion_operation.create
        crit.ad_group = ad_group_rn
        crit.status = enums.AdGroupCriterionStatusEnum.ENABLED
        crit.keyword.text = kw["text"]
        crit.keyword.match_type = getattr(
            enums.KeywordMatchTypeEnum, kw["match_type"]
        )
        operations.append(op)

    op = ga_client.get_type("MutateOperation")
    ad_group_ad = op.ad_group_ad_operation.create
    ad_group_ad.ad_group = ad_group_rn
    ad_group_ad.status = enums.AdGroupAdStatusEnum.ENABLED
    ad_group_ad.ad.final_urls.append(url)
    for text in heads:
        asset = ga_client.get_type("AdTextAsset")
        asset.text = text
        ad_group_ad.ad.responsive_search_ad.headlines.append(asset)
    for text in descs:
        asset = ga_client.get_type("AdTextAsset")
        asset.text = text
        ad_group_ad.ad.responsive_search_ad.descriptions.append(asset)
    if p1:
        ad_group_ad.ad.responsive_search_ad.path1 = p1
    if p2:
        ad_group_ad.ad.responsive_search_ad.path2 = p2
    operations.append(op)

    request = ga_client.get_type("MutateGoogleAdsRequest")
    request.customer_id = cid
    request.mutate_operations.extend(operations)

    # ---- Propose & Confirm ---------------------------------------------
    operation_payload = {
        "tool": "create_search_campaign",
        "campaign_name": name,
        "ad_group_name": group_name,
        "budget_micros": budget_micros,
        "final_url": url,
        "headlines": heads,
        "descriptions": descs,
        "path1": p1,
        "path2": p2,
        "keywords": kws,
        "negative_keywords": negs,
        "location_ids": locations,
        "language_ids": languages,
        "bidding_strategy": strategy.value,
        "max_cpc_micros": max_cpc_micros,
        "target_cpa_micros": target_cpa_micros,
        "include_search_partners": include_search_partners,
        "geo_target_type": GeoTargetType(geo_target_type).value,
        "contains_eu_political_advertising": contains_eu_political_advertising,
    }

    def _kw_list(items: list[dict[str, str]]) -> str:
        return ", ".join(f"{k['text']} [{k['match_type']}]" for k in items)

    change_items = pac.build_change_items(
        operation="CREATE",
        entity_readable="Search campaign (created PAUSED)",
        primary_key_readable=name,
        fields={
            "Campaign name": name,
            "Status": "PAUSED (will not spend until enabled)",
            "Daily budget": f"{guards.from_micros(budget_micros):.2f} (account currency)",
            "Bidding": strategy.value,
            "Max CPC": (
                f"{guards.from_micros(max_cpc_micros):.2f}"
                if max_cpc_micros
                else None
            ),
            "Target CPA": (
                f"{guards.from_micros(target_cpa_micros):.2f}"
                if target_cpa_micros
                else None
            ),
            "Networks": "Google Search"
            + (" + search partners" if include_search_partners else "")
            + "; Display Network off",
            "Locations (geo target IDs)": ", ".join(locations),
            "Location option": GeoTargetType(geo_target_type).value,
            "Languages (language IDs)": ", ".join(languages),
            "Negative keywords": _kw_list(negs) if negs else None,
            "EU political advertising": (
                "Yes" if contains_eu_political_advertising else "No"
            ),
        },
    ) + pac.build_change_items(
        operation="CREATE",
        entity_readable="Ad group + keywords + responsive search ad",
        primary_key_readable=group_name,
        fields={
            "Ad group name": group_name,
            "Keywords": _kw_list(kws),
            "Final URL": url,
            "Display path": "/".join(p for p in (p1, p2) if p) or None,
            "Headlines": " | ".join(heads),
            "Descriptions": " | ".join(descs),
        },
    )

    def _validate() -> None:
        request.validate_only = True
        ga_service.mutate(request=request)

    def _commit() -> str:
        request.validate_only = False
        response = ga_service.mutate(request=request)
        names = _mutate_result_names(response)
        campaign_ids = [
            utils.extract_id(n) for n in names if "/campaigns/" in n
        ]
        ad_group_ids = [utils.extract_id(n) for n in names if "/adGroups/" in n]
        return (
            f"Successfully created PAUSED Search campaign '{name}'. "
            f"Campaign ID: {campaign_ids[0] if campaign_ids else 'unknown'}; "
            f"ad group ID: {ad_group_ids[0] if ad_group_ids else 'unknown'}; "
            f"{len(names)} resources created. It will not spend until "
            "enabled with campaigns_enable_campaign."
        )

    return pac.propose_and_confirm(
        customer_id=cid,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload=operation_payload,
        change_items=change_items,
        validate_fn=_validate,
        commit_fn=_commit,
        error_prefix="Failed to create Search campaign",
    )


@campaigns_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
)
def update_campaign_budget(
    customer_id: str | int,
    campaign_id: str | int,
    new_daily_budget: float,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Changes a campaign's average daily budget.

    Refuses shared budgets (they affect other campaigns), amounts above the
    server's daily cap, and increases above the server's per-change limit.

    Propose & Confirm Workflow: call with `validate_only=True` (default) to get
    `change_items` (old -> new) and a `checksum`; after the user approves, call
    again with the same parameters, `validate_only=False` and the `checksum`.
    If the budget changes in between, the checksum no longer matches and you
    must propose again.

    Args:
        customer_id: Google Ads customer ID.
        campaign_id: The campaign's ID.
        new_daily_budget: New average daily budget in the account's currency.
        login_customer_id: Optional manager account ID.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior validate_only=True call.
    """
    cid = guards.require_write_allowed(customer_id)
    camp_id = _clean_campaign_id(campaign_id)
    new_micros = guards.check_daily_budget(new_daily_budget)

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    current = _fetch_campaign(ga_client, cid, camp_id)
    if current["budget_shared"]:
        raise ToolError(
            f"Error: Campaign {camp_id} uses a shared budget, which other "
            "campaigns also use. Change it in Google Ads instead. No changes "
            "were made."
        )
    if current["budget_micros"] == new_micros:
        raise ToolError(
            f"Error: The budget is already {guards.from_micros(new_micros):.2f}."
        )
    guards.check_budget_increase(current["budget_micros"], new_micros)

    budget_service = utils.get_googleads_service_from_client(
        ga_client, "CampaignBudgetService"
    )
    budget_op = ga_client.get_type("CampaignBudgetOperation")
    budget_op.update.resource_name = current["budget_resource_name"]
    budget_op.update.amount_micros = new_micros
    budget_op.update_mask.paths.append("amount_micros")

    request = ga_client.get_type("MutateCampaignBudgetsRequest")
    request.customer_id = cid
    request.operations.append(budget_op)

    operation_payload = {
        "tool": "update_campaign_budget",
        "campaign_id": camp_id,
        "budget_resource_name": current["budget_resource_name"],
        "old_budget_micros": current["budget_micros"],
        "new_budget_micros": new_micros,
    }
    change_items = pac.build_change_items(
        operation="UPDATE",
        entity_readable="Campaign budget",
        primary_key_readable=f"{current['name']} (ID {camp_id})",
        fields={"Daily budget": f"{guards.from_micros(new_micros):.2f}"},
        original_fields={
            "Daily budget": f"{guards.from_micros(current['budget_micros']):.2f}"
        },
    )

    def _validate() -> None:
        request.validate_only = True
        budget_service.mutate_campaign_budgets(request=request)

    def _commit() -> str:
        request.validate_only = False
        budget_service.mutate_campaign_budgets(request=request)
        return (
            f"Successfully changed the daily budget of '{current['name']}' "
            f"from {guards.from_micros(current['budget_micros']):.2f} to "
            f"{guards.from_micros(new_micros):.2f}."
        )

    return pac.propose_and_confirm(
        customer_id=cid,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload=operation_payload,
        change_items=change_items,
        validate_fn=_validate,
        commit_fn=_commit,
        error_prefix="Failed to update campaign budget",
    )


def _set_campaign_status(
    *,
    tool_name: str,
    target_status: str,
    customer_id: str | int,
    campaign_id: str | int,
    login_customer_id: str | int | None,
    validate_only: bool,
    checksum: str | None,
) -> str:
    cid = guards.require_write_allowed(customer_id)
    camp_id = _clean_campaign_id(campaign_id)

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    current = _fetch_campaign(ga_client, cid, camp_id)
    if current["status"] == "REMOVED":
        raise ToolError(
            f"Error: Campaign {camp_id} is removed and cannot be changed."
        )
    if current["status"] == target_status:
        raise ToolError(
            f"Error: Campaign '{current['name']}' is already {target_status}."
        )

    campaign_service = utils.get_googleads_service_from_client(
        ga_client, "CampaignService"
    )
    campaign_op = ga_client.get_type("CampaignOperation")
    campaign_op.update.resource_name = current["resource_name"]
    campaign_op.update.status = getattr(
        ga_client.enums.CampaignStatusEnum, target_status
    )
    campaign_op.update_mask.paths.append("status")

    request = ga_client.get_type("MutateCampaignsRequest")
    request.customer_id = cid
    request.operations.append(campaign_op)

    operation_payload = {
        "tool": tool_name,
        "campaign_id": camp_id,
        "old_status": current["status"],
        "new_status": target_status,
    }
    fields: dict[str, Any] = {"Status": target_status}
    if target_status == "ENABLED":
        fields["Daily budget (will start spending)"] = (
            f"{guards.from_micros(current['budget_micros']):.2f}"
        )
    change_items = pac.build_change_items(
        operation="UPDATE",
        entity_readable=f"Campaign ({current['channel_type']})",
        primary_key_readable=f"{current['name']} (ID {camp_id})",
        fields=fields,
        original_fields={"Status": current["status"]},
    )

    def _validate() -> None:
        request.validate_only = True
        campaign_service.mutate_campaigns(request=request)

    def _commit() -> str:
        request.validate_only = False
        campaign_service.mutate_campaigns(request=request)
        verb = "enabled (now live)" if target_status == "ENABLED" else "paused"
        return (
            f"Successfully {verb} campaign '{current['name']}' (ID {camp_id})."
        )

    return pac.propose_and_confirm(
        customer_id=cid,
        validate_only=validate_only,
        checksum=checksum,
        operation_payload=operation_payload,
        change_items=change_items,
        validate_fn=_validate,
        commit_fn=_commit,
        error_prefix=f"Failed to set campaign status to {target_status}",
    )


@campaigns_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
)
def pause_campaign(
    customer_id: str | int,
    campaign_id: str | int,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Pauses a campaign so it stops serving and spending.

    Propose & Confirm Workflow: call with `validate_only=True` (default) to get
    `change_items` and a `checksum`; after the user approves, call again with
    the same parameters, `validate_only=False` and the `checksum`.

    Args:
        customer_id: Google Ads customer ID.
        campaign_id: The campaign's ID.
        login_customer_id: Optional manager account ID.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior validate_only=True call.
    """
    return _set_campaign_status(
        tool_name="pause_campaign",
        target_status="PAUSED",
        customer_id=customer_id,
        campaign_id=campaign_id,
        login_customer_id=login_customer_id,
        validate_only=validate_only,
        checksum=checksum,
    )


@campaigns_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
def enable_campaign(
    customer_id: str | int,
    campaign_id: str | int,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Enables a campaign so it goes LIVE and starts spending its budget.

    This is the only tool that makes a campaign live. Only use it when the user
    has explicitly asked to enable this specific campaign.

    Propose & Confirm Workflow: call with `validate_only=True` (default) to get
    `change_items` (including the daily budget that will start spending) and a
    `checksum`; after the user explicitly approves going live, call again with
    the same parameters, `validate_only=False` and the `checksum`.

    Args:
        customer_id: Google Ads customer ID.
        campaign_id: The campaign's ID.
        login_customer_id: Optional manager account ID.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior validate_only=True call.
    """
    return _set_campaign_status(
        tool_name="enable_campaign",
        target_status="ENABLED",
        customer_id=customer_id,
        campaign_id=campaign_id,
        login_customer_id=login_customer_id,
        validate_only=validate_only,
        checksum=checksum,
    )
