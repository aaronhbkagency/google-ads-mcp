"""Tests for the keywords tools."""

import json
import os
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from fastmcp.exceptions import ToolError

from ads_mcp import guards
from ads_mcp import write_common as common
from ads_mcp.tools import keywords
from tests.tools.campaigns_test import CID, _Base


def _enum(name):
    return NS(name=name)


def ad_group_row(bidding="TARGET_SPEND", status="ENABLED", camp_status="ENABLED"):
    return NS(
        ad_group=NS(
            name="Brass handles", status=_enum(status), type_=_enum("SEARCH_STANDARD")
        ),
        campaign=NS(
            id=22,
            name="DoUp Search",
            status=_enum(camp_status),
            bidding_strategy_type=_enum(bidding),
            advertising_channel_type=_enum("SEARCH"),
        ),
    )


def kw_row(text, match="PHRASE", prefix="ad_group_criterion", **extra):
    crit = NS(keyword=NS(text=text, match_type=_enum(match)), **extra)
    return NS(**{prefix: crit})


def criterion_row(kid, text, status="ENABLED", cpc=0, match="PHRASE"):
    return NS(
        ad_group_criterion=NS(
            criterion_id=kid,
            keyword=NS(text=text, match_type=_enum(match)),
            status=_enum(status),
            cpc_bid_micros=cpc,
        )
    )


def campaign_row(cid=22, name="DoUp Search", status="ENABLED"):
    return NS(
        campaign=NS(
            id=cid,
            name=name,
            status=_enum(status),
            bidding_strategy_type=_enum("TARGET_SPEND"),
            advertising_channel_type=_enum("SEARCH"),
        )
    )


class _KwBase(_Base):

    def setUp(self):
        super().setUp()
        # (substring of the GAQL query, rows) checked in order.
        self.routes = []
        self.queries = []

        def search(customer_id, query):
            self.queries.append(query)
            for needle, rows in self.routes:
                if needle in query:
                    return rows
            return []

        self.svc("GoogleAdsService").search.side_effect = search

    def route(self, needle, rows):
        self.routes.append((needle, rows))


class TestAddKeywords(_KwBase):

    def test_propose_skips_duplicates_then_commits(self):
        self.route("FROM ad_group WHERE", [ad_group_row()])
        self.route("FROM ad_group_criterion WHERE", [kw_row("brass handles")])
        svc = self.svc("AdGroupCriterionService")
        args = dict(
            customer_id=CID,
            ad_group_id="33",
            keywords=[
                {"text": "Brass  Handles", "match_type": "PHRASE"},
                {"text": "brass knobs", "match_type": "EXACT"},
            ],
        )
        proposal = json.loads(keywords.add_keywords(**args))
        fields = {
            i["field_readable"]: i["value_readable"] for i in proposal["change_items"]
        }
        self.assertEqual(fields["Keywords to add"], "brass knobs [EXACT]")
        self.assertIn("Brass Handles", fields["Already in ad group (skipped)"])
        self.assertIn("LIVE", fields["Serving"])

        request = svc.mutate_ad_group_criteria.call_args.kwargs["request"]
        self.assertTrue(request.validate_only)
        self.assertEqual(len(request.operations), 1)
        crit = request.operations[0].create
        self.assertEqual(crit.ad_group, f"customers/{CID}/adGroups/33")
        self.assertEqual(crit.keyword.text, "brass knobs")
        self.assertFalse(crit.negative)

        with self.assertRaisesRegex(ToolError, "checksum"):
            keywords.add_keywords(**args, validate_only=False)
        result = keywords.add_keywords(
            **args, validate_only=False, checksum=proposal["checksum"]
        )
        self.assertIn("added 1 keyword", result)
        self.assertFalse(
            svc.mutate_ad_group_criteria.call_args.kwargs["request"].validate_only
        )

    def test_paused_campaign_note(self):
        self.route("FROM ad_group WHERE", [ad_group_row(camp_status="PAUSED")])
        proposal = json.loads(
            keywords.add_keywords(CID, "33", [{"text": "brass knobs"}])
        )
        fields = {
            i["field_readable"]: i["value_readable"] for i in proposal["change_items"]
        }
        self.assertIn("campaign is PAUSED", fields["Serving"])

    def test_refusals(self):
        self.route("FROM ad_group WHERE", [ad_group_row(bidding="TARGET_SPEND")])
        broad = [{"text": "door handles", "match_type": "BROAD"}]
        with self.assertRaisesRegex(ToolError, "BROAD"):
            keywords.add_keywords(CID, "33", broad)
        with self.assertRaisesRegex(ToolError, "Manual CPC"):
            keywords.add_keywords(CID, "33", [{"text": "x", "cpc_bid": 2}])
        with self.assertRaisesRegex(ToolError, "cap"):
            keywords.add_keywords(CID, "33", [{"text": "x", "cpc_bid": 500}])
        with self.assertRaisesRegex(ToolError, "write allowlist"):
            keywords.add_keywords("1112223333", "33", [{"text": "x"}])
        with self.assertRaisesRegex(ToolError, "at least one keyword"):
            keywords.add_keywords(CID, "33", [])

    def test_broad_allowed_with_smart_bidding_and_bids_with_manual(self):
        self.route(
            "FROM ad_group WHERE", [ad_group_row(bidding="MAXIMIZE_CONVERSIONS")]
        )
        proposal = json.loads(
            keywords.add_keywords(
                CID, "33", [{"text": "door handles", "match_type": "BROAD"}]
            )
        )
        self.assertEqual(proposal["status"], "PROPOSED")

        self.routes.clear()
        self.route("FROM ad_group WHERE", [ad_group_row(bidding="MANUAL_CPC")])
        keywords.add_keywords(CID, "33", [{"text": "brass knobs", "cpc_bid": 1.25}])
        request = self.svc(
            "AdGroupCriterionService"
        ).mutate_ad_group_criteria.call_args.kwargs["request"]
        self.assertEqual(request.operations[0].create.cpc_bid_micros, 1_250_000)

    def test_all_duplicates(self):
        self.route("FROM ad_group WHERE", [ad_group_row()])
        self.route("FROM ad_group_criterion WHERE", [kw_row("brass knobs", "EXACT")])
        with self.assertRaisesRegex(ToolError, "already in the ad group"):
            keywords.add_keywords(
                CID, "33", [{"text": "brass knobs", "match_type": "EXACT"}]
            )


