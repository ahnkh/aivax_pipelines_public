#!/usr/bin/env python3
"""Grok upload multipart + promote attachment binding."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adapters import grok_upload  # noqa: E402


class TestGrokMultipart(unittest.TestCase):
    def test_cdn_complete_object_replaces_direct_upload_instead_of_appending(self) -> None:
        uid = "24c23fd9-a2ca-420c-b502-fca5f693e726"
        payload = b"complete-grok-file-payload" * 4
        req = {
            "direction": "response",
            "meta": {
                "host": "assets.grok.com",
                "path": f"/users/user-1/{uid}/content",
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(grok_upload._fum, "_UPLOAD_SAVE_DIR", tmpdir):
                self.assertTrue(grok_upload._fum.register_upload_mapping(uid, "document.pdf", {}))
                saved_path = grok_upload._fum.append_upload_payload(uid, payload, {})
                self.assertEqual(Path(saved_path).read_bytes(), payload)
                with mock.patch.object(grok_upload, "_remember_grok_file_binding"):
                    result = grok_upload._handle_grok_cdn_asset_response(req, payload)
                self.assertTrue(result.get("handled"))
                self.assertEqual(Path(saved_path).read_bytes(), payload)
                self.assertEqual(Path(saved_path).stat().st_size, len(payload))
                grok_upload._fum.discard_upload(uid)

    def test_direct_multipart_skips_filename_less_fields(self) -> None:
        boundary = "----AivaxBound"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="source_conversation_id"\r\n\r\n'
            f"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="doc.pdf"\r\n'
            f"Content-Type: application/pdf\r\n\r\n"
            f"%PDF-1.4 fake\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")
        req = {
            "content_type": f"multipart/form-data; boundary={boundary}",
            "meta": {"host": "www.grok.com", "path": "/http/upload-file-v2/direct"},
            "direction": "request",
            "request_id": "req-1",
        }
        with mock.patch.object(
            grok_upload._fum,
            "create_pending_upload_batch_from_files",
            return_value={
                "handled": True,
                "saved_files": [{"upload_id": "grok-abc", "filename": "doc.pdf"}],
                "upload_id": "grok-abc",
                "upload_filename": "doc.pdf",
            },
        ) as create:
            with mock.patch.object(grok_upload._fum, "remember_pending_uploads_for_scope"):
                with mock.patch.object(
                    grok_upload._fum,
                    "collection_policy_from_request",
                    return_value={},
                ):
                    with mock.patch.object(
                        grok_upload._fum,
                        "parse_multipart",
                        return_value=[
                            {"name": "source_conversation_id", "filename": "", "body": b"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
                            {"name": "file", "filename": "doc.pdf", "body": b"%PDF-1.4 fake"},
                        ],
                    ):
                        result = grok_upload._handle_grok_direct_multipart(req, body, "req-1")
        self.assertTrue(result.get("handled"))
        files = create.call_args[0][1]
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0][0], "doc.pdf")

    def test_build_policy_attachments_promotes_pending(self) -> None:
        req = {"meta": {"host": "www.grok.com", "path": "/ws/mgw"}}
        obj = {"fileAttachments": ["24c23fd9-a2ca-420c-b502-fca5f693e726"]}
        with mock.patch.object(
            grok_upload,
            "_promote_one_pending_to_asset",
            return_value={"handled": True, "upload_id": "24c23fd9-a2ca-420c-b502-fca5f693e726"},
        ) as promote:
            with mock.patch.object(
                grok_upload,
                "_resolve_grok_file_ref",
                return_value={"upload_id": "24c23fd9-a2ca-420c-b502-fca5f693e726", "file_name": "doc.pdf"},
            ):
                with mock.patch.object(
                    grok_upload,
                    "_finalize_policy_attachment",
                    return_value={
                        "id": "/run/aivax/attachments/24c23fd9-a2ca-420c-b502-fca5f693e726",
                        "size": 10,
                        "name": "doc.pdf",
                        "mime_type": "application/pdf",
                    },
                ):
                    rows = grok_upload.build_policy_attachments(req, obj, b"{}")
        promote.assert_called_once()
        self.assertEqual(rows[0]["name"], "doc.pdf")

    def test_recent_chat_file_used_when_ws_omits_file_mention(self) -> None:
        grok_upload._RECENT_CHAT_FILES_BY_SCOPE.clear()
        req = {
            "direction": "request",
            "meta": {"host": "www.grok.com", "path": "/ws/mgw/", "x_userid": "user-1"},
        }
        uid = "24c23fd9-a2ca-420c-b502-fca5f693e726"
        grok_upload.remember_recent_chat_file(req, uid)
        obj = {
            "event": {
                "type": "response.create",
                "item": {
                    "role": "user",
                    "x_grok": {"input_chunks": [{"text": {"text": "요약해줘"}}]},
                },
            }
        }
        with mock.patch.object(
            grok_upload,
            "_promote_one_pending_to_asset",
            return_value={"handled": True, "upload_id": uid},
        ):
            with mock.patch.object(
                grok_upload,
                "_resolve_grok_file_ref",
                return_value={"upload_id": uid, "file_name": "신청서.pdf"},
            ):
                with mock.patch.object(
                    grok_upload,
                    "_finalize_policy_attachment",
                    return_value={
                        "id": f"/run/aivax/attachments/{uid}",
                        "size": 10,
                        "name": "신청서.pdf",
                        "mime_type": "application/pdf",
                    },
                ):
                    rows = grok_upload.build_policy_attachments(req, obj, b"{}")
        self.assertEqual(rows[0]["name"], "신청서.pdf")


class TestGrokRecentScope(unittest.TestCase):
    def tearDown(self) -> None:
        grok_upload._RECENT_CHAT_FILES_BY_SCOPE.clear()

    def test_scope_uses_cdn_user_and_ws_uid(self) -> None:
        cdn_req = {
            "meta": {
                "host": "assets.grok.com",
                "path": "/users/c41affc0-e0c6-46e3-84ce-a11cff3d6edd/24c23fd9-a2ca-420c-b502-fca5f693e726/content",
            }
        }
        ws_req = {
            "meta": {
                "host": "grok.com",
                "path": "/ws/mgw/?uid=c41affc0-e0c6-46e3-84ce-a11cff3d6edd",
            }
        }
        self.assertEqual(
            grok_upload._grok_upload_scope(cdn_req),
            "grok:c41affc0-e0c6-46e3-84ce-a11cff3d6edd",
        )
        self.assertEqual(
            grok_upload._grok_upload_scope(ws_req),
            "grok:c41affc0-e0c6-46e3-84ce-a11cff3d6edd",
        )

    def test_cdn_save_remembers_for_later_ws_peek(self) -> None:
        uid = "24c23fd9-a2ca-420c-b502-fca5f693e726"
        user = "c41affc0-e0c6-46e3-84ce-a11cff3d6edd"
        payload = b"complete-grok-file-payload" * 4
        cdn_req = {
            "direction": "response",
            "meta": {
                "host": "assets.grok.com",
                "path": f"/users/{user}/{uid}/content",
            },
        }
        ws_req = {
            "direction": "request",
            "meta": {
                "host": "grok.com",
                "path": f"/ws/mgw/?uid={user}",
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(grok_upload._fum, "_UPLOAD_SAVE_DIR", tmpdir):
                grok_upload._fum.register_upload_mapping(uid, "주민번호.png", {})
                result = grok_upload._handle_grok_cdn_asset_response(cdn_req, payload)
                self.assertTrue(result.get("handled"))
                self.assertEqual(result.get("upload_id"), uid)
                self.assertIn(uid, grok_upload.peek_recent_chat_files(ws_req))
                # Must NOT leak into a different user's / orphan-only chat turn.
                other = {
                    "meta": {"host": "grok.com", "path": "/ws/mgw/?uid=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}
                }
                self.assertNotIn(uid, grok_upload.peek_recent_chat_files(other))
                grok_upload._fum.discard_upload(uid)

    def test_recent_peek_is_consumed_after_policy_bind(self) -> None:
        uid = "24c23fd9-a2ca-420c-b502-fca5f693e726"
        req = {
            "direction": "request",
            "meta": {"host": "grok.com", "path": "/ws/mgw/?uid=user-1", "x_userid": "user-1"},
        }
        grok_upload.remember_recent_chat_file(req, uid)
        self.assertEqual(grok_upload.peek_recent_chat_files(req), [uid])
        obj = {
            "event": {
                "type": "response.create",
                "item": {
                    "role": "user",
                    "x_grok": {"input_chunks": [{"text": {"text": "요약해줘"}}]},
                },
            }
        }
        with mock.patch.object(grok_upload, "_promote_one_pending_to_asset", return_value={"handled": True}):
            with mock.patch.object(
                grok_upload,
                "_resolve_grok_file_ref",
                return_value={"upload_id": uid, "file_name": "doc.pdf"},
            ):
                with mock.patch.object(
                    grok_upload,
                    "_finalize_policy_attachment",
                    return_value={
                        "id": f"/run/aivax/attachments/{uid}",
                        "size": 10,
                        "name": "doc.pdf",
                        "mime_type": "application/pdf",
                    },
                ):
                    rows = grok_upload.build_policy_attachments(req, obj, b"{}")
        self.assertEqual(rows[0]["name"], "doc.pdf")
        # Second text-only turn must not inherit the same file into UI.
        self.assertEqual(grok_upload.peek_recent_chat_files(req), [])

    def test_rest_asset_without_pending_still_remembers(self) -> None:
        uid = "24c23fd9-a2ca-420c-b502-fca5f693e726"
        req = {
            "direction": "response",
            "meta": {
                "host": "grok.com",
                "path": f"/rest/assets/{uid}",
                "x_userid": "user-1",
            },
        }
        payload = (
            b'{"fileMetadataId":"%s","fileName":"doc.pdf","fileMimeType":"application/pdf","sizeBytes":12}'
            % uid.encode()
        )
        with mock.patch.object(grok_upload._fum, "pop_pending_uploads_for_scope", return_value=[]):
            with mock.patch.object(grok_upload, "_pending_grok_ids_on_disk", return_value=[]):
                with mock.patch.object(
                    grok_upload._fum,
                    "collection_policy_from_request",
                    return_value={},
                ):
                    with mock.patch.object(
                        grok_upload._fum,
                        "register_upload_mapping",
                        return_value=True,
                    ):
                        with mock.patch.object(
                            grok_upload._fum,
                            "get_saved_attachment",
                            return_value={"id": f"/run/aivax/attachments/{uid}"},
                        ):
                            result = grok_upload._handle_grok_rest_asset_response(req, payload)
        self.assertTrue((result or {}).get("handled"))
        self.assertIn(uid, grok_upload.peek_recent_chat_files(req))

    def test_load_responses_with_file_meta_is_not_upload_finalize(self) -> None:
        """Chat history embeds fileAttachmentsMetadata — must not passthrough."""
        conv = "221816e6-6bda-4700-9873-00c9d0884390"
        req = {
            "direction": "response",
            "unit_type": "json_body",
            "content_type": "application/json",
            "meta": {
                "host": "grok.com",
                "path": f"/rest/app-chat/conversations/{conv}/load-responses",
            },
        }
        payload = json.dumps(
            {
                "responses": [
                    {
                        "sender": "human",
                        "message": "주민번호 이미지 확인해줘",
                        "fileAttachmentsMetadata": [
                            {
                                "fileMetadataId": "312ea686-b657-42f5-848c-8a45edcb44a6",
                                "fileName": "주민번호.png",
                            }
                        ],
                    },
                    {"sender": "assistant", "message": "확인했습니다."},
                ]
            },
            ensure_ascii=False,
        ).encode("utf-8")
        result = grok_upload.normalize_upload(req, payload)
        self.assertIsNone(result)
        from adapters import grok_engine

        text = grok_engine.extract(req, payload)
        self.assertEqual(text, "확인했습니다.")
        self.assertEqual(
            req.get("_grok_deferred_human_prompt"), "주민번호 이미지 확인해줘"
        )

    def test_load_responses_skips_when_ws_already_handled(self) -> None:
        from adapters import grok_engine

        conv = "221816e6-6bda-4700-9873-00c9d0884390"
        human = "127.0.0.1"
        assistant = "loopback address explanation"
        grok_engine._grok_turn_handled_clear_for_tests()
        grok_engine._grok_turn_mark_handled("prompt", conv, human)
        grok_engine._grok_turn_mark_handled(
            "output", conv, "different-ws-text", response_id="resp-same-id"
        )
        req = {
            "direction": "response",
            "unit_type": "json_body",
            "content_type": "application/json",
            "meta": {
                "host": "grok.com",
                "path": f"/rest/app-chat/conversations/{conv}/load-responses",
            },
        }
        payload = json.dumps(
            {
                "responses": [
                    {"sender": "human", "message": human},
                    {
                        "sender": "assistant",
                        "message": assistant,
                        "responseId": "resp-same-id",
                    },
                ]
            },
            ensure_ascii=False,
        ).encode("utf-8")
        text = grok_engine.extract(req, payload)
        self.assertEqual(text, "")
        self.assertFalse(req.get("_extract_final"))
        self.assertNotIn("_grok_deferred_human_prompt", req)


class TestGrokRejectedUploadDoesNotRenameSiblings(unittest.TestCase):
    """A refused AVIF must not rename PNG/BMP that already uploaded."""

    def setUp(self) -> None:
        grok_upload._RECENT_CHAT_FILES_BY_SCOPE.clear()
        grok_upload._GROK_FILE_METADATA_INDEX.clear()
        grok_upload._GROK_FILE_METADATA_ORDER.clear()

    def tearDown(self) -> None:
        grok_upload._RECENT_CHAT_FILES_BY_SCOPE.clear()
        grok_upload._GROK_FILE_METADATA_INDEX.clear()
        grok_upload._GROK_FILE_METADATA_ORDER.clear()

    def test_saved_png_and_bmp_keep_their_names_when_avif_temp_remains(self) -> None:
        user = "c41affc0-e0c6-46e3-84ce-a11cff3d6edd"
        png_id = "e1ac646f-996f-4052-8c46-e2d65805d80a"
        bmp_id = "f5931823-7a3d-4949-bb7b-e25045004c43"
        avif_tmp = "grok-2d28117f94854de8"
        png_name = "계좌번호 - sc제일은행.png"
        bmp_name = "계좌번호 - sc제일은행.bmp"
        avif_name = "계좌번호 - sc제일은행.avif"
        png = b"\x89PNG\r\n\x1a\n" + b"png-body"
        bmp = b"BM" + b"bmp-body"
        avif = b"\x00\x00\x00 ftypavif" + b"avif-body"
        req = {
            "direction": "request",
            "meta": {
                "host": "grok.com",
                "path": f"/ws/mgw/?uid={user}",
                "x_userid": user,
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict("os.environ", {"AIVAX_ATTACH_TMPFS": tmpdir}):
                with mock.patch.object(grok_upload._fum, "_UPLOAD_SAVE_DIR", tmpdir):
                    grok_upload._fum.save_inline_upload(png_id, png_name, png, {})
                    grok_upload._fum.save_inline_upload(bmp_id, bmp_name, bmp, {})
                    grok_upload._fum.save_inline_upload(avif_tmp, avif_name, avif, {})
                    grok_upload._remember_grok_file_binding(png_id, png_name, len(png), "image/png")
                    grok_upload._remember_grok_file_binding(bmp_id, bmp_name, len(bmp), "image/bmp")
                    grok_upload.remember_recent_chat_file(req, avif_tmp)
                    try:
                        rows = grok_upload.build_policy_attachments(
                            req,
                            {"fileAttachments": [png_id, bmp_id]},
                            b"{}",
                        )
                        self.assertEqual([row["name"] for row in rows], [png_name, bmp_name])
                        self.assertEqual((Path(tmpdir) / png_id).read_bytes(), png)
                        self.assertEqual((Path(tmpdir) / bmp_id).read_bytes(), bmp)
                        self.assertEqual((Path(tmpdir) / avif_tmp).read_bytes(), avif)
                        self.assertNotEqual(
                            grok_upload._fum.lookup_upload_id_by_registered_server_uuid(png_id),
                            avif_tmp,
                        )
                        self.assertNotEqual(
                            grok_upload._fum.lookup_upload_id_by_registered_server_uuid(bmp_id),
                            avif_tmp,
                        )
                        self.assertEqual(
                            grok_upload._resolve_grok_file_ref(png_id)["file_name"],
                            png_name,
                        )
                        self.assertEqual(
                            grok_upload._resolve_grok_file_ref(bmp_id)["file_name"],
                            bmp_name,
                        )
                    finally:
                        for upload_id in (png_id, bmp_id, avif_tmp):
                            grok_upload._fum.discard_upload(upload_id)

    def test_terminal_error_discards_the_refused_file_only(self) -> None:
        user = "c41affc0-e0c6-46e3-84ce-a11cff3d6edd"
        base = "82d2f6b1-9a95-423e-8a9c-5671471a9d64"
        upload_rid = base + "-up-0004863205e65e21"
        png_id = "e1ac646f-996f-4052-8c46-e2d65805d80a"
        png_name = "계좌번호 - sc제일은행.png"
        png = b"\x89PNG\r\n\x1a\n" + b"png-body"
        scope_req = {
            "direction": "request",
            "request_id": upload_rid,
            "meta": {"host": "grok.com", "path": "/http/upload-file-v2/direct", "x_userid": user},
        }
        ack = {
            "direction": "response",
            "request_id": base,
            "meta": {"host": "grok.com", "path": "/http/upload-file-v2/direct", "x_userid": user},
        }
        body = b'{"uploadId":"006b7a30-9485-4cc9-9b8b-35d799d3ee3f","terminalError":"Cannot open given image [WKE=file:parse-failed]"}'
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict("os.environ", {"AIVAX_ATTACH_TMPFS": tmpdir}):
                with mock.patch.object(grok_upload._fum, "_UPLOAD_SAVE_DIR", tmpdir):
                    grok_upload._fum.save_inline_upload(png_id, png_name, png, {})
                    created = grok_upload._fum.create_pending_upload_batch_from_files(
                        upload_rid,
                        [("계좌번호 - sc제일은행.avif", b"\x00\x00\x00 ftypavifrejected")],
                        "grok",
                        {},
                    )
                    avif_ids = [
                        str(item.get("upload_id") or "")
                        for item in created.get("saved_files") or []
                    ]
                    grok_upload._fum.remember_pending_uploads_for_scope(
                        f"grok:{user}", avif_ids
                    )
                    for avif_id in avif_ids:
                        grok_upload.remember_recent_chat_file(scope_req, avif_id)
                    try:
                        result = grok_upload.normalize_upload(ack, body)
                        self.assertTrue((result or {}).get("handled"))
                        self.assertEqual(
                            grok_upload._fum.peek_pending_uploads_for_scope(f"grok:{user}"),
                            [],
                        )
                        for avif_id in avif_ids:
                            self.assertFalse((Path(tmpdir) / avif_id).exists())
                            self.assertNotIn(avif_id, grok_upload.peek_recent_chat_files(ack))
                        self.assertTrue((Path(tmpdir) / png_id).exists())
                        rows = grok_upload.build_policy_attachments(
                            ack,
                            {"fileAttachments": [png_id]},
                            b"{}",
                        )
                        self.assertEqual(rows[0]["name"], png_name)
                        self.assertEqual((Path(tmpdir) / png_id).read_bytes(), png)
                    finally:
                        grok_upload._fum.discard_upload(png_id)
                        for avif_id in avif_ids:
                            grok_upload._fum.discard_upload(avif_id)

    def test_status_ack_keeps_the_pending_file(self) -> None:
        user = "c41affc0-e0c6-46e3-84ce-a11cff3d6edd"
        base = "11111111-2222-3333-4444-555555555555"
        upload_rid = base + "-up-aabbccddeeff0011"
        fresh_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        ack = {
            "direction": "response",
            "request_id": base,
            "meta": {"host": "grok.com", "path": "/http/upload-file-v2/direct", "x_userid": user},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict("os.environ", {"AIVAX_ATTACH_TMPFS": tmpdir}):
                with mock.patch.object(grok_upload._fum, "_UPLOAD_SAVE_DIR", tmpdir):
                    created = grok_upload._fum.create_pending_upload_batch_from_files(
                        upload_rid,
                        [("notes.pdf", b"%PDF-1.4 pending")],
                        "grok",
                        {},
                    )
                    temp_id = str(created["saved_files"][0]["upload_id"])
                    grok_upload._fum.remember_pending_uploads_for_scope(f"grok:{user}", [temp_id])
                    try:
                        result = grok_upload.normalize_upload(ack, b'{"status":1,"error":null}')
                        self.assertTrue((result or {}).get("handled"))
                        self.assertEqual((Path(tmpdir) / temp_id).read_bytes(), b"%PDF-1.4 pending")
                        rows = grok_upload.build_policy_attachments(
                            ack,
                            {"fileAttachments": [fresh_id]},
                            b"{}",
                        )
                        self.assertEqual(rows[0]["name"], "notes.pdf")
                        self.assertEqual((Path(tmpdir) / fresh_id).read_bytes(), b"%PDF-1.4 pending")
                        self.assertFalse((Path(tmpdir) / temp_id).exists())
                    finally:
                        grok_upload._fum.discard_upload(temp_id)
                        grok_upload._fum.discard_upload(fresh_id)
                        grok_upload._fum.pop_pending_uploads_for_scope(f"grok:{user}")


if __name__ == "__main__":
    unittest.main()
