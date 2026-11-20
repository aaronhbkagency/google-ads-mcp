# Campaign write tools and write guards (fork additions)

This fork adds `campaigns`, `keywords` and `ads` tool namespaces and server-side write guards on top of upstream `googleads/google-ads-mcp`.

## Tools

| Tool | What it does |
|---|---|
| `campaigns_create_search_campaign` | Creates a budget, a Search campaign (**always PAUSED**), location and language targeting, campaign negatives, an ad group, keywords and an RSA in one atomic request |
| `campaigns_update_campaign_budget` | Changes a campaign's daily budget (old → new shown). Refuses shared budgets. |
| `campaigns_pause_campaign` | Pauses a campaign |
| `campaigns_enable_campaign` | Enables a campaign (goes live). It is the only tool that does this. |
| `keywords_add_keywords` | Adds keywords (optional CPC bid on Manual CPC) to an ad group. Skips duplicates; the preview says whether they will serve straight away. |
| `keywords_pause_keywords` / `keywords_enable_keywords` | Pauses or re-enables keywords by criterion ID |
| `keywords_update_keyword_bids` | Changes keyword max CPC bids (Manual CPC campaigns only), old → new |
| `keywords_add_negative_keywords` | Adds negatives to one campaign or one ad group, and warns when a negative would block an existing keyword |
| `keywords_create_negative_keyword_list` | Creates a shared negative keyword list, its keywords and campaign links in one atomic request |
| `keywords_add_to_negative_keyword_list` | Adds keywords to an existing shared list and names every campaign it affects |
| `keywords_apply_negative_keyword_list` | Applies a shared list to more campaigns |
| `ads_create_responsive_search_ad` | New RSA in an existing ad group, with optional pins and paths. Created **PAUSED** unless `start_enabled=True`. |
| `ads_update_responsive_search_ad` | Edits an RSA's headlines, descriptions, URL or paths in place (old → new) |
| `ads_pause_ad` / `ads_enable_ad` | Pauses or enables an ad |

All of them use the upstream Propose & Confirm workflow. `validate_only=True` (the default) runs Google's dry run and returns `change_items` plus a `checksum`. A commit needs `validate_only=False`, the same parameters and that checksum. The budget and status tools put the current value into the checksum, so if something changes in Google Ads between the proposal and the commit, the commit is refused.

Additional rules:
- Campaigns are created PAUSED, with Display Network off and search partners off unless requested.
- At least one location is required, so a campaign can never serve worldwide.
- BROAD match keywords are refused unless bidding is `MAXIMIZE_CONVERSIONS`.
- No tool in these namespaces removes anything; keywords and ads can only be paused.
- Keyword CPC bids only apply to Manual CPC campaigns and are capped (see `GOOGLE_ADS_MCP_MAX_CPC`).
- New keywords and enabled ads in a live campaign start serving once approved; the preview states this.
- Every update reads the current state from Google Ads into the checksum, so a change made elsewhere after the preview makes the commit fail.
- The EU political advertising declaration is required. Claude is told to ask the user for it.

## Environment variables

| Variable | Required | Meaning |
|---|---|---|
| `GOOGLE_ADS_MCP_WRITE_ALLOWLIST` | **Yes, for any write** | Comma-separated customer IDs that **any** mutate tool may change, including the upstream asset and label tools. Unset or empty means every write is refused. |
| `GOOGLE_ADS_MCP_MAX_DAILY_BUDGET` | **Yes, for budget writes** | Highest daily budget a tool may set, in the account currency. Unset means budget writes are refused. |
| `GOOGLE_ADS_MCP_MAX_BUDGET_INCREASE_PCT` | No | Largest % increase to an existing budget in one change |
| `GOOGLE_ADS_MCP_MAX_CPC` | No | Highest keyword max CPC bid. If unset, `GOOGLE_ADS_MCP_MAX_DAILY_BUDGET` is the ceiling; with neither set, bid changes are refused. |

The allowlist is enforced twice:
1. By `WriteAllowlistMiddleware` on every non-read-only tool call. It runs after `AuditMiddleware`, so refused calls are still audited.
2. Inside each campaigns tool.

To turn off going live entirely, disable the enable tool in `tools_config.yaml`:

```yaml
  campaigns:
    enabled: true
    enabled_tools:
      - enable_campaign: false
```

## Running the tests

```shell
.venv/bin/python -m unittest discover -s tests -p "*_test.py" -t .
```

## Keeping up with upstream

```shell
git fetch upstream
git merge upstream/main
```

Fork changes to upstream files are kept small:
- `coordinator.py`: one middleware line
- `config.py` and `tools_config.yaml`: the `campaigns`, `keywords` and `ads` namespaces
- `tests/audit_test.py`: allowlist env in one test
