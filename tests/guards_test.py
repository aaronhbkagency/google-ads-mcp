"""Tests for the server-side write guards."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastmcp.exceptions import ToolError

from ads_mcp import guards


class TestAllowlist(unittest.TestCase):

    @patch.dict(os.environ, {}, clear=True)
    def test_unset_allowlist_refuses_all_writes(self):
        with self.assertRaisesRegex(ToolError, "not set"):
            guards.require_write_allowed("1234567890")

    @patch.dict(os.environ, {guards.ALLOWLIST_ENV: " , "})
    def test_blank_allowlist_refuses_all_writes(self):
        with self.assertRaisesRegex(ToolError, "not set"):
            guards.require_write_allowed("1234567890")

    @patch.dict(os.environ, {guards.ALLOWLIST_ENV: "260-394-5345, 111"})
    def test_allowlisted_id_passes_in_any_format(self):
        self.assertEqual(
            guards.require_write_allowed("2603945345"), "2603945345"
        )
        self.assertEqual(
            guards.require_write_allowed("260-394-5345"), "2603945345"
        )
        self.assertEqual(guards.require_write_allowed(2603945345), "2603945345")

    @patch.dict(os.environ, {guards.ALLOWLIST_ENV: "2603945345"})
    def test_other_id_refused(self):
        with self.assertRaisesRegex(ToolError, "not on this server's write"):
            guards.require_write_allowed("9999999999")
        with self.assertRaisesRegex(ToolError, "must contain digits"):
            guards.require_write_allowed(None)


class TestBudgetGuards(unittest.TestCase):

    @patch.dict(os.environ, {}, clear=True)
    def test_unset_cap_refuses_budget_writes(self):
        with self.assertRaisesRegex(ToolError, "not set"):
            guards.check_daily_budget(10)

    @patch.dict(os.environ, {guards.MAX_DAILY_BUDGET_ENV: "100"})
    def test_cap(self):
        self.assertEqual(guards.check_daily_budget(100), 100_000_000)
        self.assertEqual(guards.check_daily_budget(12.345), 12_350_000)
        with self.assertRaisesRegex(ToolError, "above this server's cap"):
            guards.check_daily_budget(100.01)
        with self.assertRaisesRegex(ToolError, "greater than 0"):
            guards.check_daily_budget(0)

    @patch.dict(os.environ, {guards.MAX_DAILY_BUDGET_ENV: "lots"})
    def test_bad_cap_config(self):
        with self.assertRaisesRegex(ToolError, "misconfiguration"):
            guards.check_daily_budget(10)

    @patch.dict(os.environ, {guards.MAX_BUDGET_INCREASE_PCT_ENV: "50"})
    def test_increase_limit(self):
        guards.check_budget_increase(20_000_000, 30_000_000)  # +50% ok
        guards.check_budget_increase(20_000_000, 5_000_000)  # decrease ok
        with self.assertRaisesRegex(ToolError, "51% increase"):
            guards.check_budget_increase(20_000_000, 30_200_000)

    @patch.dict(os.environ, {}, clear=True)
    def test_no_increase_limit_when_unset(self):
        guards.check_budget_increase(1_000_000, 90_000_000)


def _context(name, arguments):
    return SimpleNamespace(
        message=SimpleNamespace(name=name, arguments=arguments),
        fastmcp_context=None,
    )


class TestWriteAllowlistMiddleware(unittest.IsolatedAsyncioTestCase):

    async def _run(self, tool, name, arguments):
        middleware = guards.WriteAllowlistMiddleware()
        call_next = AsyncMock(return_value="ok")
        with patch("ads_mcp.audit._resolve_tool", AsyncMock(return_value=tool)):
            result = await middleware.on_call_tool(
                _context(name, arguments), call_next
            )
        return result, call_next

    def _tool(self, read_only):
        return SimpleNamespace(
            annotations=SimpleNamespace(read_only_hint=read_only)
        )

    @patch.dict(os.environ, {guards.ALLOWLIST_ENV: "2603945345"})
    async def test_read_only_tool_not_checked(self):
        result, call_next = await self._run(
            self._tool(True), "search_search", {"customer_id": "999"}
        )
        self.assertEqual(result, "ok")
        call_next.assert_awaited_once()

    @patch.dict(os.environ, {guards.ALLOWLIST_ENV: "2603945345"})
    async def test_mutating_tool_allowlisted(self):
        result, _ = await self._run(
            self._tool(False),
            "labels_manage_labels",
            {"customer_id": "260-394-5345", "validate_only": True},
        )
        self.assertEqual(result, "ok")

    @patch.dict(os.environ, {guards.ALLOWLIST_ENV: "2603945345"})
    async def test_mutating_tool_refused(self):
        with self.assertRaises(ToolError):
            await self._run(
                self._tool(False),
                "assets_create_asset",
                {"customer_id": "1111111111", "validate_only": True},
            )

    @patch.dict(os.environ, {guards.ALLOWLIST_ENV: "2603945345"})
    async def test_missing_read_only_hint_is_mutating(self):
        with self.assertRaises(ToolError):
            await self._run(
                self._tool(None), "x_tool", {"customer_id": "1111111111"}
            )

    @patch.dict(os.environ, {guards.ALLOWLIST_ENV: "2603945345"})
    async def test_unresolved_tool_with_mutation_args_refused(self):
        with self.assertRaises(ToolError):
            await self._run(
                None, "mystery", {"customer_id": "1", "checksum": "abc"}
            )
        result, _ = await self._run(None, "mystery", {"customer_id": "1"})
        self.assertEqual(result, "ok")


if __name__ == "__main__":
    unittest.main()
