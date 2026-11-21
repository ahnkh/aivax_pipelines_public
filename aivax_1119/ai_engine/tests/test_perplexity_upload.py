#!/usr/bin/env python3
"""Perplexity S3 host matching + legacy results URL logging + ask attachment bind."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AI_ENGINE = Path(__file__).resolve().parents[1]
for _path in (AI_ENGINE, AI_ENGINE / "core", AI_ENGINE / "adapters"):
    sys.path.insert(0, str(_path))

import file_upload_manager as fum  # noqa: E402
import perplexity_upload  # noqa: E402
from core import ai_policy_engine  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 6000
CLIENT_ID = "cd8fc0ef-5271-4cd3-a0cc-705c4244c231"
FILE_UUID = "77952f98-3184-43a5-802c-8f1e81ec3767"


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
    perplexity_upload._PPLX_FILE_META.clear()
    perplexity_upload._PPLX_FILE_META_ORDER.clear()
    perplexity_upload._PENDING_BATCH_CLIENT_IDS.clear()
    perplexity_upload._PENDING_BATCH_CLIENT_IDS_BY_SCOPE.clear()
    perplexity_upload._PENDING_BATCH_FIFO.clear()


class PerplexityS3HostTest(unittest.TestCase):
    def test_global_and_regional_s3_hosts(self) -> None:
        self.assertTrue(
            perplexity_upload.is_perplexity_s3_host(
                "ppl-ai-file-upload.s3.amazonaws.com"
            )
        )
        self.assertTrue(
            perplexity_upload.is_perplexity_s3_host(
                "ppl-ai-file-upload.s3.us-east-1.amazonaws.com"
            )
        )
        self.assertTrue(
            perplexity_upload.is_perplexity_s3_host(
                "ppl-ai-file-upload.s3.dualstack.us-east-1.amazonaws.com:443"
            )
        )
        self.assertFalse(perplexity_upload.is_perplexity_s3_host("s3.amazonaws.com"))
        self.assertFalse(perplexity_upload.is_perplexity_s3_host("www.perplexity.ai"))

    def test_policy_engine_maps_regional_s3_to_perplexity(self) -> None:
        self.assertEqual(
            ai_policy_engine._host_to_service_id(
                "ppl-ai-file-upload.s3.us-east-1.amazonaws.com"
            ),
            "perplexity",
        )
        req = {
            "meta": {
                "host": "ppl-ai-file-upload.s3.ap-northeast-2.amazonaws.com",
                "path": "/web/direct-files/x",
            }
        }
        self.assertEqual(
            ai_policy_engine.PolicyEngine._resolve_adapter_service_id(req, "generic"),
            "perplexity",
        )


class PerplexityUploadFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        fum._UPLOAD_SAVE_DIR = self.tempdir.name
        _reset_fum()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_legacy_results_logs_destination_host_and_saves_regional_put(self) -> None:
        batch_req = {
            "direction": "request",
            "request_id": "cd589a2e-1bb0-45d1-9a16-1929ea96f281-up-1",
            "content_type": "application/json",
            "meta": {
                "host": "www.perplexity.ai",
                "path": "/rest/uploads/batch_create_upload_urls",
                "pplx_visitor_id": "87bb2c1c-1c7a-42c0-a663-eef120051add",
            },
        }
        body = json.dumps(
            {
                "files": {
                    CLIENT_ID: {
                        "filename": "img_1786595188171.jpg",
                        "content_type": "image/jpeg",
                        "file_size": len(JPEG),
                    }
                }
            }
        ).encode()
        remembered = perplexity_upload.normalize_upload(batch_req, body)
        self.assertTrue(remembered and remembered.get("handled"))

        resp_req = {
            "direction": "response",
            "request_id": "cd589a2e-1bb0-45d1-9a16-1929ea96f281",
            "meta": {
                "host": "www.perplexity.ai",
                "path": "/rest/uploads/batch_create_upload_urls",
                "pplx_visitor_id": "87bb2c1c-1c7a-42c0-a663-eef120051add",
            },
        }
        s3_host = "ppl-ai-file-upload.s3.us-east-1.amazonaws.com"
        s3_url = (
            f"https://{s3_host}/web/direct-files/attachments/images/u/"
            f"{FILE_UUID}/img_1786595188171.jpg"
        )
        results = {
            CLIENT_ID: {
                "file_uuid": FILE_UUID,
                "s3_object_url": s3_url,
                "url": f"https://{s3_host}/",
                "fields": {
                    "key": (
                        f"web/direct-files/attachments/images/u/{FILE_UUID}/"
                        "img_1786595188171.jpg"
                    )
                },
            }
        }
        with mock.patch.object(perplexity_upload.logger, "info") as info:
            mapped = perplexity_upload.normalize_upload(
                resp_req, json.dumps({"results": results}).encode()
            )
        self.assertTrue(mapped and mapped.get("handled"))
        dest_logs = [
            " ".join(str(a) for a in call.args)
            for call in info.call_args_list
            if call.args and "upload destinations" in str(call.args[0])
        ]
        self.assertTrue(dest_logs)
        self.assertTrue(any(s3_host in row for row in dest_logs))

        put_req = {
            "direction": "request",
            "request_id": "s3-put-1",
            "meta": {
                "host": s3_host,
                "path": (
                    f"/web/direct-files/attachments/images/u/{FILE_UUID}/"
                    "img_1786595188171.jpg"
                ),
            },
        }
        saved = perplexity_upload.normalize_upload(put_req, JPEG)
        self.assertTrue(saved and saved.get("handled"))
        atts = perplexity_upload.build_policy_attachments(
            {"meta": {"host": "www.perplexity.ai", "path": "/rest/sse/perplexity_ask"}},
            {"params": {"attachments": [s3_url], "query_str": "이 이미지 설명해줘"}},
            b"",
        )
        self.assertEqual(len(atts), 1)
        self.assertTrue(str(atts[0].get("id") or "").endswith(FILE_UUID))
        self.assertEqual(atts[0].get("name"), "img_1786595188171.jpg")
        self.assertGreater(int(atts[0].get("size") or 0), 1000)

    def test_ask_fetches_s3_when_put_never_teed(self) -> None:
        """Decryptor-site Fast S3 PUT: ask URL is the only way to collect bytes."""
        batch_req = {
            "direction": "request",
            "request_id": "rid-fast-s3",
            "content_type": "application/json",
            "meta": {
                "host": "www.perplexity.ai",
                "path": "/rest/uploads/batch_create_upload_urls",
            },
        }
        body = json.dumps(
            {
                "files": {
                    CLIENT_ID: {
                        "filename": "img_1786595188171.jpg",
                        "content_type": "image/jpeg",
                        "file_size": len(JPEG),
                    }
                }
            }
        ).encode()
        self.assertTrue(perplexity_upload.normalize_upload(batch_req, body).get("handled"))

        s3_host = "ppl-ai-file-upload.s3.amazonaws.com"
        s3_url = (
            f"https://{s3_host}/web/direct-files/attachments/images/u/"
            f"{FILE_UUID}/img_1786595188171.jpg"
        )
        resp_req = {
            "direction": "response",
            "request_id": "rid-fast-s3",
            "meta": {
                "host": "www.perplexity.ai",
                "path": "/rest/uploads/batch_create_upload_urls",
            },
        }
        results = {
            CLIENT_ID: {
                "file_uuid": FILE_UUID,
                "s3_object_url": s3_url,
            }
        }
        perplexity_upload.normalize_upload(
            resp_req, json.dumps({"results": results}).encode()
        )

        class _Resp:
            def __init__(self) -> None:
                self._data = JPEG

            def read(self, n: int = -1) -> bytes:
                if n is None or n < 0:
                    return self._data
                return self._data[:n]

            def geturl(self) -> str:
                return s3_url

            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *args: object) -> None:
                return None

        with mock.patch("urllib.request.urlopen", return_value=_Resp()):
            atts = perplexity_upload.build_policy_attachments(
                {
                    "meta": {
                        "host": "www.perplexity.ai",
                        "path": "/rest/sse/perplexity_ask",
                    }
                },
                {
                    "params": {
                        "attachments": [s3_url],
                        "query_str": "이 이미지 설명해줘",
                    }
                },
                b"",
            )
        self.assertEqual(len(atts), 1)
        self.assertTrue(str(atts[0].get("id") or "").endswith(FILE_UUID))
        self.assertEqual(atts[0].get("name"), "img_1786595188171.jpg")
        self.assertGreater(int(atts[0].get("size") or 0), 1000)


if __name__ == "__main__":
    unittest.main()
