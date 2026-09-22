import unittest
import tempfile
import os
import time
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch
from contextlib import asynccontextmanager
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import httpx
import web.server as server

@asynccontextmanager
async def noop(app):
    yield

class RelayGatewayTests(unittest.TestCase):
    def test_idle_socket_expires_without_waiting_for_logs(self):
        class IdleUpstream:
            def __aiter__(self): return self
            async def __anext__(self):
                await asyncio.Event().wait()
        @asynccontextmanager
        async def connect(*args, **kwargs):
            yield IdleUpstream()
        token = server._encode_signed_payload({'user':{'name':'test'}, 'exp':int(time.time())+2}, 'test-only')
        with TestClient(self.make_app()) as client, patch.object(server.websockets, 'connect', connect):
            client.cookies.set(server.AUTH_SESSION_COOKIE, token)
            with client.websocket_connect('/api/relay/ws/logs') as ws:
                self.assertEqual(ws.receive()['code'], 1008)

    def test_missing_credentials_fail_closed(self):
        for flag in ('true', '', 'typo'):
            with self.subTest(flag=flag), patch.dict(os.environ, {'FEISHU_LOGIN_ENABLED':flag,'FEISHU_CLIENT_ID':'','FEISHU_APP_ID':'','FEISHU_APP_SECRET':''}):
                with self.assertRaises(RuntimeError):
                    server._load_feishu_auth_config()

    def test_session_secret_persists(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'DATA_DIR', Path(directory)), patch.dict(os.environ, {'FEISHU_LOGIN_ENABLED':'true','FEISHU_CLIENT_ID':'test','FEISHU_APP_SECRET':'test','FEISHU_SESSION_SECRET':'','WEB_SESSION_SECRET':''}):
            first = server._load_feishu_auth_config()['session_secret']
            self.assertEqual(first, server._load_feishu_auth_config()['session_secret'])
            self.assertGreater(len(first), 40)
            self.assertEqual((Path(directory)/'.web-session-secret').stat().st_mode & 0o777, 0o600)

    def make_app(self):
        with patch.object(server, 'lifespan', noop), patch.object(server, '_load_feishu_auth_config', return_value={'enabled': True, 'session_secret': 'test-only'}):
            return server.create_app()

    def test_requires_login_for_page_api_and_socket(self):
        with TestClient(self.make_app()) as client:
            self.assertEqual(client.get('/relay', follow_redirects=False).status_code, 307)
            self.assertEqual(client.get('/api/relay/config').status_code, 401)
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect('/api/relay/ws/logs'):
                    pass

    def test_authenticated_proxy_and_origin_guard(self):
        upstream = AsyncMock()
        upstream.request.return_value = httpx.Response(200, json={'ok': True})
        manager = AsyncMock()
        manager.__aenter__.return_value = upstream
        with TestClient(self.make_app()) as client, patch.object(server, '_read_auth_session', return_value={'name': 'test'}), patch.object(server.httpx, 'AsyncClient', return_value=manager):
            self.assertEqual(client.get('/relay').status_code, 200)
            response = client.get('/api/relay/config')
            self.assertEqual(response.json(), {'ok': True})
            self.assertEqual(upstream.request.call_args.args[1], 'http://opus-relay:8090/api/config')
            self.assertNotIn('cookie', upstream.request.call_args.kwargs['headers'])
            self.assertEqual(client.post('/api/relay/config', json={}, headers={'origin': 'https://evil.example'}).status_code, 403)
            self.assertEqual(client.post('/api/relay/config', json={}, headers={'origin': 'https://testserver'}).status_code, 200)
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect('/api/relay/ws/logs', headers={'origin': 'https://evil.example'}):
                    pass

    def test_upstream_unavailable(self):
        upstream = AsyncMock()
        upstream.request.side_effect = httpx.ConnectError('offline')
        manager = AsyncMock()
        manager.__aenter__.return_value = upstream
        with TestClient(self.make_app()) as client, patch.object(server, '_read_auth_session', return_value={'name': 'test'}), patch.object(server.httpx, 'AsyncClient', return_value=manager):
            self.assertEqual(client.get('/api/relay/config').status_code, 502)
