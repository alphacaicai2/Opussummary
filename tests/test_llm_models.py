import unittest
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

import web.server as server


@asynccontextmanager
async def noop_lifespan(app):
    yield


class LLMModelDiscoveryTests(unittest.TestCase):
    def test_fetches_and_deduplicates_openai_compatible_models(self):
        response = httpx.Response(
            200,
            json={"data": [{"id": "model-b"}, {"id": "model-a"}, {"id": "model-b"}]},
        )
        payload = server.LLMModelsRequest(
            provider="siliconflow",
            base_url="https://api.example.test/v1/",
            api_key="secret-value",
        )

        with patch.object(server.httpx, "get", return_value=response) as request:
            result = server.fetch_llm_models(payload)

        self.assertEqual(result, {"models": ["model-b", "model-a"], "count": 2})
        self.assertEqual(request.call_args.args[0], "https://api.example.test/v1/models")
        self.assertEqual(request.call_args.kwargs["headers"], {"Authorization": "Bearer secret-value"})

    def test_uses_anthropic_headers(self):
        response = httpx.Response(200, json={"data": [{"id": "claude-test"}]})
        payload = server.LLMModelsRequest(
            provider="anthropic",
            base_url="https://api.anthropic.test/v1",
            api_key="secret-value",
        )

        with patch.object(server.httpx, "get", return_value=response) as request:
            server.fetch_llm_models(payload)

        self.assertEqual(
            request.call_args.kwargs["headers"],
            {"x-api-key": "secret-value", "anthropic-version": "2023-06-01"},
        )

    def test_upstream_error_does_not_expose_api_key(self):
        response = httpx.Response(401, json={"message": "invalid key: never-return-this-key"})
        payload = server.LLMModelsRequest(
            base_url="https://api.example.test/v1",
            api_key="never-return-this-key",
        )

        with patch.object(server.httpx, "get", return_value=response):
            with self.assertRaises(HTTPException) as raised:
                server.fetch_llm_models(payload)

        self.assertEqual(raised.exception.status_code, 502)
        self.assertNotIn("never-return-this-key", str(raised.exception.detail))
        self.assertIn("HTTP 401", str(raised.exception.detail))

    def test_edit_keeps_stored_api_key_when_field_is_blank(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            with (
                patch.object(server, "DATA_DIR", data_dir),
                patch.object(server, "DB_PATH", data_dir / "briefings.db"),
                patch.object(server, "ENV_PATH", data_dir / ".env"),
                patch.object(server, "_BOOTSTRAPPED", False),
                patch.object(server, "lifespan", noop_lifespan),
                patch.object(server, "_load_feishu_auth_config", return_value={"enabled": False}),
            ):
                app = server.create_app()
                now = server._utcnow_iso()
                with server._get_conn() as conn:
                    cursor = conn.execute(
                        """
                        INSERT INTO llm_configs
                            (name, provider, base_url, api_key, model, is_default, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        ("test", "custom", "https://api.example.test/v1", "stored-secret", "old-model", 1, now, now),
                    )
                    config_id = int(cursor.lastrowid)
                    conn.commit()

                with TestClient(app) as client:
                    response = client.put(
                        f"/api/llm-configs/{config_id}",
                        json={
                            "name": "test",
                            "provider": "custom",
                            "base_url": "https://api.example.test/v1",
                            "model": "new-model",
                            "is_default": True,
                        },
                    )

                self.assertEqual(response.status_code, 200)
                with server._get_conn() as conn:
                    row = conn.execute("SELECT api_key, model FROM llm_configs WHERE id = ?", (config_id,)).fetchone()
                self.assertEqual(row["api_key"], "stored-secret")
                self.assertEqual(row["model"], "new-model")

                with patch.object(server.httpx, 'get') as outbound:
                    with self.assertRaises(HTTPException) as error:
                        server.fetch_llm_models(server.LLMModelsRequest(llm_config_id=config_id, base_url='https://different.example/v1'))
                    self.assertEqual(error.exception.status_code, 400)
                    outbound.assert_not_called()
                with TestClient(app) as client:
                    response = client.put(f'/api/llm-configs/{config_id}', json={'name':'test','provider':'custom','base_url':'https://different.example/v1','model':'new-model'})
                    self.assertEqual(response.status_code, 400)

    def test_models_success_does_not_hide_generation_failure(self):
        with patch.object(server.httpx, 'get', return_value=httpx.Response(200,json={'data':[]})), patch.object(server.httpx, 'post', return_value=httpx.Response(402,json={'message':'secret'})) as chat:
            result = server.test_llm_connection(server.LLMTestRequest(base_url='https://example.test/v1', api_key='secret',model='test'))
        self.assertFalse(result['success'])
        self.assertTrue(result['models_available'])
        self.assertNotIn('secret',result['detail'])
        chat.assert_called_once()


if __name__ == "__main__":
    unittest.main()