class TestKeywordStatusAndBids(_KwBase):

    def test_pause_skips_already_paused(self):
        self.route("FROM ad_group WHERE", [ad_group_row()])
        self.route(
            "criterion_id IN",
            [
                criterion_row(1, "brass knobs"),
                criterion_row(2, "brass pulls", "PAUSED"),
            ],
        )
        proposal = json.loads(keywords.pause_keywords(CID, "33", ["1", "2"]))
        self.assertEqual(len(proposal["change_items"]), 1)
        item = proposal["change_items"][0]
        self.assertEqual(item["original_value_readable"], "ENABLED")
        request = self.svc(
            "AdGroupCriterionService"
        ).mutate_ad_group_criteria.call_args.kwargs["request"]
        op = request.operations[0]
        self.assertEqual(
            op.update.resource_name, f"customers/{CID}/adGroupCriteria/33~1"
        )
        self.assertEqual(list(op.update_mask.paths), ["status"])

    def test_enable_shows_serving_and_errors(self):
        self.route("FROM ad_group WHERE", [ad_group_row()])
        self.route("criterion_id IN", [criterion_row(2, "brass pulls", "PAUSED")])
        proposal = json.loads(keywords.enable_keywords(CID, "33", ["2"]))
        fields = [i["field_readable"] for i in proposal["change_items"]]
        self.assertIn("Serving", fields)
        with self.assertRaisesRegex(ToolError, "already PAUSED"):
            keywords.pause_keywords(CID, "33", ["2"])
        with self.assertRaisesRegex(ToolError, "not found"):
            keywords.pause_keywords(CID, "33", ["2", "9"])

    def test_bids_manual_only_and_stale_checksum(self):
        self.route("FROM ad_group WHERE", [ad_group_row(bidding="TARGET_SPEND")])
        with self.assertRaisesRegex(ToolError, "Manual CPC"):
            keywords.update_keyword_bids(CID, "33", [{"keyword_id": "1", "cpc_bid": 2}])

        self.routes.clear()
        self.route("FROM ad_group WHERE", [ad_group_row(bidding="MANUAL_CPC")])
        rows = [criterion_row(1, "brass knobs", cpc=1_000_000)]
        self.route("criterion_id IN", rows)
        bids = [{"keyword_id": "1", "cpc_bid": 2}]
        proposal = json.loads(keywords.update_keyword_bids(CID, "33", bids))
        item = proposal["change_items"][0]
        self.assertEqual(item["original_value_readable"], "1.00")
        self.assertEqual(item["value_readable"], "2.00")
        rows[0] = criterion_row(1, "brass knobs", cpc=1_500_000)
        with self.assertRaisesRegex(ToolError, "mismatched"):
            keywords.update_keyword_bids(
                CID, "33", bids, validate_only=False, checksum=proposal["checksum"]
            )
        with self.assertRaisesRegex(ToolError, "cap"):
            keywords.update_keyword_bids(
                CID, "33", [{"keyword_id": "1", "cpc_bid": 101}]
            )
        with patch.dict(os.environ, {guards.MAX_CPC_ENV: "1.5"}):
            with self.assertRaisesRegex(ToolError, "GOOGLE_ADS_MCP_MAX_CPC"):
                keywords.update_keyword_bids(CID, "33", bids)


