import unittest
from unittest.mock import Mock

from src.adapters.notification_policy.valkey_policy_notification_outbox_adapter import (
    ValkeyPolicyNotificationOutboxAdapter,
)
from src.domain.notification_policy.models.occurred_at import OccurredAt
from src.domain.notification_policy.models.policy_notification_event import (
    PolicyNotificationEvent,
)


class PolicyNotificationEventsTest(unittest.TestCase):
    def _event(
        self,
        *,
        occurred_at: str = "2026-09-09T01:02:03Z",
    ) -> PolicyNotificationEvent:
        return PolicyNotificationEvent(
            occurred_at=OccurredAt(occurred_at),
            message_id="message-1",
            user_id="user-1",
            user_email="user@example.com",
            ai_service="chatgpt",
            prompt="hello",
            mode="block",
            output_text="blocked",
        )

    def test_discards_event_after_three_retries(self) -> None:
        client = Mock()
        pipeline = client.pipeline.return_value
        pipeline.execute.side_effect = RuntimeError("Valkey offline")
        outbox = ValkeyPolicyNotificationOutboxAdapter(
            client,
            "management:policy-notification:v1",
        )
        self.addCleanup(outbox.close)
        outbox.enqueue(self._event())

        # 최초 발행 한 번과 재시도 세 번 후에는 버퍼에서 폐기한다.
        for _ in range(4):
            self.assertEqual(outbox.flush(), 0)

        self.assertEqual(pipeline.execute.call_count, 4)
        self.assertEqual(outbox._pending, [])

    def test_publishes_only_management_field_value_pairs(self) -> None:
        client = Mock()
        pipeline = client.pipeline.return_value
        pipeline.execute.return_value = ["1726308000000-0"]
        outbox = ValkeyPolicyNotificationOutboxAdapter(
            client,
            "management:policy-notification:v1",
        )
        self.addCleanup(outbox.close)
        outbox.enqueue(self._event())

        self.assertEqual(outbox.flush(), 1)

        arguments = pipeline.eval.call_args.args
        self.assertEqual(
            arguments[1:4],
            (
                2,
                outbox._dedupe_key(self._event()),
                "management:policy-notification:v1",
            ),
        )
        script = arguments[0].upper()
        self.assertIn("REDIS.CALL('XADD'", script)
        for forbidden_command in ("XGROUP", "XTRIM", "FLUSHDB", "REDIS.CALL('DEL'"):
            self.assertNotIn(forbidden_command, script)
        self.assertEqual(
            [call[0] for call in pipeline.method_calls],
            ["eval", "execute"],
        )
        self.assertEqual(
            [call[0] for call in client.method_calls],
            ["pipeline"],
        )
        stream_fields = dict(zip(arguments[5::2], arguments[6::2]))
        self.assertEqual(
            stream_fields,
            {
                "timestamp": "2026-09-09T01:02:03Z",
                "messageId": "message-1",
                "userId": "user-1",
                "userEmail": "user@example.com",
                "aiService": "chatgpt",
                "prompt": "hello",
                "mode": "block",
                "outputText": "blocked",
            },
        )
        self.assertNotIn("eventId", stream_fields)
        self.assertNotIn("eventType", stream_fields)
        self.assertNotIn("occurredAt", stream_fields)
