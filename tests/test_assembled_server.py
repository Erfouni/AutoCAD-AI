"""The assembled server, end to end: FastMCP's transport behind the gate.

test_secret_path_gate drives SecretPathAuth against a stub. Nothing checked
that the real app still answers MCP once the pieces are put together, so an
MCP SDK upgrade that changed how the streamable-HTTP transport responds -- or
started rejecting the tunnel's Host header -- would only show up as a broken
connector in ChatGPT. These build the app exactly as main() does and talk to
it through Starlette's TestClient: no uvicorn, no tunnel, and no AutoCAD,
because initialising and listing tools never touches COM.

    python -m unittest discover -s tests -t .
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Must be in place before importing config: without it, the import writes a
# .secret file into the source tree.
os.environ.setdefault("ACAD_MCP_SECRET_PATH", "unit-test-secret")

from starlette.testclient import TestClient  # noqa: E402

import config  # noqa: E402
import server  # noqa: E402

SECRET = "unit-test-secret"
MCP = f"/{SECRET}/mcp"

# What a request looks like once ngrok has forwarded it: the Host header is the
# tunnel's public name, not 127.0.0.1.
TUNNEL = {"Host": "example.ngrok-free.app"}
MCP_HEADERS = {
    **TUNNEL,
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def rpc(method, params=None, id_=1):
    message = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        message["params"] = params
    return message


INITIALIZE = rpc("initialize", {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "unit-test", "version": "0"},
})


class AssembledServer(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        # build_app() creates the output and log folders; keep them out of the
        # user's profile and the source tree.
        cls._patches = [
            mock.patch.object(config, "SECRET_PATH", SECRET),
            mock.patch.object(config, "MCP_PATH", MCP),
            mock.patch.object(config, "AUTH_TOKEN", ""),
            mock.patch.object(config, "SAVE_DIR", tmp / "out"),
            mock.patch.object(config, "LOG_DIR", tmp / "logs"),
        ]
        for patch in cls._patches:
            patch.start()
        _mcp, cls.registrar, app = server.build_app()
        # Entering the client runs the app's lifespan, which starts the
        # transport's session manager -- what uvicorn does in main().
        cls.client = TestClient(app, base_url="http://127.0.0.1:8770")
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        for patch in reversed(cls._patches):
            patch.stop()
        cls._tmp.cleanup()

    def post(self, message, headers=MCP_HEADERS):
        return self.client.post(MCP, json=message, headers=headers)

    def test_initialize_through_the_tunnel_host(self):
        response = self.post(INITIALIZE)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(
            response.headers["content-type"].startswith("application/json"),
            response.headers["content-type"],
        )
        result = response.json()["result"]
        self.assertEqual(result["serverInfo"]["name"], "AutoCAD")
        self.assertIn("draw_batch", result["instructions"])

    def test_tools_list_needs_no_session(self):
        """Stateless: a client that reconnects need not initialise again."""
        response = self.post(rpc("tools/list", id_=2))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("mcp-session-id", response.headers)
        names = [tool["name"] for tool in response.json()["result"]["tools"]]
        self.assertEqual(len(names), self.registrar.count)
        self.assertIn("draw_batch", names)
        self.assertIn("capture_view", names)

    def test_health_route_sits_behind_the_secret_path(self):
        response = self.client.get(f"/{SECRET}/health", headers=TUNNEL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["tools"], self.registrar.count)
        self.assertEqual(response.json()["mcp_endpoint"], MCP)

    def test_transport_is_not_reachable_without_the_secret(self):
        for path in ("/mcp", "/", "/health"):
            with self.subTest(path=path):
                response = self.client.post(path, json=INITIALIZE, headers=MCP_HEADERS)
                self.assertEqual(response.status_code, 404)

    def test_bearer_token_is_checked_in_front_of_the_transport(self):
        with mock.patch.object(config, "AUTH_TOKEN", "s3cret"):
            refused = self.post(
                INITIALIZE, {**MCP_HEADERS, "Authorization": "Bearer wrong"})
            accepted = self.post(
                INITIALIZE, {**MCP_HEADERS, "Authorization": "Bearer s3cret"})
        self.assertEqual(refused.status_code, 401)
        self.assertEqual(accepted.status_code, 200, accepted.text)


if __name__ == "__main__":
    unittest.main()
