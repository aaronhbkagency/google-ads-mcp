"""Tests for the ads (responsive search ad) tools."""

import json
import unittest
from types import SimpleNamespace as NS

from fastmcp.exceptions import ToolError

from ads_mcp.tools import ads
from tests.tools.campaigns_test import CID
from tests.tools.keywords_test import _KwBase, _enum, ad_group_row

HEADS = ["Brass Cabinet Handles", "Free AU Shipping", "Shop DoUp"]
DESCS = ["Solid brass handles made to last.", "Order online today."]


def _asset(text, pin="UNSPECIFIED"):
    return NS(text=text, pinned_field=_enum(pin))


def ad_row(
    status="ENABLED",
    ad_type="RESPONSIVE_SEARCH_AD",
    heads=None,
    descs=None,
    url="https://doup.com.au/brass",
    path1="brass",
    path2="",
    ad_group_id=33,
):
    heads = (
        heads
        if heads is not None
        else [
            _asset(HEADS[0], "HEADLINE_1"),
            _asset(HEADS[1]),
            _asset(HEADS[2]),
        ]
    )
    descs = descs if descs is not None else [_asset(d) for d in DESCS]
    return NS(
        ad_group_ad=NS(
            status=_enum(status),
            ad=NS(
                type_=_enum(ad_type),
                final_urls=[url],
                responsive_search_ad=NS(
                    headlines=heads, descriptions=descs, path1=path1, path2=path2
                ),
            ),
        ),
        ad_group=NS(id=ad_group_id, name="Brass handles", status=_enum("ENABLED")),
        campaign=NS(name="DoUp Search", status=_enum("ENABLED")),
    )


def _fields(proposal):
    return {i["field_readable"]: i for i in proposal["change_items"]}


class TestCreateRsa(_KwBase):

    def _args(self, **kw):
        args = dict(
            customer_id=CID,
            ad_group_id="33",
            final_url="https://doup.com.au/brass",
            headlines=HEADS,
            descriptions=DESCS,
            pinned_headlines={"Shop DoUp": 1},
        )
        args.update(kw)
        return args

    def test_created_paused_with_pins_then_commit(self):
        self.route("FROM ad_group WHERE", [ad_group_row()])
        svc = self.svc("AdGroupAdService")
        proposal = json.loads(ads.create_responsive_search_ad(**self._args()))
        fields = _fields(proposal)
        self.assertIn("PAUSED", fields["Status"]["value_readable"])
        self.assertIn(
            "Shop DoUp (pinned headline 1)", fields["Headlines"]["value_readable"]
        )

        request = svc.mutate_ad_group_ads.call_args.kwargs["request"]
        create = request.operations[0].create
        enums = self.real_client.enums
        self.assertEqual(create.status, enums.AdGroupAdStatusEnum.PAUSED)
        self.assertEqual(create.ad_group, f"customers/{CID}/adGroups/33")
        pinned = [h for h in create.ad.responsive_search_ad.headlines if h.pinned_field]
        self.assertEqual(len(pinned), 1)
        self.assertEqual(
            pinned[0].pinned_field, enums.ServedAssetFieldTypeEnum.HEADLINE_1
        )

        with self.assertRaisesRegex(ToolError, "mismatched"):
            ads.create_responsive_search_ad(
                **self._args(start_enabled=True),
                validate_only=False,
                checksum=proposal["checksum"],
            )
        result = ads.create_responsive_search_ad(
            **self._args(), validate_only=False, checksum=proposal["checksum"]
        )
        self.assertIn("PAUSED responsive search ad", result)

    def test_start_enabled_shows_serving(self):
        self.route("FROM ad_group WHERE", [ad_group_row()])
        proposal = json.loads(
            ads.create_responsive_search_ad(**self._args(start_enabled=True))
        )
        self.assertIn("LIVE", _fields(proposal)["Status"]["value_readable"])

    def test_refusals(self):
        cases = [
            (dict(pinned_headlines={"Not a headline": 1}), "not one of the headlines"),
            (dict(pinned_headlines={"Shop DoUp": 4}), "position 1-3"),
            (dict(headlines=["One", "Two"]), "3-15 headlines"),
            (dict(final_url="doup.com.au"), "https"),
            (dict(customer_id="1112223333"), "write allowlist"),
        ]
        for overrides, message in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(ToolError, message):
                    ads.create_responsive_search_ad(**self._args(**overrides))
        row = ad_group_row()
        row.ad_group.type_ = _enum("DISPLAY_STANDARD")
        self.route("FROM ad_group WHERE", [row])
        with self.assertRaisesRegex(ToolError, "standard Search ad group"):
            ads.create_responsive_search_ad(**self._args())


