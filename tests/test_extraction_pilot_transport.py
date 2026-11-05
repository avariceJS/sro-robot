"""Synthetic transport checks; no document text or external network is used."""
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from scripts.dev.extraction_pilot.harness import http


class LocalHandler(BaseHTTPRequestHandler):
    redirect = False

    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        if self.redirect:
            self.send_response(302)
            self.send_header('Location', 'https://example.invalid/blocked')
            self.end_headers()
        else:
            body = json.dumps({'local': True}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *args):
        pass


class PilotTransportTests(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(('127.0.0.1', 0), LocalHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_environment_proxy_is_ignored_for_loopback(self):
        LocalHandler.redirect = False
        with patch.dict(os.environ, {'http_proxy': 'http://127.0.0.1:1', 'no_proxy': ''}):
            self.assertEqual(http(self.endpoint, '/synthetic', {'sample': 'synthetic'}), {'local': True})

    def test_redirect_is_blocked_before_external_request(self):
        LocalHandler.redirect = True
        with self.assertRaisesRegex(RuntimeError, 'Local inference redirect blocked'):
            http(self.endpoint, '/synthetic', {'sample': 'synthetic'})

    def test_non_loopback_endpoint_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Loopback-only'):
            http('http://example.invalid:1234', '/synthetic', {'sample': 'synthetic'})


if __name__ == '__main__':
    unittest.main()
