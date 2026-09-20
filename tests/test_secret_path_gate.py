"""The request gate in server.SecretPathAuth.

The README's security model rests on one claim: the transport is mounted at an
unguessable URL path and everything not addressed to that path is refused. That
claim was asserted but never tested, so nothing would notice if the prefix check
loosened -- and a gate that quietly stops gating looks exactly like one that
works.

SecretPathAuth is plain ASGI middleware, so these drive it directly with a stub
downstream app rather than standing up uvicorn or touching AutoCAD.

    python -m unittest discover -s tests -t .
"""

import asyncio
import os
import unittest
from unittest import mock

# Must be in place before importing config: without it, the import writes a
# .secret file into the source tree.
os.environ.setdefault("ACAD_MCP_SECRET_PATH", "unit-test-secret")

import config  # noqa: E402
import server  # noqa: E402


class StubApp:
    """Downstream app that records whether the gate let a request through."""

    def __init__(self):
        self.calls = []

    async def __call__(self, scope, receive, send):
        self.calls.append(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"through"})

    @property
    def reached(self):
        return bool(self.calls)


def call(gate, path, headers=None, scope_type="http", method="POST"):
    """Drive the gate once; return (status, body) as seen by the client."""
    scope = {"type": scope_type, "path": path, "method": method,
             "client": ("127.0.0.1", 51234),
             "headers": list(headers or [])}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(gate(scope, receive, send))

    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


def build(secret="unit-test-secret"):
    """A gate wired to a fresh stub, for the given secret path."""
    downstream = StubApp()
    with mock.patch.object(config, "SECRET_PATH", secret):
        gate = server.SecretPathAuth(downstream)
    return gate, downstream


class PathGate(unittest.TestCase):
    """Only the secret path, and paths beneath it, may reach the transport."""

    def test_exact_secret_path_passes(self):
        gate, downstream = build()
        status, _ = call(gate, "/unit-test-secret")
        self.assertEqual(status, 200)
        self.assertTrue(downstream.reached)

    def test_path_below_the_secret_passes(self):
        gate, downstream = build()
        status, _ = call(gate, "/unit-test-secret/mcp")
        self.assertEqual(status, 200)
        self.assertTrue(downstream.reached)

    def test_unrelated_path_is_refused(self):
        gate, downstream = build()
        status, body = call(gate, "/mcp")
        self.assertEqual(status, 404)
        self.assertEqual(body, b"Not Found")
        self.assertFalse(downstream.reached)

    def test_root_is_refused(self):
        gate, downstream = build()
        self.assertEqual(call(gate, "/")[0], 404)
        self.assertFalse(downstream.reached)

    def test_prefix_confusion_is_refused(self):
        """`/<secret>x` must not pass as `/<secret>`.

        A bare startswith(prefix) check would let every path that merely begins
        with the secret through, which turns the secret into a guessable stem.
        """
        gate, downstream = build()
        for path in ("/unit-test-secretx",
                     "/unit-test-secret-extra",
                     "/unit-test-secret.json"):
            with self.subTest(path=path):
                self.assertEqual(call(gate, path)[0], 404)
        self.assertFalse(downstream.reached)

    def test_secret_elsewhere_in_the_path_is_refused(self):
        gate, downstream = build()
        self.assertEqual(call(gate, "/public/unit-test-secret")[0], 404)
        self.assertFalse(downstream.reached)

    def test_non_http_scope_passes_untouched(self):
        """Lifespan and websocket scopes must not be path-checked."""
        gate, downstream = build()
        call(gate, "", scope_type="lifespan")
        self.assertTrue(downstream.reached)
        self.assertEqual(downstream.calls[0]["type"], "lifespan")


class BearerToken(unittest.TestCase):
    """The optional bearer check: enforced when presented, never required."""

    def test_no_token_configured_ignores_any_header(self):
        gate, downstream = build()
        with mock.patch.object(config, "AUTH_TOKEN", ""):
            status, _ = call(gate, "/unit-test-secret",
                             headers=[(b"authorization", b"Bearer whatever")])
        self.assertEqual(status, 200)
        self.assertTrue(downstream.reached)

    def test_correct_token_passes(self):
        gate, downstream = build()
        with mock.patch.object(config, "AUTH_TOKEN", "s3cret"):
            status, _ = call(gate, "/unit-test-secret",
                             headers=[(b"authorization", b"Bearer s3cret")])
        self.assertEqual(status, 200)
        self.assertTrue(downstream.reached)

    def test_wrong_token_is_rejected(self):
        gate, downstream = build()
        with mock.patch.object(config, "AUTH_TOKEN", "s3cret"):
            status, body = call(gate, "/unit-test-secret",
                                headers=[(b"authorization", b"Bearer wrong")])
        self.assertEqual(status, 401)
        self.assertEqual(body, b"Unauthorized")
        self.assertFalse(downstream.reached)

    def test_token_without_the_bearer_scheme_is_rejected(self):
        gate, downstream = build()
        with mock.patch.object(config, "AUTH_TOKEN", "s3cret"):
            status, _ = call(gate, "/unit-test-secret",
                             headers=[(b"authorization", b"s3cret")])
        self.assertEqual(status, 401)
        self.assertFalse(downstream.reached)

    def test_missing_header_is_allowed_by_design(self):
        """ChatGPT connectors cannot send headers; the path is the real gate."""
        gate, downstream = build()
        with mock.patch.object(config, "AUTH_TOKEN", "s3cret"):
            status, _ = call(gate, "/unit-test-secret")
        self.assertEqual(status, 200)
        self.assertTrue(downstream.reached)

    def test_surrounding_whitespace_is_tolerated(self):
        gate, downstream = build()
        with mock.patch.object(config, "AUTH_TOKEN", "s3cret"):
            status, _ = call(gate, "/unit-test-secret",
                             headers=[(b"authorization", b"  Bearer s3cret  ")])
        self.assertEqual(status, 200)
        self.assertTrue(downstream.reached)

    def test_bad_token_on_a_bad_path_is_still_a_404(self):
        """The path check runs first; a wrong path must not reveal the token."""
        gate, downstream = build()
        with mock.patch.object(config, "AUTH_TOKEN", "s3cret"):
            status, _ = call(gate, "/wrong",
                             headers=[(b"authorization", b"Bearer wrong")])
        self.assertEqual(status, 404)
        self.assertFalse(downstream.reached)


if __name__ == "__main__":
    unittest.main()
