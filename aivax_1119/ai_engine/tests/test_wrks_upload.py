from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


AI_ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AI_ENGINE / "core"))
sys.path.insert(0, str(AI_ENGINE / "adapters"))

import file_upload_manager as fum  # noqa: E402
import wrks_engine  # noqa: E402
import wrks_upload  # noqa: E402


def multipart(files: list[tuple[str, bytes]], boundary: str = "aivax-boundary") -> bytes:
    chunks: list[bytes] = []
    for filename, body in files:
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                (
                    'Content-Disposition: form-data; name="file"; '
                    f'filename="{filename}"\r\n'
                ).encode(),
                b"Content-Type: application/octet-stream\r\n\r\n",
                body,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks)


def request(direction: str, request_id: str, path: str = "/file/toOpenai") -> dict:
    return {
        "direction": direction,
        "request_id": request_id,
        "content_type": "multipart/form-data; boundary=aivax-boundary",
        "meta": {
            "host": "gateway-api.wrks.ai",
            "path": path,
            "authorization": "Bearer shared-token",
        },
    }


class WrksUploadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        fum._UPLOAD_SAVE_DIR = self.tempdir.name
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
        wrks_upload._WRKS_FILE_INDEX.clear()
        wrks_upload._WRKS_FILE_ORDER.clear()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_multi_file_request_response_and_policy_attachments(self) -> None:
        upload = wrks_upload.normalize_upload(
            request("request", "upload-request"),
            multipart([("a.txt", b"alpha"), ("b.txt", b"bravo")]),
        )
        self.assertIsNotNone(upload)
        self.assertEqual(2, len(upload["saved_files"]))

        response = {
            "data": {
                "files": [
                    {"name": "a.txt", "gptFileId": "file-a"},
                    {"name": "b.txt", "gptFileId": "file-b"},
                ]
            }
        }
        finalized = wrks_upload.normalize_upload(
            request("response", "different-response-id"),
            json.dumps(response).encode(),
        )
        self.assertEqual(
            ["file-a", "file-b"],
            [row["upload_id"] for row in finalized["saved_files"]],
        )

        attachments = wrks_upload.build_policy_attachments(
            request("request", "chat-request", "/conversation/c1/message/stream"),
            {"message": "inspect both", "gptFileIds": ["file-a", "file-b"]},
            b"",
        )
        self.assertEqual(["a.txt", "b.txt"], [row["name"] for row in attachments])
        self.assertEqual([b"alpha", b"bravo"], [Path(row["id"]).read_bytes() for row in attachments])

        chat_payload = json.dumps(
            {"message": "inspect both", "gptFileIds": ["file-a", "file-b"]}
        ).encode()
        chat_req = request("request", "chat-request", "/conversation/c1/message/stream")
        chat_req["content_type"] = "application/json"
        policy_payload = wrks_engine.build_policy_api_payload(
            chat_req,
            chat_payload,
            "inspect both",
        )
        self.assertIn("file_block_filter", policy_payload["filter_list"])
        self.assertEqual(["a.txt", "b.txt"], [row["name"] for row in policy_payload["attachments"]])

        blocked = wrks_engine.rewrite_prompt_for_block(
            chat_req,
            chat_payload,
            "AIVAX blocked this file",
            "",
        )
        self.assertIsNotNone(blocked)
        blocked_body = json.loads(blocked[0])
        self.assertEqual("AIVAX blocked this file", blocked_body["message"])
        self.assertEqual([], blocked_body["gptFileIds"])

    def test_image_upload_resolves_attachment_from_analyze_image_url(self) -> None:
        image_bytes = b"\x89PNG\r\n\x1a\n" + b"pixels"
        upload = wrks_upload.normalize_upload(
            request("request", "image-request", "/file/forConversation"),
            multipart([("screenshot.png", image_bytes)]),
        )
        self.assertIsNotNone(upload)

        token = "pHhMgXWwoIViucWKpczavUE6Xhviiwl9KXXfoWNK"
        response = {
            "data": {"file": {"imageUrl": f"https://images.wrks.ai/dalle/{token}.png"}}
        }
        finalized = wrks_upload.normalize_upload(
            request("response", "image-response", "/file/forConversation"),
            json.dumps(response).encode(),
        )
        self.assertEqual([token], [row["upload_id"] for row in finalized["saved_files"]])
        self.assertEqual("screenshot.png", finalized["saved_files"][0]["filename"])

        chat_body = {
            "message": (
                f"이미지입니다. [analyzeImageUrl] https://images.wrks.ai/dalle/{token}.png"
            )
        }
        attachments = wrks_upload.build_policy_attachments(
            request("request", "chat-request", "/conversation/c1/message/stream"),
            chat_body,
            b"",
        )
        self.assertEqual(1, len(attachments))
        self.assertEqual("screenshot.png", attachments[0]["name"])
        self.assertEqual(len(image_bytes), attachments[0]["size"])
        self.assertEqual(image_bytes, Path(attachments[0]["id"]).read_bytes())

        chat_req = request("request", "chat-request", "/conversation/c1/message/stream")
        chat_req["content_type"] = "application/json"
        policy_payload = wrks_engine.build_policy_api_payload(
            chat_req,
            json.dumps(chat_body, ensure_ascii=False).encode(),
            "",
        )
        self.assertEqual("이미지입니다.", policy_payload["prompt"])
        self.assertEqual(["screenshot.png"], [row["name"] for row in policy_payload["attachments"]])

    def test_out_of_order_responses_claim_only_matching_pending_upload(self) -> None:
        wrks_upload.normalize_upload(
            request("request", "upload-a"),
            multipart([("a.txt", b"alpha")]),
        )
        wrks_upload.normalize_upload(
            request("request", "upload-b"),
            multipart([("b.txt", b"bravo")]),
        )

        response_b = {"data": {"file": {"name": "b.txt", "gptFileId": "file-b"}}}
        finalized_b = wrks_upload.normalize_upload(
            request("response", "response-b"),
            json.dumps(response_b).encode(),
        )
        self.assertEqual("file-b", finalized_b["saved_files"][0]["upload_id"])

        response_a = {"data": {"file": {"name": "a.txt", "gptFileId": "file-a"}}}
        finalized_a = wrks_upload.normalize_upload(
            request("response", "response-a"),
            json.dumps(response_a).encode(),
        )
        self.assertEqual("file-a", finalized_a["saved_files"][0]["upload_id"])
        self.assertEqual(b"alpha", Path(fum.get_saved_attachment("file-a")["id"]).read_bytes())
        self.assertEqual(b"bravo", Path(fum.get_saved_attachment("file-b")["id"]).read_bytes())


if __name__ == "__main__":
    unittest.main()
