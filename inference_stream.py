"""Loading-friendly, bounded and cancellable HTTP inference transport.

Socket activity is not model progress: empty keep-alives must not keep a stalled
request alive forever. These deadlines cover response headers as well as tokens.
"""
import asyncio
from dataclasses import dataclass
import math
import queue
import threading
import time

import httpx

FIRST_RESPONSE_TIMEOUT_SECONDS = 3600
GENERATION_IDLE_TIMEOUT_SECONDS = 180
TOTAL_REQUEST_TIMEOUT_SECONDS = 5400
MAX_PACKET_BYTES = 65536


@dataclass(frozen=True)
class InferenceTimeouts:
    first_response: float = FIRST_RESPONSE_TIMEOUT_SECONDS
    generation_idle: float = GENERATION_IDLE_TIMEOUT_SECONDS
    total: float = TOTAL_REQUEST_TIMEOUT_SECONDS
    connect: float = 2
    write: float = 15

    def __post_init__(self):
        for name in ('first_response', 'generation_idle', 'total', 'connect', 'write'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 7200:
                raise ValueError('Invalid inference timeout: ' + name)

    def http_timeout(self):
        # The watchdog below owns read deadlines. A per-read timeout would kill
        # large cold models early or allow empty heartbeats to extend the wait.
        return httpx.Timeout(None, connect=self.connect, write=self.write, pool=2)


class InferenceTimeoutError(ValueError):
    def __init__(self, stage, seconds, provider):
        self.stage = stage
        duration = f'{seconds / 60:g} minutes' if seconds >= 60 else f'{seconds:g} seconds'
        description = {
            'first_response': f'took too long to produce its first response ({duration}). The model may still be loading or processing context.',
            'generation_idle': f'took too long to continue generating ({duration} without output or tool-call progress).',
            'total': f'took too long and reached the request limit ({duration}).',
        }[stage]
        super().__init__(f'{provider} {description} Partial output is retained. Check engine status and resume the run; '
                         'if memory is full, select a smaller context or model.')


def meaningful_packet(packet):
    message = packet.get('message') or {}
    if message.get('content') or message.get('thinking'):
        return True
    return any((call.get('function') or {}).get('name') or (call.get('function') or {}).get('arguments')
               for call in message.get('tool_calls') or [])


async def bounded_lines(response, maximum=MAX_PACKET_BYTES):
    """Read NDJSON/SSE lines without accumulating an unbounded upstream line."""
    pending = bytearray()
    async for chunk in response.aiter_bytes():
        offset = 0
        while offset < len(chunk):
            lf, cr = chunk.find(b'\n', offset), chunk.find(b'\r', offset)
            newline = cr if lf < 0 else lf if cr < 0 else min(lf, cr)
            end = len(chunk) if newline < 0 else newline
            if len(pending) + end - offset > maximum:
                raise ValueError('The engine returned an oversized response packet.')
            pending.extend(chunk[offset:end])
            if newline < 0:
                break
            try:
                yield pending.decode('utf-8')
            except UnicodeDecodeError:
                raise ValueError('The engine returned an invalid UTF-8 response packet.') from None
            pending.clear()
            offset = newline + 1
    if pending:
        try:
            yield pending.decode('utf-8')
        except UnicodeDecodeError:
            raise ValueError('The engine returned an invalid UTF-8 response packet.') from None


async def bounded_error_response(response):
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > MAX_PACKET_BYTES:
            raise ValueError(f'The engine returned HTTP {response.status_code} with an oversized error response.')
        body.extend(chunk)
    return httpx.Response(response.status_code, headers=response.headers, content=bytes(body), request=response.request)


def cancellable_inference(url, payload, cancel_event, consume, *, headers=None, timeouts=None,
                          provider='Local engine', response_error=None):
    """Adapt an async HTTP consumer to synchronous callers with prompt Stop.

    ``consume(response, publish)`` validates protocol packets and publishes only
    normalized dictionaries. Closing this generator interrupts an outstanding
    connect/read, including before headers or the first generated token.
    """
    cancel_event = cancel_event if cancel_event is not None else threading.Event()
    if cancel_event.is_set():
        return
    timeouts = timeouts or InferenceTimeouts()
    packets = queue.Queue(maxsize=32)
    stopped, finished = threading.Event(), threading.Event()
    failures = []

    async def run():
        started = time.monotonic()
        progress = None

        async def publish(packet):
            nonlocal progress
            if meaningful_packet(packet):
                progress = time.monotonic()
            while not stopped.is_set() and not cancel_event.is_set():
                try:
                    packets.put_nowait(packet)
                    return
                except queue.Full:
                    await asyncio.sleep(.02)

        async def receive():
            async with httpx.AsyncClient(timeout=timeouts.http_timeout(), trust_env=False) as client:
                async with client.stream('POST', url, json=payload, headers=headers) as response:
                    if response.is_error:
                        error_response = await bounded_error_response(response)
                        if response_error:
                            response_error(error_response)
                        else:
                            error_response.raise_for_status()
                    await consume(response, publish)

        async def watch():
            while not stopped.is_set() and not cancel_event.is_set():
                now = time.monotonic()
                deadlines = [('total', started + timeouts.total, timeouts.total)]
                deadlines.append(('first_response', started + timeouts.first_response, timeouts.first_response)
                                 if progress is None else ('generation_idle', progress + timeouts.generation_idle, timeouts.generation_idle))
                stage, deadline, seconds = min(deadlines, key=lambda item: item[1])
                if now >= deadline:
                    raise InferenceTimeoutError(stage, seconds, provider)
                await asyncio.sleep(min(.05, max(.001, deadline - now)))

        request_task = asyncio.create_task(receive())
        watcher_task = asyncio.create_task(watch())
        try:
            done, _ = await asyncio.wait((request_task, watcher_task), return_when=asyncio.FIRST_COMPLETED)
            # Stop wins over a simultaneous timeout/network error.
            if not (stopped.is_set() or cancel_event.is_set()):
                if request_task in done:
                    await request_task
                else:
                    await watcher_task
        finally:
            request_task.cancel()
            watcher_task.cancel()
            await asyncio.gather(request_task, watcher_task, return_exceptions=True)

    def worker():
        try:
            asyncio.run(run())
        except Exception as exc:
            failures.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=worker, name='Forge-inference-stream', daemon=True)
    thread.start()
    try:
        while not cancel_event.is_set():
            try:
                yield packets.get(timeout=.05)
            except queue.Empty:
                if finished.is_set():
                    if failures:
                        raise failures[0]
                    return
    finally:
        stopped.set()
        thread.join(timeout=1)
