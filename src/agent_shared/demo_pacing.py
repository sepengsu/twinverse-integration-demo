"""Optional presentation pacing; normal agent execution has no added wait."""
from __future__ import annotations

import asyncio
import math
import os
import time


async def finish_demo_stage(started_at: float) -> None:
    seconds = float(os.environ.get("A2A_DEMO_STAGE_SECONDS", "0"))
    if not math.isfinite(seconds) or not 0 <= seconds <= 30:
        raise ValueError("A2A_DEMO_STAGE_SECONDS must be between 0 and 30.")
    remaining = seconds - (time.monotonic() - started_at)
    if remaining > 0:
        await asyncio.sleep(remaining)
