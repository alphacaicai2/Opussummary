from __future__ import annotations

import os
import unittest
from contextlib import asynccontextmanager
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, quote, urlparse

from fastapi.testclient import TestClient

import web.server as server


ENV_KEYS = [
    "FEISHU_LOGIN_ENABLED",
    "FEISHU_CLIENT_ID",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_SESSION_SECRET",
    "FEISHU_ALLOWED_IDENTIFIERS",
    "FEISHU_ALLOWED_EMAIL_DOMAINS",
]


@asynccontextmanager
async def _noop_lifespan(app):
    yield


def _create_test_app():
    with patch.object(server, "lifespan", _noop_lifespan):
        return server.create_app()


class FeishuAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._env_backup = {key: os.environ.get(key) for key in ENV_KEYS}
        os.environ["FEISHU_LOGIN_ENABLED"] = "true"
        os.environ["FEISHU_CLIENT_ID"] = "cli_test_app"
        os.environ.pop("FEISHU_APP_ID", None)
        os.environ["FEISHU_APP_SECRET"] = "test_secret"
        os.environ["FEISHU_SESSION_SECRET"] = "unit-test-session-secret"
        os.environ["FEISHU_ALLOWED_IDENTIFIERS"] = ""
        os.environ["FEISHU_ALLOWED_EMAIL_DOMAINS"] = "vibexcap.com"

    @classmethod
    def tearDownClass(cls):
        for key, value in cls._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_api_requires_auth_when_enabled(self):
        app = _create_test_app()
        with TestClient(app) as client:
            resp = client.get("/api/settings", follow_redirects=False)
            self.assertEqual(resp.status_code, 401)
            payload = resp.json()
            self.assertEqual(payload.get("login_url"), "/login")

            status = client.get("/api/auth/status")
            self.assertEqual(status.status_code, 200)
            status_payload = status.json()
            self.assertTrue(status_payload["enabled"])
            self.assertFalse(status_payload["authenticated"])
            self.assertEqual(status_payload.get("login_url"), "/login")

            page_resp = client.get("/login?next=%2F", follow_redirects=False)
            self.assertEqual(page_resp.status_code, 200)
            self.assertIn("飞书扫码登录", page_resp.text)
            self.assertIn("/auth/feishu/login?next=%2F", page_resp.text)

    @patch("web.server.httpx.get")
    @patch("web.server.httpx.post")
    def test_oauth_callback_sets_cookie_and_allows_api(self, mock_post: Mock, mock_get: Mock):
        token_resp = Mock()
        token_resp.status_code = 200
        token_resp.content = b"ok"
        token_resp.json.return_value = {
            "code": 0,
            "data": {"access_token": "u-access-token"},
        }
        mock_post.return_value = token_resp

        userinfo_resp = Mock()
        userinfo_resp.status_code = 200
        userinfo_resp.content = b"ok"
        userinfo_resp.json.return_value = {
            "code": 0,
            "data": {
                "open_id": "ou_test",
                "user_id": "u_test",
                "name": "Tester",
                "email": "tester@vibexcap.com",
            },
        }
        mock_get.return_value = userinfo_resp

        app = _create_test_app()
        with TestClient(app) as client:
            login_resp = client.get("/auth/feishu/login?next=%2Fapi%2Fsettings", follow_redirects=False)
            self.assertEqual(login_resp.status_code, 302)
            location = login_resp.headers.get("location", "")
            self.assertIn("accounts.feishu.cn/open-apis/authen/v1/authorize", location)
            params = parse_qs(urlparse(location).query)
            self.assertEqual(params.get("client_id"), ["cli_test_app"])
            self.assertNotIn("app_id", params)
            state = params["state"][0]

            callback_resp = client.get(
                f"/auth/feishu/callback?code=test-code&state={quote(state, safe='')}",
                follow_redirects=False,
            )
            self.assertEqual(callback_resp.status_code, 302)
            self.assertEqual(callback_resp.headers.get("location"), "/api/settings")
            self.assertIn("opus_session=", callback_resp.headers.get("set-cookie", ""))

            protected_resp = client.get("/api/settings")
            self.assertEqual(protected_resp.status_code, 200)
            self.assertIn("providers", protected_resp.json())

            status_resp = client.get("/api/auth/status")
            self.assertEqual(status_resp.status_code, 200)
            status_payload = status_resp.json()
            self.assertTrue(status_payload["authenticated"])
            self.assertEqual(status_payload["user"]["email"], "tester@vibexcap.com")


if __name__ == "__main__":
    unittest.main()
