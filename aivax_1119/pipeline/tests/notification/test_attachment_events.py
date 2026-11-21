import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from src.adapters.notification_attachment import (
    AttachmentNotificationMapper,
    ValkeyAttachmentNotificationOutboxAdapter,
)
from src.application.services.notification_attachment import (
    AttachmentNotificationService,
)
from src.bootstrap import PipelineBootstrap
from src.bootstrap.env_loader import EnvironmentLoader
from src.core.file import FileMetadata
from src.domain.notification_attachment.models import AttachmentNotificationEvent
from src.domain.notification_attachment.ports import (
    AttachmentNotificationOutboxPort,
)


class AttachmentEventsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.backup_root = Path(self._temporary_directory.name)
        self.backup_path = self.backup_root / "report.pdf"
        self.backup_path.write_bytes(b"attachment data")
        self.metadata_reader = Mock()
        self.metadata_reader.read.return_value = FileMetadata(
            mime_type="application/pdf",
            extension="pdf",
            size_bytes=42,
        )
        self.file = {
            "file_name": "report.pdf",
            "file_backup_path": str(self.backup_path),
            "file_info": {"size": 42, "mime_type": "application/pdf"},
        }

    def _event(self) -> AttachmentNotificationEvent:
        return AttachmentNotificationEvent(
            message_id="message-1",
            index_name="aivax_log*",
            file_name="report.pdf",
            file_backup_path=str(self.backup_path),
            file_size_bytes=42,
            detected_mime_type="application/pdf",
        )

    def _service(
        self,
        outbox: AttachmentNotificationOutboxPort,
        index_name: str = "aivax_log*",
    ) -> AttachmentNotificationService:
        return AttachmentNotificationService(
            outbox,
            index_name,
            metadata_reader=self.metadata_reader,
            shared_backup_roots=(self.backup_root,),
        )

    def test_mapper_uses_only_the_six_management_fields(self) -> None:
        event = self._event()
        fields = AttachmentNotificationMapper().toPersistence(event)

        self.assertEqual(
            fields,
            {
                "messageId": "message-1",
                "indexName": "aivax_log*",
                "fileName": "report.pdf",
                "fileBackupPath": str(self.backup_path),
                "fileSizeBytes": 42,
                "detectedMimeType": "application/pdf",
            },
        )
        self.assertEqual(AttachmentNotificationMapper().toDomain(fields), event)

    def test_ip_and_occurrence_time_survive_outbox_serialization(self) -> None:
        outbox = Mock(spec=AttachmentNotificationOutboxPort)
        self.assertTrue(
            self._service(outbox).enqueue_archived_file(
                message_id="message-1",
                file_name="report.pdf",
                file_backup_path=str(self.backup_path),
                source_ip=" 123.123.123.123 ",
                occurred_at="2026-10-06T19:00:00+09:00",
            )
        )
        event = outbox.enqueue.call_args.args[0]
        fields = AttachmentNotificationMapper().toPersistence(event)
        self.assertEqual(fields["sourceIp"], "123.123.123.123")
        self.assertEqual(fields["occurredAt"], "2026-10-06T10:00:00Z")
        self.assertEqual(AttachmentNotificationMapper().toDomain(fields), event)
        self.assertEqual(event.event_id, self._event().event_id)

    def test_naive_occurrence_time_is_not_guessed(self) -> None:
        outbox = Mock(spec=AttachmentNotificationOutboxPort)
        self.assertTrue(
            self._service(outbox).enqueue_archived_file(
                message_id="message-1",
                file_name="report.pdf",
                file_backup_path=str(self.backup_path),
                occurred_at="2026-10-06T19:00:00",
            )
        )
        self.assertIsNone(outbox.enqueue.call_args.args[0].occurred_at)

    def test_service_enqueues_one_event_for_one_archived_file(self) -> None:
        outbox = Mock(spec=AttachmentNotificationOutboxPort)
        service = self._service(outbox)

        enqueued = service.enqueue_archived_file(
            message_id="message-1",
            file_name="report.pdf",
            file_backup_path=str(self.backup_path),
        )

        self.assertTrue(enqueued)
        self.assertEqual(outbox.enqueue.call_count, 1)
        self.assertEqual(outbox.enqueue.call_args.args[0], self._event())

    def test_service_rejects_a_missing_backup(self) -> None:
        outbox = Mock(spec=AttachmentNotificationOutboxPort)
        service = self._service(outbox)

        with self.assertLogs(level="ERROR"):
            enqueued = service.enqueue_archived_file(
                message_id="message-1",
                file_name="report.pdf",
                file_backup_path=str(self.backup_root / "missing.pdf"),
            )

        self.assertFalse(enqueued)
        outbox.enqueue.assert_not_called()

    def test_metadata_failure_does_not_enqueue_an_incomplete_event(self) -> None:
        outbox = Mock(spec=AttachmentNotificationOutboxPort)
        service = self._service(outbox)
        self.metadata_reader.read.side_effect = RuntimeError("metadata unavailable")

        with self.assertLogs(level="ERROR"):
            enqueued = service.enqueue_archived_file(
                message_id="message-1",
                file_name="report.pdf",
                file_backup_path=str(self.backup_path),
            )

        self.assertFalse(enqueued)
        outbox.enqueue.assert_not_called()

    def test_service_uses_metadata_read_from_the_completed_backup_file(self) -> None:
        outbox = Mock(spec=AttachmentNotificationOutboxPort)
        self.metadata_reader.read.return_value = FileMetadata(
            mime_type="application/pdf",
            extension="pdf",
            size_bytes=248_321,
        )

        self.assertTrue(
            self._service(outbox).enqueue_archived_file(
                message_id="message-1",
                file_name="report.pdf",
                file_backup_path=str(self.backup_path),
            ),
        )

        event = outbox.enqueue.call_args.args[0]
        self.assertEqual(event.file_size_bytes, 248_321)
        self.assertEqual(event.detected_mime_type, "application/pdf")
        self.assertEqual(event.file_backup_path, str(self.backup_path))

    def test_service_rejects_a_backup_outside_management_shared_roots(self) -> None:
        outbox = Mock(spec=AttachmentNotificationOutboxPort)
        service = AttachmentNotificationService(
            outbox,
            "aivax_log*",
            metadata_reader=self.metadata_reader,
            shared_backup_roots=(self.backup_root / "other",),
        )

        with self.assertLogs(level="ERROR"):
            enqueued = service.enqueue_archived_file(
                message_id="message-1",
                file_name="report.pdf",
                file_backup_path=str(self.backup_path),
            )

        self.assertFalse(enqueued)
        outbox.enqueue.assert_not_called()

    def test_mapper_omits_an_empty_optional_index_name(self) -> None:
        event = AttachmentNotificationEvent(
            message_id="message-1",
            index_name=None,
            file_name="report.pdf",
            file_backup_path=str(self.backup_path),
            file_size_bytes=42,
            detected_mime_type="application/pdf",
        )

        fields = AttachmentNotificationMapper().toPersistence(event)

        self.assertNotIn("indexName", fields)
        self.assertIsNone(AttachmentNotificationMapper().toDomain(fields).index_name)

    def test_outbox_flushes_the_six_fields_with_a_dedupe_marker(self) -> None:
        client = Mock()
        pipeline = client.pipeline.return_value
        pipeline.execute.return_value = ["1-0"]
        outbox = ValkeyAttachmentNotificationOutboxAdapter(
            client,
            "management:file-asset:v1",
        )
        self.addCleanup(outbox.close)
        event = self._event()
        outbox.enqueue(event)

        self.assertEqual(outbox.flush(), 1)
        eval_args = pipeline.eval.call_args.args
        self.assertEqual(
            eval_args[1:5],
            (
                2,
                f"management:file-asset:v1:dedupe:{event.event_id}",
                "management:file-asset:v1",
                "86400",
            ),
        )
        script = eval_args[0].upper()
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
        self.assertEqual(
            dict(zip(eval_args[5::2], eval_args[6::2])),
            {
                "messageId": "message-1",
                "indexName": "aivax_log*",
                "fileName": "report.pdf",
                "fileBackupPath": str(self.backup_path),
                "fileSizeBytes": "42",
                "detectedMimeType": "application/pdf",
            },
        )

    def test_failed_batch_is_retained_for_the_next_flush(self) -> None:
        client = Mock()
        pipeline = client.pipeline.return_value
        pipeline.execute.side_effect = [RuntimeError("offline"), ["1-0"]]
        outbox = ValkeyAttachmentNotificationOutboxAdapter(
            client,
            "management:file-asset:v1",
        )
        self.addCleanup(outbox.close)
        outbox.enqueue(self._event())

        self.assertEqual(outbox.flush(), 0)
        self.assertEqual(outbox.flush(), 1)

    def test_bootstrap_wires_attachment_outbox(self) -> None:
        settings = EnvironmentLoader().build_settings(
            {
                "AIVAX_PIPELINE_EVENT_ATTACHMENT_STREAM_NAME": "custom:files",
                "AIVAX_PIPELINE_EVENT_ATTACHMENT_INDEX_NAME": "custom_log*",
            }
        )
        client = Mock()
        client.pipeline.return_value.execute.return_value = ["1-0"]
        with patch(
            "src.bootstrap.pipeline_bootstrap.ValkeyConnecter",
        ) as connecter_class:
            connecter = connecter_class.return_value
            connecter.connect.return_value = client
            bootstrap = PipelineBootstrap(
                settings,
                http_client_registry=None,
                logger=Mock(),
            )
            outbox, created_connecter = (
                bootstrap._create_attachment_notification_outbox()
            )

        self.assertIs(created_connecter, connecter)
        self.assertIsNotNone(outbox)
        assert outbox is not None
        service = bootstrap._create_attachment_notification_service(outbox)
        self.assertIsNotNone(service)
        assert service is not None
        self.addCleanup(outbox.close)
        self.assertEqual(service._index_name, "custom_log*")

    def test_event_outboxes_ignore_obsolete_disable_flags(self) -> None:
        settings = EnvironmentLoader().build_settings(
            {
                "AIVAX_PIPELINE_VALKEY_ENABLED": "false",
                "AIVAX_PIPELINE_EVENT_ATTACHMENT_ENABLED": "false",
                "AIVAX_PIPELINE_EVENT_POLICY_NOTIFICATION_ENABLED": "false",
                "AIVAX_PIPELINE_EVENT_USER_SYNC_ENABLED": "false",
            }
        )
        with patch("src.bootstrap.pipeline_bootstrap.ValkeyConnecter") as factory:
            bootstrap = PipelineBootstrap(
                settings, http_client_registry=None, logger=Mock()
            )
            for create in (
                bootstrap._create_attachment_notification_outbox,
                bootstrap._create_policy_notification_outbox,
                bootstrap._create_user_notification_outbox,
            ):
                with self.subTest(factory=create.__name__):
                    outbox, connecter = create()
                    self.assertIsNotNone(outbox)
                    self.assertIs(connecter, factory.return_value)
                    self.addCleanup(outbox.close)
            self.assertEqual(factory.call_count, 3)


if __name__ == "__main__":
    unittest.main()
