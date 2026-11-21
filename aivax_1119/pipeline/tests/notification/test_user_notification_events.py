import threading
import unittest
from unittest.mock import Mock

from src.adapters.notification_user.valkey_user_notification_outbox_adapter import (
    ValkeyUserNotificationOutboxAdapter,
)
from src.domain.notification_user.models.user_notification_event import (
    UserNotificationEvent,
)


class UserNotificationEventsTest(unittest.TestCase):
    def _event(self, *, prompt: str = "hello") -> UserNotificationEvent:
        return UserNotificationEvent(
            message_id="message-1",
            client_ip="203.0.113.8",
            email="user@example.com",
            ai_service=2,
            prompt=prompt,
            name="홍길동",
        )

    def test_flush_batches_cached_events_as_management_user_sync_fields(self):
        client = Mock()
        pipeline = client.pipeline.return_value
        pipeline.execute.return_value = ["1-0", "2-0"]
        outbox = ValkeyUserNotificationOutboxAdapter(
            client,
            "management:user-sync:v1",
        )
        self.addCleanup(outbox.close)
        first_event = self._event()
        second_event = self._event(prompt="hello again")
        outbox.enqueue(first_event)
        outbox.enqueue(second_event)

        self.assertEqual(outbox.flush(), 2)
        self.assertEqual(
            pipeline.xadd.call_args_list[0].args,
            (
                "management:user-sync:v1",
                {
                    "messageId": "message-1",
                    "email": "user@example.com",
                    "aiServiceId": "2",
                    "ip": "203.0.113.8",
                    "prompt": "hello",
                    "name": "홍길동",
                },
            ),
        )
        self.assertEqual(
            [call[0] for call in pipeline.method_calls],
            ["xadd", "xadd", "execute"],
        )
        self.assertEqual(
            [call[0] for call in client.method_calls],
            ["pipeline"],
        )

    def test_failed_batch_is_retained_for_the_next_flush(self):
        client = Mock()
        pipeline = client.pipeline.return_value
        pipeline.execute.side_effect = [RuntimeError("Valkey offline"), ["1-0"]]
        outbox = ValkeyUserNotificationOutboxAdapter(
            client,
            "management:user-sync:v1",
        )
        self.addCleanup(outbox.close)
        outbox.enqueue(self._event())

        self.assertEqual(outbox.flush(), 0)
        self.assertEqual(outbox.flush(), 1)
        first_attempt = pipeline.xadd.call_args_list[0].args
        second_attempt = pipeline.xadd.call_args_list[1].args
        self.assertEqual(first_attempt, second_attempt)

    def test_requeued_events_are_flushed_before_newly_enqueued_events(self):
        client = Mock()
        pipeline = client.pipeline.return_value
        outbox = ValkeyUserNotificationOutboxAdapter(
            client,
            "management:user-sync:v1",
        )
        self.addCleanup(outbox.close)
        retry_event = self._event(prompt="retry first")
        new_event = self._event(prompt="new second")

        def fail_first_batch(**_kwargs: object) -> list[str]:
            outbox.enqueue(new_event)
            raise RuntimeError("Valkey offline")

        execution_count = 0

        def execute_side_effect(**kwargs: object) -> list[str]:
            nonlocal execution_count
            execution_count += 1
            if execution_count == 1:
                return fail_first_batch(**kwargs)
            return ["1-0", "2-0"]

        pipeline.execute.side_effect = execute_side_effect
        outbox.enqueue(retry_event)

        self.assertEqual(outbox.flush(), 0)
        self.assertEqual(outbox.flush(), 2)

        flushed_prompts = [
            call.args[1]["prompt"] for call in pipeline.xadd.call_args_list
        ]
        self.assertEqual(
            flushed_prompts,
            [
                "retry first",
                "retry first",
                "new second",
            ],
        )

    def test_discards_event_after_three_retries(self):
        client = Mock()
        pipeline = client.pipeline.return_value
        pipeline.execute.side_effect = RuntimeError("Valkey offline")
        outbox = ValkeyUserNotificationOutboxAdapter(
            client,
            "management:user-sync:v1",
        )
        self.addCleanup(outbox.close)
        outbox.enqueue(self._event())

        for _ in range(4):
            self.assertEqual(outbox.flush(), 0)

        self.assertEqual(pipeline.execute.call_count, 4)
        self.assertEqual(outbox._pending, [])

    def test_adapter_owns_a_one_second_style_periodic_flush_timer(self):
        client = Mock()
        pipeline = client.pipeline.return_value
        flushed = threading.Event()
        pipeline.execute.side_effect = lambda **_: flushed.set() or ["1-0"]
        outbox = ValkeyUserNotificationOutboxAdapter(
            client,
            "management:user-sync:v1",
        )
        self.addCleanup(outbox.close)
        outbox.enqueue(self._event())

        self.assertTrue(flushed.wait(1.5))
        pipeline.execute.assert_called_once_with(raise_on_error=False)
