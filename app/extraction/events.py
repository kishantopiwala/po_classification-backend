"""SSE encoding and replay. Job mutation and streaming share one event loop."""
import asyncio
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    id: int
    name: str
    data: dict

    def encode(self):
        return f"id: {self.id}\nevent: {self.name}\ndata: {json.dumps(self.data, ensure_ascii=False)}\n\n"


async def stream_events(job, after: int, heartbeat: float):
    # The first bytes establish the stream even if there are no new events yet.
    yield ": connected\n\n"
    while True:
        job.changed.clear()
        for event in job.events:
            if event.id > after:
                after = event.id
                yield event.encode()
        if job.status in {"completed", "failed"}:
            return
        try:
            await asyncio.wait_for(job.changed.wait(), timeout=heartbeat)
        except TimeoutError:
            yield f'event: heartbeat\ndata: {json.dumps({"status": job.status})}\n\n'
