"""Tools for creating and managing responsive search ads (RSAs).

Safety rules enforced here (in addition to ads_mcp.guards):
- New ads are created PAUSED unless `start_enabled=True` is passed, and the
  preview says whether the ad would serve straight away.
- Every tool uses Propose & Confirm (validate_only + checksum); the checksum
  covers the ad's current content, so a stale proposal cannot be committed.
- Nothing is removed: ads can be paused, not deleted.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ads_mcp import guards
from ads_mcp import propose_and_confirm as pac
from ads_mcp import write_common as common
from ads_mcp.tools.campaigns import (
    _MAX_DESCRIPTION_LEN,
    _MAX_HEADLINE_LEN,
    _validate_path,
    _validate_texts,
    _validate_url,
)
import ads_mcp.utils as utils

ads_tools_mcp = FastMCP("ads")

_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _validate_pins(
    pins: dict[str, int] | None, texts: list[str], label: str, max_pos: int
) -> dict[str, int]:
    """Checks that each pinned text is in `texts` and its position is valid."""
    result: dict[str, int] = {}
    for text, pos in (pins or {}).items():
        text = text.strip()
        if text not in texts:
            raise ToolError(
                f"Error: Pinned {label} '{text}' is not one of the {label}s."
            )
        if not isinstance(pos, int) or not 1 <= pos <= max_pos:
            raise ToolError(
                f"Error: A {label} can only be pinned to position 1-{max_pos}; "
                f"got {pos!r} for '{text}'."
            )
        result[text] = pos
    return result


def _pin_name(prefix: str, pos: int | None) -> str | None:
    return f"{prefix}_{pos}" if pos else None


def _pin_pos(pinned_field_name: str, prefix: str) -> int | None:
    if pinned_field_name.startswith(prefix + "_"):
        try:
            return int(pinned_field_name.rsplit("_", 1)[1])
        except ValueError:
            return None
    return None


def _fill_assets(
    ga_client: Any,
    target: Any,
    texts: list[str],
    pins: dict[str, int],
    prefix: str,
) -> None:
    enums = ga_client.enums
    for text in texts:
        asset = ga_client.get_type("AdTextAsset")
        asset.text = text
        name = _pin_name(prefix, pins.get(text))
        if name:
            asset.pinned_field = getattr(enums.ServedAssetFieldTypeEnum, name)
        target.append(asset)


def _describe(texts: list[str], pins: dict[str, int], prefix: str) -> str:
    return " | ".join(
        f"{t} (pinned {prefix.lower()} {pins[t]})" if t in pins else t for t in texts
    )


def _fetch_ads(
    ga_client: Any, cid: str, ad_id: str, ad_group_id: str | None
) -> dict[str, Any]:
    where = f"ad_group_ad.ad.id = {ad_id} AND ad_group_ad.status != 'REMOVED'"
    if ad_group_id:
        where += f" AND ad_group.id = {ad_group_id}"
    rows = common.search(
        ga_client,
        cid,
        "SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status, "
        "ad_group_ad.ad.final_urls, ad_group_ad.ad.responsive_search_ad.headlines, "
        "ad_group_ad.ad.responsive_search_ad.descriptions, "
        "ad_group_ad.ad.responsive_search_ad.path1, "
        "ad_group_ad.ad.responsive_search_ad.path2, ad_group.id, ad_group.name, "
        "ad_group.status, campaign.name, campaign.status "
        f"FROM ad_group_ad WHERE {where}",
    )
    if not rows:
        raise ToolError(f"Error: Ad {ad_id} was not found in customer {cid}.")
    if len(rows) > 1:
        raise ToolError(
            f"Error: Ad {ad_id} is in more than one ad group; pass ad_group_id."
        )
    row = rows[0]
    ad = row.ad_group_ad.ad
    rsa = ad.responsive_search_ad
    ag_id = str(row.ad_group.id)
    return {
        "ad_id": ad_id,
        "ad_group_id": ag_id,
        "ad_group_ad_rn": f"customers/{cid}/adGroupAds/{ag_id}~{ad_id}",
        "ad_rn": f"customers/{cid}/ads/{ad_id}",
        "type": ad.type_.name,
        "status": row.ad_group_ad.status.name,
        "final_url": ad.final_urls[0] if len(ad.final_urls) else "",
        "headlines": [h.text for h in rsa.headlines],
        "headline_pins": {
            h.text: p
            for h in rsa.headlines
            if (p := _pin_pos(h.pinned_field.name, "HEADLINE"))
        },
        "descriptions": [d.text for d in rsa.descriptions],
        "description_pins": {
            d.text: p
            for d in rsa.descriptions
            if (p := _pin_pos(d.pinned_field.name, "DESCRIPTION"))
        },
        "path1": rsa.path1 or None,
        "path2": rsa.path2 or None,
        "ad_group_name": row.ad_group.name,
        "ad_group_status": row.ad_group.status.name,
        "campaign_name": row.campaign.name,
        "campaign_status": row.campaign.status.name,
    }


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


@ads_tools_mcp.tool(annotations=_WRITE)
def create_responsive_search_ad(
    customer_id: str | int,
    ad_group_id: str | int,
    final_url: str,
    headlines: list[str],
    descriptions: list[str],
    path1: str | None = None,
    path2: str | None = None,
    pinned_headlines: dict[str, int] | None = None,
    pinned_descriptions: dict[str, int] | None = None,
    start_enabled: bool = False,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Creates a responsive search ad in an existing ad group. Created PAUSED by default.

    Pass `start_enabled=True` only when the user asked for the ad to go live
    straight away; the preview says whether it would then serve. Otherwise
    use `ads_enable_ad` later. Google allows at most 3 enabled RSAs per ad group.

    Propose & Confirm Workflow: `validate_only=True` (default) runs Google's
    dry run and returns `change_items` and a `checksum`; after the user
    approves, call again with the same parameters, `validate_only=False` and
    the `checksum`.

    Args:
        customer_id: Google Ads customer ID.
        ad_group_id: The Search ad group to add the ad to.
        final_url: Landing page URL, starting with https://.
        headlines: 3-15 headlines, each at most 30 characters.
        descriptions: 2-4 descriptions, each at most 90 characters.
        path1: Optional display URL path 1 (max 15 characters).
        path2: Optional display URL path 2 (max 15 characters; needs path1).
        pinned_headlines: Optional {headline text: position 1-3}.
        pinned_descriptions: Optional {description text: position 1-2}.
        start_enabled: False (default) creates the ad PAUSED.
        login_customer_id: Optional manager account ID.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior validate_only=True call.
    """
    cid = guards.require_write_allowed(customer_id)
    ag_id = common.clean_id(ad_group_id, "ad_group_id")
    url = _validate_url(final_url)
    heads = _validate_texts(headlines, "headlines", 3, 15, _MAX_HEADLINE_LEN)
    descs = _validate_texts(descriptions, "descriptions", 2, 4, _MAX_DESCRIPTION_LEN)
    p1 = _validate_path(path1, "path1")
    p2 = _validate_path(path2, "path2")
    if p2 and not p1:
        raise ToolError("Error: path2 needs path1.")
    h_pins = _validate_pins(pinned_headlines, heads, "headline", 3)
    d_pins = _validate_pins(pinned_descriptions, descs, "description", 2)

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    ag = common.fetch_ad_group(ga_client, cid, ag_id)
    if ag["type"] != "SEARCH_STANDARD":
        raise ToolError(
            f"Error: Ad group '{ag['name']}' is {ag['type']}; responsive search "
            "ads need a standard Search ad group."
        )

    enums = ga_client.enums
    status = "ENABLED" if start_enabled else "PAUSED"
    op = ga_client.get_type("AdGroupAdOperation")
    ad_group_ad = op.create
    ad_group_ad.ad_group = ag["resource_name"]
    ad_group_ad.status = getattr(enums.AdGroupAdStatusEnum, status)
    ad_group_ad.ad.final_urls.append(url)
    rsa = ad_group_ad.ad.responsive_search_ad
    _fill_assets(ga_client, rsa.headlines, heads, h_pins, "HEADLINE")
    _fill_assets(ga_client, rsa.descriptions, descs, d_pins, "DESCRIPTION")
    if p1:
        rsa.path1 = p1
    if p2:
        rsa.path2 = p2
    request = ga_client.get_type("MutateAdGroupAdsRequest")
    request.customer_id = cid
    request.operations.append(op)

    change_items = pac.build_change_items(
        operation="CREATE",
        entity_readable="Responsive search ad",
        primary_key_readable=f"{ag['name']} (ad group {ag_id})",
        fields={
            "Campaign": ag["campaign_name"],
            "Status": (
                common.serving_note(ag["campaign_status"], ag["status"])
                if start_enabled
                else "PAUSED (won't serve until enabled)"
            ),
            "Final URL": url,
            "Display path": "/".join(p for p in (p1, p2) if p) or None,
            "Headlines": _describe(heads, h_pins, "HEADLINE"),
            "Descriptions": _describe(descs, d_pins, "DESCRIPTION"),
        },
    )
    return common.run_mutation(
        customer_id=cid,
        validate_only=validate_only,
        checksum=checksum,
        payload={
            "tool": "create_responsive_search_ad",
            "ad_group_id": ag_id,
            "status": status,
            "final_url": url,
            "headlines": heads,
            "descriptions": descs,
            "headline_pins": h_pins,
            "description_pins": d_pins,
            "path1": p1,
            "path2": p2,
        },
        change_items=change_items,
        service=utils.get_googleads_service_from_client(ga_client, "AdGroupAdService"),
        method="mutate_ad_group_ads",
        request=request,
        success=(
            f"Successfully created a {status} responsive search ad in "
            f"'{ag['name']}'."
        ),
        error_prefix="Failed to create responsive search ad",
    )


