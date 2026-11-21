from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

AI_ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AI_ENGINE / "core"))
sys.path.insert(0, str(AI_ENGINE / "adapters"))

import file_upload_manager as fum  # noqa: E402
import gemini_upload  # noqa: E402


PROBE_UID = "AJjja9YdvobPsuvm6mFAPZpMiiHAjNornEQpYmcgIdu_ck-zplW4q4C8QEYUD8SxKH2w0AtD"
REAL_UID = "AJjja9aPnp80vKw2hu-Q_0sUguaUN-ayT0jLMm4XCETUVrCQeykKSy2WJm1MI351fUK_r6PmpmJSED3zS7nnSUBwFXUHsUdxBmukspLgY2agrg"
TTL_SEG = "fk5cuolunof7ahyoobqzpozz4uqedu1786876870_Ad6Osdel5WSKUSeFwx67Oauoxphd2zTh3AIZao6mkS2oRe96ZHywAF01yCyj"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 6000


def _reset_fum() -> None:
    for mapping_name in (
        "_FILE_COLLECTION_META",
        "_COLLECTION_DENIED",
        "_upload_id_map",
        "_upload_server_uuid_alias",
        "_upload_ttl_segment_alias",
        "_saved_file_paths",
        "_saved_file_sizes",
        "_pending_upload_ids_by_request",
        "_pending_upload_ids_by_scope",
    ):
        getattr(fum, mapping_name).clear()
    for heap_name in (
        "_upload_expiry_heap",
        "_pending_request_expiry_heap",
        "_scope_pending_expiry_heap",
    ):
        getattr(fum, heap_name).clear()
    gemini_upload._PENDING_UPLOAD_IDS.clear()
    gemini_upload._PENDING_UPLOAD_FILENAMES.clear()
    gemini_upload._CLAIMED_POLICY_UPLOADS.clear()
    gemini_upload._INIT_NAME_BY_REQUEST.clear()
    gemini_upload._UPLOAD_CLIENT.clear()


def _stream_payload(segment: str, name: str, size: int, mime: str = "image/jpeg") -> bytes:
    inner = json.dumps(
        [[[["/contrib_service/ttl_1d/" + segment, size, None, mime], name]]],
        ensure_ascii=False,
    )
    return b"f.req=" + quote(inner).encode("utf-8")


