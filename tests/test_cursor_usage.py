import json
import tempfile
import unittest
from pathlib import Path

from codex_model_router.cursor_usage import build_cursor_payload


class CursorUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.routes = self.root / "decisions.jsonl"
        self.csv = self.root / "usage-events.csv"

    def test_csv_accepts_complete_non_api_rows_without_dedup(self):
        self.csv.write_text(
            "Date,Kind,Model,Input (w/ Cache Write),Input (w/o Cache Write),Cache Read,Output Tokens,Total Tokens\n"
            "2026-09-10T10:00:00Z,Included,model-a,2,3,5,7,17\n"
            "2026-09-10T10:00:00Z,On-demand,model-a,2,3,5,7,17\n"
            "2026-09-10T10:01:00Z,User API Key,model-b,4,5,6,7,22\n"
            "2026-09-10T10:02:00Z,Included,model-c,,,7,,\n"
        )
        result = build_cursor_payload(self.root / "unused.db", self.routes, csv_path=self.csv)
        self.assertEqual(result["summary"]["input_tokens"], 20)
        self.assertEqual(result["summary"]["uncached_input_tokens"], 10)
        self.assertEqual(result["coverage"]["accepted_rows"], 2)
        self.assertEqual(result["coverage"]["skipped_api_key_rows"], 1)
        self.assertEqual(result["coverage"]["invalid_rows"], 1)

    def test_no_csv_returns_unknown_usage(self):
        result = build_cursor_payload(self.root / "unused.db", self.routes, csv_path=self.root / "missing.csv")
        self.assertTrue(all(value is None for value in result["summary"].values()))
        self.assertEqual(result["by_day"], [])
        self.assertEqual(result["by_model"], [])

    def test_routes_pair_then_filter_with_proposal_timestamp(self):
        self.routes.write_text("\n".join(json.dumps(row) for row in (
            {"id": "a", "timestamp": "2026-09-09T23:59:59Z", "original_model": "old", "requested_model": "new", "actual_model": None, "reason": "policy"},
            {"id": "a", "timestamp": "2026-09-10T00:00:01Z", "actual_model": "new", "reason": "observed"},
            {"id": "b", "timestamp": "2026-09-10T01:00:00Z", "original_model": "old", "requested_model": "new", "actual_model": None, "reason": "policy"},
            {"id": "b", "timestamp": "2026-09-10T01:00:01Z", "actual_model": "other"},
        )))
        result = build_cursor_payload(self.root / "unused.db", self.routes, start="2026-09-10", end="2026-09-10", csv_path=self.root / "missing.csv")
        self.assertEqual([row["id"] for row in result["routes"]], ["b", "a"])
        self.assertEqual(result["routes"][1]["actual_model"], "new")
        self.assertEqual(result["route_summary"], {"total": 2, "verified": 2, "matched": 1, "failed": 1, "errors": 0})

    def test_route_error_is_failed_but_unobserved_evaluation_is_not(self):
        self.routes.write_text("\n".join(json.dumps(row) for row in (
            {"id": "error", "timestamp": "2026-09-10T01:00:00Z", "requested_model": "a", "actual_model": None, "reason": "error: timeout"},
            {"id": "pending", "timestamp": "2026-09-10T01:01:00Z", "requested_model": "a", "actual_model": None, "reason": "evaluated by composer"},
        )))
        result = build_cursor_payload(self.root / "unused.db", self.routes, csv_path=self.root / "missing.csv")
        self.assertEqual(result["route_summary"], {"total": 2, "verified": 0, "matched": 0, "failed": 1, "errors": 0})
