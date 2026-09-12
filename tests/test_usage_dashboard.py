import json
import tempfile
from pathlib import Path
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from codex_model_router.dashboard import DashboardHandler, DASHBOARD_HTML, build_dashboard_payload


class UsageDashboardTests(unittest.TestCase):
    def test_dashboard_navigation_uses_relative_links_for_subpath_deployment(self):
        self.assertIn('href="./"', DASHBOARD_HTML)
        self.assertIn('href="cursor"', DASHBOARD_HTML)
        self.assertNotIn('href="/cursor"', DASHBOARD_HTML)

    def test_route_distribution_date_filter_uses_all_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'routes.jsonl'
            rows=[{'event':'route_decision','timestamp':'2026-09-08T12:00:00+08:00','codex':{'model':'gpt-5.6-sol'},'route':{'source':'codex-evaluator'}},
                  {'event':'route_decision','timestamp':'2026-09-09T12:00:00+08:00','codex':{'model':'gpt-5.6-luna'},'route':{'source':'codex-evaluator'}}]
            path.write_text('\n'.join(json.dumps(r) for r in rows))
            d=build_dashboard_payload(path,limit=1,start='2026-09-08',end='2026-09-08')
            self.assertEqual(d['stats']['filtered_models_by_source']['codex-evaluator'],{'sol':1})
            self.assertEqual(d['stats']['total'],2)
            self.assertEqual(build_dashboard_payload(path,start='2020-01-01',end='2020-01-01')['stats']['filtered_models_by_source']['codex-evaluator'],{})
            self.assertEqual(sum(build_dashboard_payload(path)['stats']['filtered_models_by_source']['codex-evaluator'].values()),2)
            with self.assertRaises(ValueError):build_dashboard_payload(path,start='bad')

    def test_usage_endpoint_and_invalid_dates(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), DashboardHandler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        url = 'http://127.0.0.1:%s/api/usage?start=2026-09-09&end=2026-09-09' % server.server_port
        try:
            with patch('codex_model_router.dashboard.build_usage_payload', return_value={'summary': {'input_tokens': 42}}) as build:
                with urllib.request.urlopen(url) as response:
                    self.assertEqual(json.load(response)['summary']['input_tokens'], 42)
                self.assertEqual(build.call_args.kwargs['start'], '2026-09-09')
            with patch('codex_model_router.dashboard.build_usage_payload', side_effect=ValueError('date')):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(url)
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join()
        self.assertIn('api/usage?', DASHBOARD_HTML)
        self.assertIn('本机 Codex · Token 用量', DASHBOARD_HTML)