class GeminiUploadIdTest(unittest.TestCase):
    def test_current_and_legacy_guploader_ids(self) -> None:
        self.assertTrue(gemini_upload._is_gemini_local_upload_id(PROBE_UID))
        self.assertTrue(gemini_upload._is_gemini_local_upload_id(REAL_UID))
        self.assertTrue(
            gemini_upload._is_gemini_local_upload_id(
                "AKh73eQJaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            )
        )
        self.assertFalse(gemini_upload._is_gemini_local_upload_id("file-abc123"))
        self.assertFalse(
            gemini_upload._is_gemini_local_upload_id("8a4897a5-3a7c-4ce6-846c-d94a150e3e56")
        )
        self.assertFalse(gemini_upload._is_gemini_local_upload_id("grok-a0e5a6a21de044e7"))
        self.assertTrue(
            gemini_upload._is_gemini_local_upload_id(
                "AP6rU836JpBUQsd8P4MJfYByo9DZ_yKI6b0wqPwEcLRZPUxELbZ9SIKEI5gHM6dK"
            )
        )


class GeminiUploadResolveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        fum._UPLOAD_SAVE_DIR = self.tempdir.name
        _reset_fum()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_ttl_mapped_ajjja_id_is_not_dropped(self) -> None:
        path = fum.append_upload_payload(REAL_UID, JPEG, {})
        self.assertTrue(path)
        fum.register_upload_ttl_segment(TTL_SEG, REAL_UID)
        item = gemini_upload._finalize_gemini_attachment_item(
            fum.lookup_upload_id_by_ttl_segment(TTL_SEG),
            "image/jpeg",
            "img_1786595188171.jpg",
            size_hint=len(JPEG),
            ttl_segment=TTL_SEG,
        )
        self.assertTrue(str(item.get("id") or "").endswith(REAL_UID))
        self.assertGreater(int(item.get("size") or 0), 1000)
        self.assertEqual(item.get("name"), "img_1786595188171.jpg")

    def test_two_session_upload_binds_filename_and_policy_attachment(self) -> None:
        fum.register_upload_mapping(PROBE_UID, "img_1786595188171.jpg", {})
        req = {
            "direction": "request",
            "unit_type": "raw_body",
            "request_id": "up-1",
            "meta": {
                "path": f"/upload/?upload_id={REAL_UID}",
                "host": "generativelanguage.googleapis.com",
            },
        }
        saved = gemini_upload.normalize_upload(req, JPEG)
        self.assertTrue(saved and saved.get("handled"))
        self.assertEqual(
            fum.get_registered_upload_filename(REAL_UID),
            "img_1786595188171.jpg",
        )
        fum.register_upload_ttl_segment(TTL_SEG, REAL_UID)

        atts = gemini_upload.build_policy_attachments(
            _stream_payload(TTL_SEG, "img_1786595188171.jpg", len(JPEG))
        )
        self.assertEqual(len(atts), 1)
        self.assertTrue(str(atts[0].get("id") or "").endswith(REAL_UID))
        self.assertEqual(atts[0].get("name"), "img_1786595188171.jpg")
        self.assertGreater(int(atts[0].get("size") or 0), 1000)

    def test_filename_binds_to_newest_pending_id_not_a_stale_one(self) -> None:
        stale = "AP6rU80BpFgLOJEXMcWZ6RXIsNkAu26tR_MQye5UPT1_WuotTZJmmx09qvR5ynPj"
        fresh = "AP6rU81e8v0TlncjM1SEEWkj8aMHgDHM7hybzHCvsIbo87islraxSxz94ZNAv3z"
        gemini_upload._remember_pending_upload_id(stale)
        gemini_upload._remember_pending_upload_id(fresh)
        req = {
            "direction": "request",
            "unit_type": "raw_body",
            "request_id": "name-1",
            "meta": {"path": "/upload/", "host": "push.clients6.google.com"},
        }
        gemini_upload.normalize_upload(req, b"File name: Big-Endian-and-Little-Endian-1.png")
        self.assertEqual(
            fum.get_registered_upload_filename(fresh),
            "Big-Endian-and-Little-Endian-1.png",
        )
        self.assertEqual(fum.get_registered_upload_filename(stale), "")

    def test_parallel_uploads_keep_their_own_names(self) -> None:
        def init(rid: str, name: str) -> None:
            gemini_upload.normalize_upload(
                {
                    "direction": "request",
                    "unit_type": "raw_body",
                    "request_id": f"{rid}-up-0001",
                    "meta": {"path": "/upload/", "host": "push.clients6.google.com"},
                },
                f"File name: {name}".encode(),
            )

        def answer(rid: str, uid: str) -> None:
            gemini_upload.normalize_upload(
                {
                    "direction": "response",
                    "request_id": rid,
                    "meta": {
                        "path": "/upload/",
                        "host": "push.clients6.google.com",
                        "upload_id": uid,
                    },
                },
                b"",
            )

        phone = "AP6rU80MR4BN0pJ6UZ8A7MZwdoOYAa2daIM9cCCI2_9tmf5ODkg4w_kiL6aXrJ42"
        endian = "AP6rU80IU8y4f5z37pLrlr9xdg3KZ1JwsF8QANIkxdHmKOol8rgecMD2OvE16XEh"
        init("phone-req", "전화번호.png")
        init("endian-req", "Big-Endian-and-Little-Endian-1.png")
        answer("endian-req", endian)
        answer("phone-req", phone)
        self.assertEqual(fum.get_registered_upload_filename(phone), "전화번호.png")
        self.assertEqual(
            fum.get_registered_upload_filename(endian), "Big-Endian-and-Little-Endian-1.png"
        )

    def test_process_session_claims_only_this_clients_upload(self) -> None:
        uid = "AP6rU836JpBUQsd8P4MJfYByo9DZ_yKI6b0wqPwEcLRZPUxELbZ9SIKEI5gHM6dK"
        other = "AP6rU836JpBUQsd8P4MJfYByo9DZ_yKI6b0wqPwEcLRZPUxELbZ9SIKEI5gOTHER1"
        fum.register_upload_mapping(uid, "Big-Endian-and-Little-Endian-1.png", {})
        fum.append_upload_payload(uid, b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, {})
        fum.register_upload_mapping(other, "redis.md", {})
        fum.append_upload_payload(other, b"# redis\n" + b"\x00" * 64, {})
        owner = {"meta": {"client_ip": "10.0.21.142"}}
        stranger = {"meta": {"client_ip": "10.0.80.172"}}
        gemini_upload._remember_upload_client(uid, owner)
        gemini_upload._remember_upload_client(other, stranger)

        self.assertEqual(gemini_upload.claim_recent_gemini_attachments(), [])
        stolen = gemini_upload.claim_recent_gemini_attachments(req=owner)
        self.assertEqual(len(stolen), 1)
        self.assertEqual(stolen[0].get("name"), "Big-Endian-and-Little-Endian-1.png")
        self.assertEqual(gemini_upload.claim_recent_gemini_attachments(req=owner), [])
        self.assertEqual(
            gemini_upload.claim_recent_gemini_attachments(req=stranger)[0].get("name"),
            "redis.md",
        )

    def test_file_already_on_a_prompt_is_not_claimed_again(self) -> None:
        fum.register_upload_mapping(REAL_UID, "img_1786595188171.jpg", {})
        fum.append_upload_payload(REAL_UID, JPEG, {})
        fum.register_upload_ttl_segment(TTL_SEG, REAL_UID)
        owner = {"meta": {"client_ip": "10.0.3.26"}}
        gemini_upload._remember_upload_client(REAL_UID, owner)
        attached = gemini_upload.build_policy_attachments(
            _stream_payload(TTL_SEG, "img_1786595188171.jpg", len(JPEG)),
            owner,
        )
        self.assertTrue(str(attached[0].get("id") or "").endswith(REAL_UID))
        self.assertEqual(gemini_upload.claim_recent_gemini_attachments(req=owner), [])


class TestFileOnlyStreamGenerate(unittest.TestCase):
    def test_known_chat_with_file_and_no_prompt_is_not_continuation(self) -> None:
        if str(AI_ENGINE) not in sys.path:
            sys.path.insert(0, str(AI_ENGINE))
        from adapters import gemini_engine

        sid = "c_fileonlychat"
        gemini_engine.register_gemini_turn_parent(sid, "req-parent")
        req = {
            "direction": "request",
            "meta": {
                "host": "gemini.google.com",
                "path": "/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate",
            },
        }
        file_turn = json.dumps(
            [
                None,
                json.dumps(
                    [
                        [
                            "",
                            0,
                            None,
                            [
                                [
                                    [
                                        "/contrib_service/ttl_1d/segfileonly123",
                                        10,
                                        None,
                                        "application/pdf",
                                    ],
                                    "a.pdf",
                                ]
                            ],
                        ],
                        None,
                        sid,
                    ]
                ),
            ]
        )
        payload = ("f.req=" + quote(file_turn)).encode("utf-8")
        self.assertFalse(gemini_engine.gemini_is_continuation_request(req, payload))

        continuation = json.dumps(
            [None, json.dumps([["", 0, None, []], None, sid])]
        )
        empty = ("f.req=" + quote(continuation)).encode("utf-8")
        self.assertTrue(gemini_engine.gemini_is_continuation_request(req, empty))


class TestProcessSessionFileOnly(unittest.TestCase):
    """A logged-in file send has an empty user box and the path only inside protobuf."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        fum._UPLOAD_SAVE_DIR = self.tempdir.name
        _reset_fum()
        if str(AI_ENGINE) not in sys.path:
            sys.path.insert(0, str(AI_ENGINE))
        from adapters import gemini_engine
        from adapters import gemini_upload as adapter_upload

        self.engine = gemini_engine
        # The engine imports adapters.gemini_upload. The test module's
        # gemini_upload is a second copy when adapters/ is also on sys.path.
        self.upload = adapter_upload
        adapter_upload._CLAIMED_POLICY_UPLOADS.clear()
        adapter_upload._UPLOAD_CLIENT.clear()
        gemini_engine._GEMINI_GSESSION_CONV.clear()
        gemini_engine._GEMINI_GSESSION_PROMPT.clear()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _form(self, *records: bytes) -> bytes:
        import base64
        from urllib.parse import urlencode

        pairs = [("count", str(len(records)))]
        for index, record in enumerate(records):
            pairs.append((f"req{index}___data__", base64.b64encode(record).decode()))
        return urlencode(pairs).encode()

    def _req(self, rid: str) -> dict:
        return {
            "direction": "request",
            "request_id": rid,
            "meta": {
                "host": "geminiweb-pa.clients6.google.com",
                "path": "/v1/processSession?gsessionid=gs-file&VER=8",
                "client_ip": "10.0.50.60",
            },
        }

    def _save(self) -> None:
        fum.register_upload_mapping(REAL_UID, "새 문서.hwp", {})
        fum.append_upload_payload(REAL_UID, b"HWP" + b"\x00" * 64, {})
        fum.register_upload_ttl_segment(TTL_SEG, REAL_UID)
        self.upload._remember_upload_client(REAL_UID, self._req("up"))

    def _file_turn(self) -> bytes:
        path = f"/contrib_service/ttl_1d/{TTL_SEG}".encode()
        context = self.engine._encode_pb_ld(1, path)
        empty_box = self.engine._encode_process_session_prompt("")
        return self._form(context, empty_box)

    def test_file_only_turn_is_inspected_and_not_copied_onto_the_next_prompt(self) -> None:
        self._save()
        file_req = self._req("file-turn")
        payload = self._file_turn()
        self.assertEqual(self.engine.extract(file_req, payload), "사용자가 파일을 업로드하였습니다.")
        self.assertTrue(file_req.get("_gemini_file_only"))
        self.assertEqual(self.engine._lookup_process_session_prompt("gs-file"), "file-turn")

        policy = self.engine.build_policy_api_payload(
            file_req, payload, "사용자가 파일을 업로드하였습니다."
        )
        self.assertIsNotNone(policy)
        self.assertIn("file_block_filter", policy["filter_list"])
        self.assertEqual(policy["attachments"][0]["name"], "새 문서.hwp")
        self.assertTrue(str(policy["attachments"][0]["id"]).endswith(REAL_UID))

        follow = self._req("text-turn")
        follow_body = self._form(
            self.engine._encode_pb_ld(1, f"/contrib_service/ttl_1d/{TTL_SEG}".encode()),
            self.engine._encode_process_session_prompt("뭐지...?"),
        )
        self.assertEqual(self.engine.extract(follow, follow_body), "뭐지...?")
        self.assertFalse(follow.get("_gemini_file_only"))
        follow_policy = self.engine.build_policy_api_payload(follow, follow_body, "뭐지...?")
        self.assertEqual(follow_policy["prompt"], "뭐지...?")
        self.assertFalse(any(item.get("id") for item in follow_policy["attachments"]))
        self.assertEqual(self.upload.claim_recent_gemini_attachments(req=self._req("later")), [])

    def test_context_post_with_a_path_is_not_a_file_turn(self) -> None:
        self._save()
        req = self._req("ctx")
        payload = self._form(
            self.engine._encode_pb_ld(1, f"/contrib_service/ttl_1d/{TTL_SEG}".encode())
        )
        self.assertEqual(self.engine.extract(req, payload), "")
        self.assertFalse(req.get("_gemini_file_only"))

    def test_block_rewrites_the_empty_box_and_drops_the_file_path(self) -> None:
        self._save()
        req = self._req("file-turn")
        payload = self._file_turn()
        rewritten, reason = self.engine.rewrite_prompt_for_block(
            req, payload, "[AIVAX] 첨부파일 차단", "지시문", ""
        )
        self.assertIn("block", reason)
        self.assertNotIn(TTL_SEG.encode(), rewritten)
        again = self.engine.extract(req, rewritten)
        self.assertIn("[AIVAX] 첨부파일 차단", again)
        self.assertIn("지시문", again)

    def test_captured_file_only_body(self) -> None:
        captured = Path(
            "/home/juyonglee/AIVAX/inline_engine/aivax/evidence/PROVE/"
            "I350_BYPASS_1G/empty_gemini/"
            "1794352289_533a02b5-831c-43ea-9a43-3a4585b670a5_"
            "process_session_no_prompt_v1_processSession.bin"
        )
        if not captured.is_file():
            self.skipTest("captured body is not on this machine")
        body = captured.read_bytes()
        segments = self.engine._process_session_ttl_segments(body)
        self.assertTrue(segments)
        fum.register_upload_mapping(REAL_UID, "새 문서.hwp", {})
        fum.append_upload_payload(REAL_UID, b"HWP" + b"\x00" * 80, {})
        fum.register_upload_ttl_segment(segments[0], REAL_UID)
        req = self._req("captured")
        self.assertEqual(self.engine.extract(req, body), "사용자가 파일을 업로드하였습니다.")
        policy = self.engine.build_policy_api_payload(
            req, body, "사용자가 파일을 업로드하였습니다."
        )
        self.assertEqual(policy["attachments"][0]["name"], "새 문서.hwp")


if __name__ == "__main__":
    unittest.main()
