import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from codex_model_router.dashboard import DashboardHandler, DASHBOARD_HTML


class UsageDashboardTests(unittest.TestCase):
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
