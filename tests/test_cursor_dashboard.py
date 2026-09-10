import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from codex_model_router.dashboard import DashboardHandler


class CursorDashboardTests(unittest.TestCase):
    def test_import_requires_same_origin_and_valid_csv(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, 'home', return_value=Path(directory)):
            server = ThreadingHTTPServer(('127.0.0.1', 0), DashboardHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = 'http://127.0.0.1:' + str(server.server_port)
            data = b'Date,Kind,Model,Input (w/ Cache Write),Input (w/o Cache Write),Cache Read,Output Tokens,Total Tokens\n2026-09-10T01:00:00Z,Included,composer-2.5,2,3,10,5,20\n'
            target = Path(directory) / '.cursor/model-router/usage-events.csv'
            try:
                for origin in ('https://example.com', None):
                    request = urllib.request.Request(base + '/api/cursor/import', data=data)
                    if origin:
                        request.add_header('Origin', origin)
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(request)
                    self.assertEqual(caught.exception.code, 403)
                    caught.exception.close()
                    self.assertFalse(target.exists())
                request = urllib.request.Request(base + '/api/cursor/import', data=data, headers={'Origin': base, 'Content-Type': 'text/csv'})
                with urllib.request.urlopen(request) as response:
                    self.assertTrue(json.load(response)['ok'])
                self.assertEqual(target.read_bytes(), data)
                request = urllib.request.Request(base + '/api/cursor/import', data=b'not csv', headers={'Origin': base})
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request)
                self.assertEqual(caught.exception.code, 400)
                caught.exception.close()
                self.assertEqual(target.read_bytes(), data)
                with urllib.request.urlopen(base + '/api/cursor?start=2026-09-10&end=2026-09-10') as response:
                    payload = json.load(response)
                    self.assertEqual(payload['summary']['input_tokens'], 15)
                    self.assertEqual(payload['summary']['output_tokens'], 5)
                with urllib.request.urlopen(base + '/cursor') as response:
                    self.assertIn('Cursor', response.read().decode())
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
