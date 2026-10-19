import json
import unittest
from pathlib import Path

from fastapi.responses import JSONResponse

from app import main, registry


class FakeRequest:
    def __init__(self, headers: dict[str, str], method: str = "GET"):
        self.headers = headers
        self.method = method


async def _next_response(request):
    return JSONResponse({"reached": True}, status_code=418)


class LocalApiGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_loopback_host_is_rejected(self):
        response = await main._local_api_guard(
            FakeRequest({"host": "evil.example"}), _next_response)
        self.assertEqual(response.status_code, 403)

    async def test_cross_origin_state_request_is_rejected(self):
        response = await main._local_api_guard(
            FakeRequest({
                "host": "127.0.0.1:8600",
                "origin": "https://evil.example",
                "x-aac-request": "1",
            }, method="POST"),
            _next_response,
        )
        self.assertEqual(response.status_code, 403)

    async def test_state_request_requires_marker_header(self):
        response = await main._local_api_guard(
            FakeRequest({"host": "127.0.0.1:8600"}, method="POST"),
            _next_response,
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("标记", json.loads(response.body)["detail"])

    async def test_same_origin_marked_request_reaches_route(self):
        response = await main._local_api_guard(
            FakeRequest({
                "host": "127.0.0.1:8600",
                "origin": "http://127.0.0.1:8600",
                "x-aac-request": "1",
            }, method="POST"),
            _next_response,
        )
        self.assertEqual(response.status_code, 418)


class FrontendSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (Path(__file__).resolve().parents[1] / "app" / "static" / "index.html").read_text(
            encoding="utf-8")

    def test_all_api_calls_use_fetch_wrapper(self):
        self.assertIn("function apiFetch(", self.html)
        self.assertEqual(self.html.count("fetch("), 1)

    def test_strings_are_escaped_for_html_and_inline_javascript(self):
        self.assertIn(".replace(/'/g,'&#39;')", self.html)
        self.assertIn("function jsArg(s){ return esc(JSON.stringify(String(s))); }", self.html)

    def test_sensitive_operation_errors_are_rendered_as_text(self):
        self.assertIn("t.textContent = String(msg);", self.html)


class RegistryPackagingTests(unittest.TestCase):
    def test_registry_file_lives_with_the_package(self):
        self.assertEqual(registry.REGISTRY_PATH, Path(registry.__file__).resolve().with_name("agents.yaml"))
        self.assertTrue(registry.REGISTRY_PATH.is_file())
        self.assertTrue(registry.load_agents())


if __name__ == "__main__":
    unittest.main()