class TestNegatives(_KwBase):

    def test_campaign_level_with_conflict_warning(self):
        self.route("FROM campaign WHERE", [campaign_row()])
        self.route(
            "FROM campaign_criterion", [kw_row("free", "BROAD", "campaign_criterion")]
        )
        self.route(
            "SELECT ad_group_criterion.keyword.text FROM",
            [kw_row("cheap brass handles"), kw_row("brass knobs")],
        )
        negs = [
            {"text": "cheap", "match_type": "PHRASE"},
            {"text": "free", "match_type": "BROAD"},
            {"text": "diy", "match_type": "EXACT"},
        ]
        proposal = json.loads(
            keywords.add_negative_keywords(CID, negs, campaign_id="22")
        )
        fields = {
            i["field_readable"]: i["value_readable"] for i in proposal["change_items"]
        }
        self.assertEqual(fields["Negatives to add"], "cheap [PHRASE], diy [EXACT]")
        self.assertEqual(fields["Already there (skipped)"], "free [BROAD]")
        self.assertIn(
            "'cheap brass handles'", fields["WARNING: would block existing keywords"]
        )
        request = self.svc(
            "CampaignCriterionService"
        ).mutate_campaign_criteria.call_args.kwargs["request"]
        self.assertEqual(len(request.operations), 2)
        self.assertTrue(request.operations[0].create.negative)
        self.assertEqual(
            request.operations[0].create.campaign, f"customers/{CID}/campaigns/22"
        )

    def test_ad_group_level_and_target_rules(self):
        self.route("FROM ad_group WHERE", [ad_group_row()])
        keywords.add_negative_keywords(CID, [{"text": "diy"}], ad_group_id="33")
        request = self.svc(
            "AdGroupCriterionService"
        ).mutate_ad_group_criteria.call_args.kwargs["request"]
        self.assertTrue(request.operations[0].create.negative)
        with self.assertRaisesRegex(ToolError, "exactly one"):
            keywords.add_negative_keywords(CID, [{"text": "diy"}])
        with self.assertRaisesRegex(ToolError, "exactly one"):
            keywords.add_negative_keywords(
                CID, [{"text": "diy"}], campaign_id="22", ad_group_id="33"
            )

    def test_create_list_is_atomic(self):
        self.route(
            "FROM campaign WHERE",
            [campaign_row(22, "A"), campaign_row(23, "B")],
        )
        proposal = json.loads(
            keywords.create_negative_keyword_list(
                CID,
                "Brand safety",
                [{"text": "free"}, {"text": "diy", "match_type": "EXACT"}],
                campaign_ids=["22", "23"],
            )
        )
        self.assertEqual(proposal["status"], "PROPOSED")
        request = self.svc("GoogleAdsService").mutate.call_args.kwargs["request"]
        ops = list(request.mutate_operations)
        self.assertEqual(len(ops), 5)
        shared = ops[0].shared_set_operation.create
        self.assertEqual(shared.name, "Brand safety")
        self.assertEqual(
            shared.type_, self.real_client.enums.SharedSetTypeEnum.NEGATIVE_KEYWORDS
        )
        self.assertEqual(
            ops[1].shared_criterion_operation.create.shared_set, shared.resource_name
        )
        self.assertEqual(
            ops[4].campaign_shared_set_operation.create.campaign,
            f"customers/{CID}/campaigns/23",
        )
        with self.assertRaisesRegex(ToolError, "not found"):
            keywords.create_negative_keyword_list(
                CID, "X", [{"text": "free"}], campaign_ids=["99"]
            )

    def test_add_to_list(self):
        self.route(
            "FROM shared_set WHERE",
            [
                NS(
                    shared_set=NS(
                        name="Brand safety",
                        type_=_enum("NEGATIVE_KEYWORDS"),
                        status=_enum("ENABLED"),
                    )
                )
            ],
        )
        self.route(
            "FROM shared_criterion", [kw_row("free", "PHRASE", "shared_criterion")]
        )
        self.route("FROM campaign_shared_set", [NS(campaign=NS(id=22))])
        self.route("FROM campaign WHERE", [campaign_row()])
        proposal = json.loads(
            keywords.add_to_negative_keyword_list(
                CID, "77", [{"text": "free"}, {"text": "jobs"}]
            )
        )
        fields = {
            i["field_readable"]: i["value_readable"] for i in proposal["change_items"]
        }
        self.assertEqual(fields["Negatives to add"], "jobs [PHRASE]")
        self.assertEqual(fields["Campaigns affected"], "DoUp Search")
        request = self.svc(
            "SharedCriterionService"
        ).mutate_shared_criteria.call_args.kwargs["request"]
        self.assertEqual(
            request.operations[0].create.shared_set, f"customers/{CID}/sharedSets/77"
        )

    def test_list_type_checked(self):
        self.route(
            "FROM shared_set WHERE",
            [
                NS(
                    shared_set=NS(
                        name="Placements",
                        type_=_enum("NEGATIVE_PLACEMENTS"),
                        status=_enum("ENABLED"),
                    )
                )
            ],
        )
        with self.assertRaisesRegex(ToolError, "not a negative keyword list"):
            keywords.add_to_negative_keyword_list(CID, "77", [{"text": "jobs"}])

    def test_apply_list_skips_attached(self):
        self.route(
            "FROM shared_set WHERE",
            [
                NS(
                    shared_set=NS(
                        name="Brand safety",
                        type_=_enum("NEGATIVE_KEYWORDS"),
                        status=_enum("ENABLED"),
                    )
                )
            ],
        )
        self.route(
            "FROM campaign WHERE", [campaign_row(22, "A"), campaign_row(23, "B")]
        )
        self.route("FROM campaign_shared_set", [NS(campaign=NS(id=22))])
        self.route(
            "FROM shared_criterion", [kw_row("free", "BROAD", "shared_criterion")]
        )
        self.route(
            "SELECT ad_group_criterion.keyword.text FROM",
            [kw_row("free delivery handles")],
        )
        proposal = json.loads(
            keywords.apply_negative_keyword_list(CID, "77", ["22", "23"])
        )
        fields = {
            i["field_readable"]: i["value_readable"] for i in proposal["change_items"]
        }
        self.assertEqual(fields["Apply to campaigns"], "B")
        self.assertEqual(fields["Already using it (skipped)"], "A")
        self.assertIn(
            "free delivery handles", fields["WARNING: would block existing keywords"]
        )
        request = self.svc(
            "CampaignSharedSetService"
        ).mutate_campaign_shared_sets.call_args.kwargs["request"]
        self.assertEqual(len(request.operations), 1)
        with self.assertRaisesRegex(ToolError, "already use"):
            keywords.apply_negative_keyword_list(CID, "77", ["22"])


