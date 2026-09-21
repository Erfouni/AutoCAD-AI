#!/usr/bin/env python3
"""AutoCAD MCP server.

Exposes AutoCAD 2D drafting as MCP tools over streamable HTTP, so a cloud
assistant (ChatGPT, Manus) reached through a tunnel can draw in the AutoCAD
running on this machine.

    python server.py

Security rests on two things: the transport is mounted at an unguessable URL
path (ChatGPT's connectors cannot send custom headers, so the URL itself has to
carry the secret), and the socket only listens on 127.0.0.1 so the tunnel is the
sole way in.
"""

from __future__ import annotations

import functools
import hmac
import json
import logging
import sys
import time
from datetime import datetime, timezone

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse, Response

import config
import tools

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("acad-mcp")


# ---------------------------------------------------------------------------
# Tool-call logging
# ---------------------------------------------------------------------------

def _record(tool_name: str, arguments: dict, outcome: str, seconds: float) -> None:
    """Append one line to the audit log. Never raises."""
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": tool_name,
            "args": {k: _truncate(v) for k, v in arguments.items()},
            "outcome": outcome,
            "seconds": round(seconds, 3),
        }
        with (config.LOG_DIR / "toolcalls.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except Exception:  # noqa: BLE001 - logging must never break a drawing call
        pass


def _truncate(value, limit: int = 300):
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + f"...({len(text)} chars)"


class LoggingRegistrar:
    """Stands in for the FastMCP server while tools register themselves.

    Wraps every tool so each call is timed and written to the audit log, without
    the tool layer having to know that logging exists. functools.wraps keeps the
    signature and type hints intact, which is what FastMCP builds its JSON
    schema from.
    """

    def __init__(self, mcp: FastMCP) -> None:
        self._mcp = mcp
        self.count = 0

    def tool(self, *args, **kwargs):
        decorate = self._mcp.tool(*args, **kwargs)

        def register(fn):
            @functools.wraps(fn)
            def logged(*call_args, **call_kwargs):
                started = time.monotonic()
                try:
                    result = fn(*call_args, **call_kwargs)
                except Exception as exc:  # noqa: BLE001 - re-raised below
                    _record(
                        fn.__name__, call_kwargs,
                        f"error: {exc}", time.monotonic() - started,
                    )
                    log.warning("%s failed: %s", fn.__name__, exc)
                    raise
                _record(fn.__name__, call_kwargs, "ok", time.monotonic() - started)
                log.info("%s -> %s", fn.__name__, _truncate(result, 120))
                return result

            self.count += 1
            return decorate(logged)

        return register


# ---------------------------------------------------------------------------
# Request gate
# ---------------------------------------------------------------------------

class SecretPathAuth:
    """Rejects anything not addressed to the secret path.

    ChatGPT's custom connectors support only OAuth or no authentication -- they
    cannot attach a bearer token -- so the unguessable path is the real gate.
    Clients that *can* send an Authorization header (Manus) get it checked too,
    but its absence is not an error.
    """

    def __init__(self, app) -> None:
        self.app = app
        self.prefix = f"/{config.SECRET_PATH}"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if not (path == self.prefix or path.startswith(self.prefix + "/")):
            log.warning(
                "rejected %s %s from %s",
                scope.get("method"), path, _client(scope),
            )
            return await _plain(send, 404, b"Not Found")

        if config.AUTH_TOKEN:
            headers = dict(scope.get("headers") or [])
            presented = headers.get(b"authorization", b"").decode(errors="replace")
            expected = f"Bearer {config.AUTH_TOKEN}"
            # compare_digest rather than !=, so the time taken does not reveal
            # how much of a guessed token was right. Compared as bytes because
            # it rejects str with non-ASCII characters, which a hostile header
            # can carry.
            if presented and not hmac.compare_digest(
                presented.strip().encode(), expected.encode()
            ):
                log.warning("bad bearer token from %s", _client(scope))
                return await _plain(send, 401, b"Unauthorized")

        return await self.app(scope, receive, send)


def _client(scope) -> str:
    client = scope.get("client")
    headers = dict(scope.get("headers") or [])
    forwarded = headers.get(b"x-forwarded-for", b"").decode(errors="replace")
    direct = f"{client[0]}:{client[1]}" if client else "?"
    return f"{direct} (via {forwarded})" if forwarded else direct


async def _plain(send, status: int, body: bytes) -> None:
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

INSTRUCTIONS = """\
Controls AutoCAD 2027 running on the user's Windows machine, for 2D drafting.

Coordinates are drawing units with the origin at (0, 0); X is right, Y is up.
Angles are degrees, counter-clockwise from east. Everything is drawn in model
space at Z=0.

Work in this loop: draw, look, correct.

  1. If any tool reports AutoCAD is unavailable, call autocad_status, and
     start_autocad if it is closed. Starting it takes up to a minute.
  2. Draw. Every call crosses the internet, so put the whole drawing into one
     draw_batch call rather than sending shapes one at a time.
  3. Call capture_view and LOOK at the image. This is the only way to catch
     geometry that is the wrong size, misplaced, or overlapping -- do not
     assume a drawing is correct because the calls returned successfully.
  4. Fix what is wrong (erase_entity takes the handle a draw tool returned),
     then look again.
  5. Save with save_drawing when the user is happy, or export_pdf to hand them
     a document.

There are two ways to draw, and both return the handle of everything they
create. Keep those handles: draw_hatch, erase_entity and get_entity_info all
take one.

  draw_batch  -- the normal way. Give it the whole drawing as one list of
                 typed operations (line, polyline, rectangle, circle, arc,
                 point, text, mtext, layer). It is validated, it cannot leave
                 AutoCAD stuck, and it is one round trip instead of many.

  run_command -- anything draw_batch has no operation for: OFFSET, TRIM,
                 EXTEND, FILLET, CHAMFER, ARRAY, MIRROR, ROTATE, SCALE, BLOCK,
                 XREF, and every other AutoCAD command. Always use the
                 parenthesised form that supplies every argument at once:
                     (command "_CIRCLE" "2,2" "1.5")
                     (command "_OFFSET" "0.25" "L" "" "")
                 A bare command name leaves AutoCAD waiting at a prompt and the
                 next thing you send is eaten as the answer to it. run_command
                 tells you what was created, what it returned, and whether
                 AutoCAD is still waiting -- read that before continuing.

Put geometry on named layers with create_layer rather than leaving everything
on layer 0 -- it is what makes a drawing usable afterwards.

Prefer trying something and looking at the result over asking the user to
clarify. You can see the drawing with capture_view and undo mistakes with
erase_entity, so a wrong guess is cheap and quick to correct. Ask only when the
answer genuinely changes the design intent and you cannot infer it -- and when
you do run a command, actually run it rather than describing what it would do.

Two things worth checking before you offset, trim or copy something: call
list_entities to get the handle of the shape you actually mean (the AutoCAD
"L"ast object is often a dimension, not the geometry you just drew), and read
the units from get_drawing_info so your distances mean what you intend.\
"""


def build_app():
    config.ensure_dirs()

    mcp = FastMCP(
        name="AutoCAD",
        instructions=INSTRUCTIONS,
        host=config.HOST,
        port=config.PORT,
        streamable_http_path=config.MCP_PATH,
        # Stateless: each request stands alone, so a dropped tunnel or a
        # client that reconnects does not lose a session.
        stateless_http=True,
        json_response=True,
        # The Host header arrives as the tunnel's domain, which the rebinding
        # check would reject. The secret path is the gate instead.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )

    registrar = LoggingRegistrar(mcp)
    tools.register(registrar)

    @mcp.custom_route(f"/{config.SECRET_PATH}/health", methods=["GET"])
    async def health(_request) -> Response:
        return JSONResponse({
            "status": "ok",
            "server": "autocad-mcp",
            "tools": registrar.count,
            "mcp_endpoint": config.MCP_PATH,
        })

    return mcp, registrar, SecretPathAuth(mcp.streamable_http_app())


def main() -> int:
    mcp, registrar, app = build_app()

    print()
    print("=" * 68)
    print("  AutoCAD MCP server")
    print("=" * 68)
    print(f"  tools registered : {registrar.count}"
          f"{' (+send_command ENABLED)' if config.ENABLE_SEND_COMMAND else ''}")
    print(f"  listening on     : http://{config.HOST}:{config.PORT}")
    print(f"  MCP endpoint     : {config.local_url()}")
    print(f"  health check     : http://{config.HOST}:{config.PORT}"
          f"/{config.SECRET_PATH}/health")
    print(f"  output folder    : {config.SAVE_DIR}")
    print(f"  audit log        : {config.LOG_DIR / 'toolcalls.jsonl'}")
    print("-" * 68)
    print("  Start the tunnel in another window (start_ngrok.ps1) and give the")
    print("  AI the public URL with the same secret path on the end.")
    print("=" * 68)
    print()

    try:
        uvicorn.run(
            app,
            host=config.HOST,
            port=config.PORT,
            log_level="warning",
            access_log=False,
        )
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