class TestUpdateRsa(_KwBase):

    def test_only_changed_fields_and_pins_kept(self):
        self.route("FROM ad_group_ad", [ad_row()])
        new_heads = [HEADS[0], "Australian Made", "Shop DoUp"]
        proposal = json.loads(
            ads.update_responsive_search_ad(
                CID, "44", headlines=new_heads, path2="cabinet"
            )
        )
        fields = _fields(proposal)
        self.assertIn(
            "Brass Cabinet Handles (pinned headline 1)",
            fields["Headlines"]["value_readable"],
        )
        self.assertIn(
            "Free AU Shipping", fields["Headlines"]["original_value_readable"]
        )
        self.assertEqual(fields["path2"]["original_value_readable"], "(none)")
        self.assertNotIn("Final URL", fields)

        request = self.svc("AdService").mutate_ads.call_args.kwargs["request"]
        op = request.operations[0]
        self.assertEqual(op.update.resource_name, f"customers/{CID}/ads/44")
        self.assertEqual(
            sorted(op.update_mask.paths),
            ["responsive_search_ad.headlines", "responsive_search_ad.path2"],
        )

    def test_stale_ad_invalidates_checksum(self):
        rows = [ad_row()]
        self.route("FROM ad_group_ad", rows)
        proposal = json.loads(
            ads.update_responsive_search_ad(
                CID, "44", final_url="https://doup.com.au/new"
            )
        )
        rows[0] = ad_row(url="https://doup.com.au/changed-elsewhere")
        with self.assertRaisesRegex(ToolError, "mismatched"):
            ads.update_responsive_search_ad(
                CID,
                "44",
                final_url="https://doup.com.au/new",
                validate_only=False,
                checksum=proposal["checksum"],
            )

    def test_refusals(self):
        self.route("FROM ad_group_ad", [ad_row()])
        with self.assertRaisesRegex(ToolError, "Nothing to change"):
            ads.update_responsive_search_ad(CID, "44", path1="brass")
        self.routes.clear()
        self.route("FROM ad_group_ad", [ad_row(ad_type="EXPANDED_TEXT_AD")])
        with self.assertRaisesRegex(ToolError, "only responsive search ads"):
            ads.update_responsive_search_ad(CID, "44", path1="x")
        self.routes.clear()
        self.route("FROM ad_group_ad", [ad_row(), ad_row(ad_group_id=34)])
        with self.assertRaisesRegex(ToolError, "pass ad_group_id"):
            ads.update_responsive_search_ad(CID, "44", path1="x")


class TestAdStatus(_KwBase):

    def test_pause_and_enable(self):
        self.route("FROM ad_group_ad", [ad_row(status="ENABLED")])
        proposal = json.loads(ads.pause_ad(CID, "44"))
        self.assertEqual(
            _fields(proposal)["Status"]["original_value_readable"], "ENABLED"
        )
        request = self.svc("AdGroupAdService").mutate_ad_group_ads.call_args.kwargs[
            "request"
        ]
        self.assertEqual(
            request.operations[0].update.resource_name,
            f"customers/{CID}/adGroupAds/33~44",
        )
        with self.assertRaisesRegex(ToolError, "already ENABLED"):
            ads.enable_ad(CID, "44")

        self.routes.clear()
        self.route("FROM ad_group_ad", [ad_row(status="PAUSED")])
        proposal = json.loads(ads.enable_ad(CID, "44"))
        self.assertIn("LIVE", _fields(proposal)["Serving"]["value_readable"])
        result = ads.enable_ad(
            CID, "44", validate_only=False, checksum=proposal["checksum"]
        )
        self.assertIn("enabled ad 44", result)


if __name__ == "__main__":
    unittest.main()
