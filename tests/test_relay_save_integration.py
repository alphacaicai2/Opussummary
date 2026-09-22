"""Real gateway -> Relay validation/save -> temporary config, without a scheduler."""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from contextlib import asynccontextmanager
from fastapi.testclient import TestClient
import httpx
import web.server as server

@asynccontextmanager
async def noop(app):
    yield

class RelaySaveIntegration(unittest.TestCase):
    def test_save_reload_readback_and_invalid_payload(self):
        source = os.getenv('RELAY_TEST_SOURCE') or str(Path(__file__).resolve().parents[1] / 'relay' / 'web_server.py')
        spec = importlib.util.spec_from_file_location('relay_fixture', source)
        relay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(relay)
        original_client = httpx.AsyncClient
        def upstream_client(**kwargs):
            return original_client(transport=httpx.ASGITransport(app=relay.app))
        with tempfile.TemporaryDirectory() as directory:
            relay.CONFIG_PATH = str(Path(directory) / 'config.json')
            relay._scheduler = Mock()
            with patch.object(server, 'lifespan', noop), patch.object(server, '_load_feishu_auth_config', return_value={'enabled':True,'session_secret':'test'}):
                app = server.create_app()
            payload = {'miniflux_url':'https://example.invalid', 'miniflux_token':'test-only', 'poll_interval_minutes':7, 'routes':{'1':'https://example.invalid/webhook'}, 'categories_with_content':['1']}
            with TestClient(app) as client, patch.object(server, '_read_auth_session', return_value={'name':'test'}), patch.object(server.httpx, 'AsyncClient', upstream_client):
                response = client.post('/api/relay/config', json=payload, headers={'origin':'https://testserver'})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()['success'])
                saved = json.loads(Path(relay.CONFIG_PATH).read_text())
                self.assertEqual(saved['routes'], payload['routes'])
                self.assertEqual(saved['poll_interval_minutes'], 7)
                relay._scheduler.reload.assert_called_once_with(saved)
                self.assertEqual(client.get('/api/relay/config').json(), saved)
                self.assertEqual(client.post('/api/relay/config', json={'poll_interval_minutes':'invalid'}).status_code, 422)
                self.assertEqual(json.loads(Path(relay.CONFIG_PATH).read_text()), saved)
