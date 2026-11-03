"""Run the installed Twinverse HTTP/MQTT path without Kafka event ingestion."""
from __future__ import annotations

import asyncio
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT.parent / "twinverse" / ".env")

from twinverse.mcp_server.config import global_context
from twinverse_anchor_patch import apply_anchor_bookkeeping_patch


if __name__ == "__main__":
    apply_anchor_bookkeeping_patch()
    print("Twinverse MCP: http://127.0.0.1:8000/mcp -- Kafka event ingestion excluded", flush=True)
    asyncio.run(global_context.mcp_server.run_http_async(
        transport="streamable-http", host="127.0.0.1", port=8000, path="/mcp",
    ))
