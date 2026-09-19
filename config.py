#!/usr/bin/env python3
"""Configuration for the AutoCAD MCP server.

Every setting is an environment variable with a sane default. The secret URL
path is generated once and persisted to `.secret` so that restarting the server
does not invalidate the connector URLs already pasted into ChatGPT and Manus.
"""

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
SECRET_FILE = BASE_DIR / ".secret"

HOST = os.environ.get("ACAD_MCP_HOST", "127.0.0.1")

# 8770 rather than the more obvious 8765: that one belongs to the Codex AutoCAD
# host this project is meant to run alongside, and 8766 sits inside a
# Windows-reserved range on the machine this was built on.
PORT = int(os.environ.get("ACAD_MCP_PORT") or "8770")

# Folder the server is allowed to write DWG/PDF into. The profile root is
# deliberately outside OneDrive: sync can lock a file microseconds after
# AutoCAD writes it. An empty variable counts as unset -- otherwise it would
# resolve to the current directory and the server would write into the source
# tree.
SAVE_DIR = Path(
    os.environ.get("ACAD_MCP_SAVE_DIR") or Path.home() / "AutoCAD-MCP-Out"
).resolve()

# Optional bearer token. When a client sends an Authorization header it must
# match this; clients that cannot send headers (ChatGPT) are still allowed in
# because the secret path is the real gate.
AUTH_TOKEN = os.environ.get("ACAD_MCP_AUTH_TOKEN", "").strip()

# Raw AutoCAD command / AutoLISP execution. Remote-code-execution grade.
ENABLE_SEND_COMMAND = os.environ.get("ACAD_MCP_ENABLE_SEND_COMMAND", "") == "1"

# How long a single COM operation may take before the tool gives up.
COM_TIMEOUT = float(os.environ.get("ACAD_MCP_COM_TIMEOUT", "120"))


def _load_or_create_secret() -> str:
    """Return the URL path segment used as the server's shared secret."""
    override = os.environ.get("ACAD_MCP_SECRET_PATH", "").strip().strip("/")
    if override:
        return override

    if SECRET_FILE.exists():
        stored = SECRET_FILE.read_text(encoding="utf-8").strip()
        if stored:
            return stored

    generated = "mcp-" + secrets.token_urlsafe(24)
    SECRET_FILE.write_text(generated, encoding="utf-8")
    return generated


SECRET_PATH = _load_or_create_secret()

# Full path the streamable-http transport is mounted at.
MCP_PATH = f"/{SECRET_PATH}/mcp"


def ensure_dirs() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    SAVE_DIR.mkdir(parents=True, exist_ok=True)


def local_url() -> str:
    return f"http://{HOST}:{PORT}{MCP_PATH}"
