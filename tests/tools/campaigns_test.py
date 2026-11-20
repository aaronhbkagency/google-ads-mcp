"""Tests for the campaigns tools."""

import json
import os
import unittest
from unittest.mock import MagicMock, patch

from fastmcp.exceptions import ToolError
from google.ads.googleads.client import GoogleAdsClient
from google.auth.credentials import AnonymousCredentials

from ads_mcp import guards
from ads_mcp.tools import campaigns

CID = "2603945345"
ENV = {
    guards.ALLOWLIST_ENV: "260-394-5345",
    guards.MAX_DAILY_BUDGET_ENV: "100",
}


def _base_args(**overrides):
    args = dict(
        customer_id=CID,
        campaign_name="DoUp - Brass Handles",
        daily_budget=25,
        final_url="https://doup.com.au/brass-handles",
        headlines=["Brass Cabinet Handles", "Free AU Shipping", "Shop DoUp"],
        descriptions=[
            "Solid brass handles made to last.",
            "Order online today.",
        ],
        keywords=[
            {"text": "brass cabinet handles", "match_type": "PHRASE"},
            {"text": "brass drawer pulls", "match_type": "EXACT"},
        ],
        location_ids=["2036"],
        contains_eu_political_advertising=False,
    )
    args.update(overrides)
    return args