@ads_tools_mcp.tool(annotations=_WRITE)
def update_responsive_search_ad(
    customer_id: str | int,
    ad_id: str | int,
    headlines: list[str] | None = None,
    descriptions: list[str] | None = None,
    final_url: str | None = None,
    path1: str | None = None,
    path2: str | None = None,
    pinned_headlines: dict[str, int] | None = None,
    pinned_descriptions: dict[str, int] | None = None,
    ad_group_id: str | int | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Edits a responsive search ad's copy, URL or paths in place.

    Only the fields you pass change. `headlines` / `descriptions` replace the
    whole list. Existing pins are kept for texts that stay, unless you pass
    `pinned_headlines` / `pinned_descriptions` (pass {} to clear all pins).
    Pass path1="" or path2="" to clear a path. The edited ad goes back through
    Google's policy review.

    Propose & Confirm Workflow: `validate_only=True` (default) returns
    `change_items` (old -> new) and a `checksum`; after the user approves, call
    again with the same parameters, `validate_only=False` and the `checksum`.
    If the ad changes in between, the proposal is refused.

    Args:
        customer_id: Google Ads customer ID.
        ad_id: The ad's ID (ad_group_ad.ad.id).
        headlines: New full list of 3-15 headlines (max 30 characters each).
        descriptions: New full list of 2-4 descriptions (max 90 characters each).
        final_url: New landing page URL.
        path1: New display path 1, or "" to clear.
        path2: New display path 2, or "" to clear.
        pinned_headlines: {headline text: position 1-3}; {} clears pins.
        pinned_descriptions: {description text: position 1-2}; {} clears pins.
        ad_group_id: Only needed if the ad is in more than one ad group.
        login_customer_id: Optional manager account ID.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior validate_only=True call.
    """
    cid = guards.require_write_allowed(customer_id)
    aid = common.clean_id(ad_id, "ad_id")
    ag_filter = common.clean_id(ad_group_id, "ad_group_id") if ad_group_id else None

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    cur = _fetch_ads(ga_client, cid, aid, ag_filter)
    if cur["type"] != "RESPONSIVE_SEARCH_AD":
        raise ToolError(
            f"Error: Ad {aid} is a {cur['type']}; only responsive search ads "
            "can be edited with this tool."
        )

    new_heads = (
        _validate_texts(headlines, "headlines", 3, 15, _MAX_HEADLINE_LEN)
        if headlines is not None
        else cur["headlines"]
    )
    new_descs = (
        _validate_texts(descriptions, "descriptions", 2, 4, _MAX_DESCRIPTION_LEN)
        if descriptions is not None
        else cur["descriptions"]
    )
    h_pins = (
        _validate_pins(pinned_headlines, new_heads, "headline", 3)
        if pinned_headlines is not None
        else {t: p for t, p in cur["headline_pins"].items() if t in new_heads}
    )
    d_pins = (
        _validate_pins(pinned_descriptions, new_descs, "description", 2)
        if pinned_descriptions is not None
        else {t: p for t, p in cur["description_pins"].items() if t in new_descs}
    )
    new_url = _validate_url(final_url) if final_url is not None else cur["final_url"]
    new_p1 = _validate_path(path1, "path1") if path1 is not None else cur["path1"]
    new_p2 = _validate_path(path2, "path2") if path2 is not None else cur["path2"]
    if new_p2 and not new_p1:
        raise ToolError("Error: path2 needs path1.")

    op = ga_client.get_type("AdOperation")
    ad = op.update
    ad.resource_name = cur["ad_rn"]
    fields: dict[str, Any] = {}
    original: dict[str, Any] = {}

    if new_heads != cur["headlines"] or h_pins != cur["headline_pins"]:
        _fill_assets(
            ga_client, ad.responsive_search_ad.headlines, new_heads, h_pins, "HEADLINE"
        )
        op.update_mask.paths.append("responsive_search_ad.headlines")
        fields["Headlines"] = _describe(new_heads, h_pins, "HEADLINE")
        original["Headlines"] = _describe(
            cur["headlines"], cur["headline_pins"], "HEADLINE"
        )
    if new_descs != cur["descriptions"] or d_pins != cur["description_pins"]:
        _fill_assets(
            ga_client,
            ad.responsive_search_ad.descriptions,
            new_descs,
            d_pins,
            "DESCRIPTION",
        )
        op.update_mask.paths.append("responsive_search_ad.descriptions")
        fields["Descriptions"] = _describe(new_descs, d_pins, "DESCRIPTION")
        original["Descriptions"] = _describe(
            cur["descriptions"], cur["description_pins"], "DESCRIPTION"
        )
    if new_url != cur["final_url"]:
        ad.final_urls.append(new_url)
        op.update_mask.paths.append("final_urls")
        fields["Final URL"] = new_url
        original["Final URL"] = cur["final_url"]
    for label, new, old in (
        ("path1", new_p1, cur["path1"]),
        ("path2", new_p2, cur["path2"]),
    ):
        if new != old:
            setattr(ad.responsive_search_ad, label, new or "")
            op.update_mask.paths.append(f"responsive_search_ad.{label}")
            fields[label] = new or "(none)"
            original[label] = old or "(none)"
    if not fields:
        raise ToolError("Error: Nothing to change; the ad already has these values.")
    fields["Note"] = "The edited ad goes back through Google's policy review"

    request = ga_client.get_type("MutateAdsRequest")
    request.customer_id = cid
    request.operations.append(op)
    change_items = pac.build_change_items(
        operation="UPDATE",
        entity_readable="Responsive search ad",
        primary_key_readable=(
            f"Ad {aid} in {cur['ad_group_name']} ({cur['campaign_name']})"
        ),
        fields=fields,
        original_fields=original,
    )
    return common.run_mutation(
        customer_id=cid,
        validate_only=validate_only,
        checksum=checksum,
        payload={
            "tool": "update_responsive_search_ad",
            "ad_id": aid,
            "before": {
                k: cur[k]
                for k in (
                    "headlines",
                    "headline_pins",
                    "descriptions",
                    "description_pins",
                    "final_url",
                    "path1",
                    "path2",
                )
            },
            "after": {
                "headlines": new_heads,
                "headline_pins": h_pins,
                "descriptions": new_descs,
                "description_pins": d_pins,
                "final_url": new_url,
                "path1": new_p1,
                "path2": new_p2,
            },
        },
        change_items=change_items,
        service=utils.get_googleads_service_from_client(ga_client, "AdService"),
        method="mutate_ads",
        request=request,
        success=f"Successfully updated ad {aid} in '{cur['ad_group_name']}'.",
        error_prefix="Failed to update responsive search ad",
    )


def _set_ad_status(
    *,
    tool_name: str,
    target_status: str,
    customer_id: str | int,
    ad_id: str | int,
    ad_group_id: str | int | None,
    login_customer_id: str | int | None,
    validate_only: bool,
    checksum: str | None,
) -> str:
    cid = guards.require_write_allowed(customer_id)
    aid = common.clean_id(ad_id, "ad_id")
    ag_filter = common.clean_id(ad_group_id, "ad_group_id") if ad_group_id else None

    ga_client = utils.get_googleads_client(login_customer_id=login_customer_id)
    cur = _fetch_ads(ga_client, cid, aid, ag_filter)
    if cur["status"] == target_status:
        raise ToolError(f"Error: Ad {aid} is already {target_status}.")

    op = ga_client.get_type("AdGroupAdOperation")
    op.update.resource_name = cur["ad_group_ad_rn"]
    op.update.status = getattr(ga_client.enums.AdGroupAdStatusEnum, target_status)
    op.update_mask.paths.append("status")
    request = ga_client.get_type("MutateAdGroupAdsRequest")
    request.customer_id = cid
    request.operations.append(op)

    fields: dict[str, Any] = {"Status": target_status}
    if target_status == "ENABLED":
        fields["Serving"] = common.serving_note(
            cur["campaign_status"], cur["ad_group_status"]
        )
    fields["First headline"] = cur["headlines"][0] if cur["headlines"] else None
    change_items = pac.build_change_items(
        operation="UPDATE",
        entity_readable=f"Ad ({cur['type']})",
        primary_key_readable=(
            f"Ad {aid} in {cur['ad_group_name']} ({cur['campaign_name']})"
        ),
        fields=fields,
        original_fields={"Status": cur["status"]},
    )
    verb = "enabled" if target_status == "ENABLED" else "paused"
    return common.run_mutation(
        customer_id=cid,
        validate_only=validate_only,
        checksum=checksum,
        payload={
            "tool": tool_name,
            "ad_group_ad": cur["ad_group_ad_rn"],
            "old_status": cur["status"],
            "new_status": target_status,
        },
        change_items=change_items,
        service=utils.get_googleads_service_from_client(ga_client, "AdGroupAdService"),
        method="mutate_ad_group_ads",
        request=request,
        success=f"Successfully {verb} ad {aid} in '{cur['ad_group_name']}'.",
        error_prefix=f"Failed to set ad status to {target_status}",
    )


@ads_tools_mcp.tool(annotations=_WRITE)
def pause_ad(
    customer_id: str | int,
    ad_id: str | int,
    ad_group_id: str | int | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Pauses an ad so it stops serving (nothing is deleted).

    Propose & Confirm Workflow: `validate_only=True` (default) returns
    `change_items` and a `checksum`; after the user approves, call again with
    the same parameters, `validate_only=False` and the `checksum`.

    Args:
        customer_id: Google Ads customer ID.
        ad_id: The ad's ID (ad_group_ad.ad.id).
        ad_group_id: Only needed if the ad is in more than one ad group.
        login_customer_id: Optional manager account ID.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior validate_only=True call.
    """
    return _set_ad_status(
        tool_name="pause_ad",
        target_status="PAUSED",
        customer_id=customer_id,
        ad_id=ad_id,
        ad_group_id=ad_group_id,
        login_customer_id=login_customer_id,
        validate_only=validate_only,
        checksum=checksum,
    )


@ads_tools_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
def enable_ad(
    customer_id: str | int,
    ad_id: str | int,
    ad_group_id: str | int | None = None,
    login_customer_id: str | int | None = None,
    validate_only: bool = True,
    checksum: str | None = None,
) -> str:
    """Enables a paused ad. If its campaign and ad group are live, it starts serving.

    Only use when the user has asked to enable this specific ad.

    Propose & Confirm Workflow: `validate_only=True` (default) returns
    `change_items` (including whether it will serve straight away) and a
    `checksum`; after the user approves, call again with the same parameters,
    `validate_only=False` and the `checksum`.

    Args:
        customer_id: Google Ads customer ID.
        ad_id: The ad's ID (ad_group_ad.ad.id).
        ad_group_id: Only needed if the ad is in more than one ad group.
        login_customer_id: Optional manager account ID.
        validate_only: True (default) proposes; False commits (needs checksum).
        checksum: Checksum from the prior validate_only=True call.
    """
    return _set_ad_status(
        tool_name="enable_ad",
        target_status="ENABLED",
        customer_id=customer_id,
        ad_id=ad_id,
        ad_group_id=ad_group_id,
        login_customer_id=login_customer_id,
        validate_only=validate_only,
        checksum=checksum,
    )