class TestNegativeMatching(unittest.TestCase):

    def test_negative_blocks(self):
        cases = [
            ("cheap", "BROAD", "cheap brass handles", True),
            ("cheap handles", "BROAD", "handles that are cheap", True),
            ("cheap handles", "PHRASE", "handles that are cheap", False),
            ("brass handles", "PHRASE", "cheap brass handles", True),
            ("brass handles", "EXACT", "cheap brass handles", False),
            ("Brass Handles", "EXACT", "brass handles", True),
        ]
        for neg, match, kw, expected in cases:
            with self.subTest(neg=neg, match=match, kw=kw):
                self.assertEqual(common.negative_blocks(neg, match, kw), expected)


class TestCpcGuard(unittest.TestCase):

    def test_cpc_cap(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ToolError, "disabled"):
                guards.check_cpc_bid(1)
        with patch.dict(os.environ, {guards.MAX_DAILY_BUDGET_ENV: "50"}, clear=True):
            self.assertEqual(guards.check_cpc_bid(2.5), 2_500_000)
            with self.assertRaisesRegex(ToolError, "MAX_DAILY_BUDGET"):
                guards.check_cpc_bid(60)
        with patch.dict(
            os.environ,
            {guards.MAX_DAILY_BUDGET_ENV: "50", guards.MAX_CPC_ENV: "5"},
            clear=True,
        ):
            with self.assertRaisesRegex(ToolError, "MAX_CPC"):
                guards.check_cpc_bid(6)
            with self.assertRaisesRegex(ToolError, "greater than 0"):
                guards.check_cpc_bid(0)


if __name__ == "__main__":
    unittest.main()