class _Base(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.real_client = GoogleAdsClient(
            credentials=AnonymousCredentials(),
            developer_token="fake-token",
            use_proto_plus=True,
        )

    def setUp(self):
        env_patch = patch.dict(os.environ, ENV, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)

        self.client = MagicMock()
        self.client.get_type.side_effect = self.real_client.get_type
        self.client.enums = self.real_client.enums
        self.client.copy_from.side_effect = self.real_client.copy_from
        self.services = {}

        def get_service(name, **kwargs):
            return self.services.setdefault(name, MagicMock())

        self.client.get_service.side_effect = get_service
        client_patch = patch(
            "ads_mcp.utils.get_googleads_client", return_value=self.client
        )
        client_patch.start()
        self.addCleanup(client_patch.stop)

    def svc(self, name):
        return self.client.get_service(name)


class TestCreateSearchCampaign(_Base):

    def _mutate_response(self):
        names = [
            f"customers/{CID}/campaignBudgets/11",
            f"customers/{CID}/campaigns/22",
            f"customers/{CID}/adGroups/33",
        ]
        response = MagicMock()
        responses = []
        for n in names:
            r = self.real_client.get_type("MutateOperationResponse")
            if "campaignBudgets" in n:
                r.campaign_budget_result.resource_name = n
            elif "campaigns" in n:
                r.campaign_result.resource_name = n
            else:
                r.ad_group_result.resource_name = n
            responses.append(r)
        response.mutate_operation_responses = responses
        return response

    def test_propose_then_commit(self):
        ga = self.svc("GoogleAdsService")
        captured = []
        ga.mutate.side_effect = lambda request: (
            captured.append((request.validate_only, type(request).copy_from))
            or self._mutate_response()
        )

        proposal = json.loads(campaigns.create_search_campaign(**_base_args()))
        self.assertEqual(proposal["status"], "PROPOSED")
        self.assertEqual(captured[0][0], True)  # Google dry run ran
        fields = {
            i["field_readable"]: i["value_readable"]
            for i in proposal["change_items"]
        }
        self.assertIn("PAUSED", fields["Status"])
        self.assertEqual(fields["Daily budget"], "25.00 (account currency)")

        request = ga.mutate.call_args.kwargs["request"]
        ops = list(request.mutate_operations)
        campaign = ops[1].campaign_operation.create
        enums = self.real_client.enums
        self.assertEqual(campaign.status, enums.CampaignStatusEnum.PAUSED)
        self.assertFalse(campaign.network_settings.target_content_network)
        self.assertFalse(campaign.network_settings.target_search_network)
        self.assertEqual(
            campaign.contains_eu_political_advertising,
            enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING,
        )
        self.assertEqual(
            ops[0].campaign_budget_operation.create.amount_micros, 25_000_000
        )
        locs = [
            o.campaign_criterion_operation.create.location.geo_target_constant
            for o in ops
            if o.campaign_criterion_operation.create.location.geo_target_constant
        ]
        self.assertEqual(locs, ["geoTargetConstants/2036"])
        langs = [
            o.campaign_criterion_operation.create.language.language_constant
            for o in ops
            if o.campaign_criterion_operation.create.language.language_constant
        ]
        self.assertEqual(langs, ["languageConstants/1000"])
        rsa = ops[-1].ad_group_ad_operation.create.ad.responsive_search_ad
        self.assertEqual(len(rsa.headlines), 3)

        with self.assertRaisesRegex(ToolError, "checksum"):
            campaigns.create_search_campaign(**_base_args(validate_only=False))
        with self.assertRaisesRegex(ToolError, "mismatched"):
            campaigns.create_search_campaign(
                **_base_args(
                    daily_budget=30,
                    validate_only=False,
                    checksum=proposal["checksum"],
                )
            )

        result = campaigns.create_search_campaign(
            **_base_args(validate_only=False, checksum=proposal["checksum"])
        )
        self.assertIn("Campaign ID: 22", result)
        self.assertIn("PAUSED", result)
        self.assertEqual(captured[-1][0], False)

    def test_not_allowlisted(self):
        with self.assertRaisesRegex(ToolError, "write allowlist"):
            campaigns.create_search_campaign(
                **_base_args(customer_id="1112223333")
            )
        self.svc("GoogleAdsService").mutate.assert_not_called()

    def test_budget_over_cap(self):
        with self.assertRaisesRegex(ToolError, "cap"):
            campaigns.create_search_campaign(**_base_args(daily_budget=500))

    def test_broad_match_needs_smart_bidding(self):
        kws = [{"text": "door handles", "match_type": "BROAD"}]
        with self.assertRaisesRegex(ToolError, "BROAD"):
            campaigns.create_search_campaign(**_base_args(keywords=kws))
        self.svc("GoogleAdsService").mutate.return_value = MagicMock()
        proposal = json.loads(
            campaigns.create_search_campaign(
                **_base_args(
                    keywords=kws, bidding_strategy="MAXIMIZE_CONVERSIONS"
                )
            )
        )
        self.assertEqual(proposal["status"], "PROPOSED")

    def test_input_validation(self):
        cases = [
            (dict(location_ids=[]), "location"),
            (dict(headlines=["One", "Two"]), "3-15 headlines"),
            (dict(headlines=["x" * 31, "b", "c"]), "over 30"),
            (dict(descriptions=["only one"]), "2-4 descriptions"),
            (dict(final_url="doup.com.au"), "https"),
            (dict(keywords=[]), "at least one keyword"),
            (dict(bidding_strategy="MANUAL_CPC"), "max_cpc"),
            (dict(target_cpa=20), "MAXIMIZE_CONVERSIONS"),
            (dict(path2="shop"), "path2 needs path1"),
            (dict(path1="a/b"), "path1"),
        ]
        for overrides, message in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(ToolError, message):
                    campaigns.create_search_campaign(**_base_args(**overrides))

    def test_manual_cpc_sets_ad_group_bid(self):
        ga = self.svc("GoogleAdsService")
        campaigns.create_search_campaign(
            **_base_args(bidding_strategy="MANUAL_CPC", max_cpc=1.5)
        )
        ops = list(ga.mutate.call_args.kwargs["request"].mutate_operations)
        ad_group = [
            o.ad_group_operation.create
            for o in ops
            if o.ad_group_operation.create.name
        ][0]
        self.assertEqual(ad_group.cpc_bid_micros, 1_500_000)


def _search_row(status="PAUSED", budget_micros=20_000_000, shared=False):
    row = MagicMock()
    row.campaign.name = "DoUp - Brass Handles"
    row.campaign.status.name = status
    row.campaign.advertising_channel_type.name = "SEARCH"
    row.campaign_budget.resource_name = f"customers/{CID}/campaignBudgets/11"
    row.campaign_budget.amount_micros = budget_micros
    row.campaign_budget.explicitly_shared = shared
    return row


class TestUpdateBudget(_Base):

    def test_propose_and_commit(self):
        self.svc("GoogleAdsService").search.return_value = [_search_row()]
        budget_svc = self.svc("CampaignBudgetService")

        proposal = json.loads(campaigns.update_campaign_budget(CID, "22", 30))
        item = proposal["change_items"][0]
        self.assertEqual(item["original_value_readable"], "20.00")
        self.assertEqual(item["value_readable"], "30.00")
        request = budget_svc.mutate_campaign_budgets.call_args.kwargs["request"]
        self.assertEqual(request.operations[0].update.amount_micros, 30_000_000)
        self.assertEqual(
            list(request.operations[0].update_mask.paths), ["amount_micros"]
        )

        result = campaigns.update_campaign_budget(
            CID, "22", 30, validate_only=False, checksum=proposal["checksum"]
        )
        self.assertIn("from 20.00 to 30.00", result)

    def test_stale_budget_invalidates_checksum(self):
        ga = self.svc("GoogleAdsService")
        ga.search.return_value = [_search_row(budget_micros=20_000_000)]
        proposal = json.loads(campaigns.update_campaign_budget(CID, "22", 30))
        ga.search.return_value = [_search_row(budget_micros=10_000_000)]
        with self.assertRaisesRegex(ToolError, "mismatched"):
            campaigns.update_campaign_budget(
                CID,
                "22",
                30,
                validate_only=False,
                checksum=proposal["checksum"],
            )

    def test_refusals(self):
        ga = self.svc("GoogleAdsService")
        ga.search.return_value = [_search_row(shared=True)]
        with self.assertRaisesRegex(ToolError, "shared budget"):
            campaigns.update_campaign_budget(CID, "22", 30)
        ga.search.return_value = [_search_row()]
        with self.assertRaisesRegex(ToolError, "cap"):
            campaigns.update_campaign_budget(CID, "22", 101)
        with patch.dict(os.environ, {guards.MAX_BUDGET_INCREASE_PCT_ENV: "25"}):
            with self.assertRaisesRegex(ToolError, "50% increase"):
                campaigns.update_campaign_budget(CID, "22", 30)
        ga.search.return_value = []
        with self.assertRaisesRegex(ToolError, "not found"):
            campaigns.update_campaign_budget(CID, "22", 30)


class TestStatusTools(_Base):

    def test_enable_shows_budget_and_commits(self):
        self.svc("GoogleAdsService").search.return_value = [_search_row()]
        svc = self.svc("CampaignService")
        proposal = json.loads(campaigns.enable_campaign(CID, "22"))
        fields = {i["field_readable"]: i for i in proposal["change_items"]}
        self.assertEqual(fields["Status"]["value_readable"], "ENABLED")
        self.assertEqual(fields["Status"]["original_value_readable"], "PAUSED")
        self.assertIn("Daily budget (will start spending)", fields)
        request = svc.mutate_campaigns.call_args.kwargs["request"]
        self.assertEqual(
            request.operations[0].update.status,
            self.real_client.enums.CampaignStatusEnum.ENABLED,
        )
        result = campaigns.enable_campaign(
            CID, "22", validate_only=False, checksum=proposal["checksum"]
        )
        self.assertIn("enabled", result)

    def test_pause(self):
        self.svc("GoogleAdsService").search.return_value = [
            _search_row(status="ENABLED")
        ]
        proposal = json.loads(campaigns.pause_campaign(CID, "22"))
        self.assertEqual(proposal["status"], "PROPOSED")

    def test_already_in_state_or_removed(self):
        ga = self.svc("GoogleAdsService")
        ga.search.return_value = [_search_row(status="PAUSED")]
        with self.assertRaisesRegex(ToolError, "already PAUSED"):
            campaigns.pause_campaign(CID, "22")
        ga.search.return_value = [_search_row(status="REMOVED")]
        with self.assertRaisesRegex(ToolError, "removed"):
            campaigns.enable_campaign(CID, "22")

    def test_enable_proposal_cannot_be_used_to_pause(self):
        self.svc("GoogleAdsService").search.return_value = [_search_row()]
        proposal = json.loads(campaigns.enable_campaign(CID, "22"))
        self.svc("GoogleAdsService").search.return_value = [
            _search_row(status="ENABLED")
        ]
        with self.assertRaisesRegex(ToolError, "mismatched"):
            campaigns.pause_campaign(
                CID, "22", validate_only=False, checksum=proposal["checksum"]
            )


if __name__ == "__main__":
    unittest.main()
