import json
import tempfile
from pathlib import Path
import unittest
from codex_model_router.dashboard import _route_view, DASHBOARD_HTML, build_dashboard_payload


class EvaluatorDashboardTests(unittest.TestCase):
    def test_evaluator_metadata_precedes_legacy_placeholder(self):
        view = _route_view({
            'route': {'model': 'gpt-6-astra', 'effort': 'high', 'source': 'codex-evaluator'},
            'evaluator': {'model': 'gpt-5.6-terra', 'summary': 'type=review; risk=high; confidence=0.90', 'request_ms': 0, 'thread_id': 'test-thread'},
            'notdiamond': {'proxy_model': 'none', 'request_ms': 999},
        }, {})
        self.assertEqual(view['selector_model'], 'gpt-5.6-terra')
        self.assertEqual(view['model'], 'gpt-6-astra')
        self.assertEqual(view['request_ms'], 0)
        self.assertEqual(view['reason'], 'type=review; risk=high; confidence=0.90')
        self.assertEqual(view['session_id'], 'test-thread')
        self.assertFalse(view['fallback_used'])

    def test_legacy_record_remains_readable(self):
        view = _route_view({'route': {'source': 'notdiamond'}, 'notdiamond': {
            'proxy_model': 'claude-sonnet-5', 'request_ms': 1234, 'session_id': 'old-session'}}, {})
        self.assertEqual(view['selector_model'], 'claude-sonnet-5')
        self.assertEqual(view['request_ms'], 1234)
        self.assertEqual(view['session_id'], 'old-session')

    def test_evaluator_failure_visible(self):
        view = _route_view({'route': {'source': 'codex-evaluator-fallback'},
                            'evaluator': {'error': 'timeout'}}, {})
        self.assertTrue(view['fallback_used'])
        self.assertEqual(view['fallback_error'], 'timeout')
        self.assertIn('${esc(r.reason)}', DASHBOARD_HTML)


class SourceDistributionTests(unittest.TestCase):
    def test_sources_use_full_log_despite_table_limit(self):
        records = [
            {"event": "route_decision", "route": {"source": source, "model": model}}
            for source, model in [("notdiamond", "gpt-5.6-sol"),
                                  ("notdiamond", "gpt-5.6-sol"),
                                  ("codex-evaluator", "gpt-5.6-luna"),
                                  ("codex-evaluator", "gpt-5.6-terra"),
                                  ("codex-evaluator-fallback", "gpt-5.6-terra")]
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            path.write_text("\n".join(json.dumps(r) for r in records))
            payload = build_dashboard_payload(path, limit=1)
        self.assertEqual(len(payload["routes"]), 1)
        self.assertEqual(payload["stats"]["models_by_source"], {
            "notdiamond": {"sol": 2},
            "codex-evaluator": {"luna": 1, "terra": 1},
            "codex-evaluator-fallback": {"terra": 1},
        })
        self.assertEqual(sum(sum(v.values()) for v in payload["stats"]["models_by_source"].values()), 5)
