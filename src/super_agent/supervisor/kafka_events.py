"""선택적으로 작업 이벤트를 Kafka에 복사합니다.

기관별 위임은 계속 A2A HTTP/JSON으로 수행합니다. 대기열은 메모리에 있으며
발행 수는 대기열에 넣을 때가 아니라 Kafka 브로커가 승인할 때 증가합니다.
"""
from __future__ import annotations

import asyncio
from collections import deque
import copy
import json
from typing import Any
import uuid

from agent_shared.config import KafkaEventsConfig


class KafkaEventBridge:
    def __init__(self, config: KafkaEventsConfig):
        self.config = config
        self.session_id = uuid.uuid4().hex
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(config.queue_capacity)
        self.pending: dict[str, Any] | None = None
        self.records: deque[dict[str, Any]] = deque(maxlen=30)
        self._seen: set[str] = set()
        self._seen_order: deque[str] = deque()
        self.runner: asyncio.Task | None = None
        self.connected = False
        self.closing = False
        self.published = self.consumed = self.dropped = self.errors = 0
        self.last_error: str | None = None
        self.last_ack: dict[str, Any] | None = None

    def submit(self, event: dict[str, Any]) -> None:
        # 기록 복사에 실패해도 원래 A2A 작업 실행은 막지 않습니다.
        if not self.config.enabled:
            return
        envelope = {"schema_version":"1.0", "producer_session":self.session_id,
                    "event_id":f"{self.session_id}:{event['id']}", "event":event}
        try:
            encoded = json.dumps(envelope, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(encoded) > self.config.max_record_bytes:
                raise ValueError("event_too_large")
            if self.closing:
                raise ValueError("bridge_closed")
            self.queue.put_nowait({"envelope":envelope, "encoded":encoded})
        except (asyncio.QueueFull, ValueError, TypeError) as exc:
            self.dropped += 1
            self.last_error = f"Event was not queued: {type(exc).__name__}"

    async def start(self) -> None:
        if self.config.enabled and self.runner is None:
            self.runner = asyncio.create_task(self._run(), name="kafka-event-mirror")

    async def close(self) -> None:
        self.closing = True
        if self.runner is not None:
            try:
                await asyncio.wait_for(self.queue.join(), timeout=3)
            except asyncio.TimeoutError:
                pass
            self.runner.cancel()
            await asyncio.gather(self.runner, return_exceptions=True)
        self.connected = False

    def snapshot(self, *, include_records: bool = False) -> dict[str, Any]:
        result = {
            "enabled":self.config.enabled, "connected":self.connected,
            "mode":"event_mirror", "agent_transport":"A2A HTTP/JSON",
            "status":"disabled" if not self.config.enabled else "connected" if self.connected else "reconnecting" if self.errors else "connecting",
            "bootstrap_servers":self.config.bootstrap_servers, "topic":self.config.topic,
            "producer_session":self.session_id, "published":self.published, "consumed":self.consumed,
            "pending":self.queue.qsize() + int(self.pending is not None),
            "dropped":self.dropped, "errors":self.errors, "last_error":self.last_error,
            "last_ack":copy.deepcopy(self.last_ack), "outbox_storage":"bounded_in_memory",
        }
        if include_records:
            result["records"] = copy.deepcopy(list(self.records))
        return result

    async def _run(self) -> None:
        while True:
            producer = consumer = None
            workers = []
            try:
                from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
                producer = AIOKafkaProducer(bootstrap_servers=self.config.bootstrap_servers,
                    enable_idempotence=True, acks="all", request_timeout_ms=5000,
                    client_id=f"a2a-events-{self.session_id[:8]}")
                consumer = AIOKafkaConsumer(self.config.topic, bootstrap_servers=self.config.bootstrap_servers,
                    group_id=None, enable_auto_commit=False, auto_offset_reset="earliest",
                    request_timeout_ms=5000, client_id=f"a2a-monitor-{self.session_id[:8]}")
                await asyncio.wait_for(producer.start(), timeout=8)
                await asyncio.wait_for(consumer.start(), timeout=8)
                self.connected = True
                workers = [asyncio.create_task(self._send(producer)), asyncio.create_task(self._receive(consumer))]
                done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_EXCEPTION)
                for task in done:
                    task.result()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors += 1
                self.last_error = f"{type(exc).__name__}: Kafka connection/delivery failed; retrying"
            finally:
                self.connected = False
                for worker in workers:
                    worker.cancel()
                if workers:
                    await asyncio.gather(*workers, return_exceptions=True)
                for client in (consumer, producer):
                    if client is not None:
                        try:
                            await asyncio.wait_for(client.stop(), timeout=3)
                        except (Exception, asyncio.CancelledError):
                            pass
            await asyncio.sleep(2)

    async def _send(self, producer) -> None:
        # 브로커의 send_and_wait 확인 후에만 발행 완료로 셉니다.
        while True:
            if self.pending is None:
                self.pending = await self.queue.get()
            envelope = self.pending["envelope"]
            ack = await producer.send_and_wait(self.config.topic, value=self.pending["encoded"],
                key=envelope["event"]["global_task_id"].encode("utf-8"))
            self.last_ack = {"event_id":envelope["event_id"], "topic":ack.topic,
                             "partition":ack.partition, "offset":ack.offset}
            self.published += 1
            self.pending = None
            self.queue.task_done()

    async def _receive(self, consumer) -> None:
        async for message in consumer:
            try:
                envelope = json.loads(message.value)
                if envelope.get("producer_session") != self.session_id:
                    continue
                event_id = envelope["event_id"]
                if not isinstance(event_id, str) or event_id in self._seen:
                    continue
                if len(self._seen_order) >= 5000:
                    self._seen.discard(self._seen_order.popleft())
                self._seen.add(event_id)
                self._seen_order.append(event_id)
                self.consumed += 1
                self.records.append({"topic":message.topic, "partition":message.partition,
                                     "offset":message.offset, **envelope})
            except (ValueError, KeyError, TypeError, AttributeError):
                self.errors += 1
                self.last_error = "Invalid Kafka event envelope was skipped"
