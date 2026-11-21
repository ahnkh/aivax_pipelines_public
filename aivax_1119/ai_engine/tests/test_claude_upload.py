#!/usr/bin/env python3
"""Claude upload metadata remains available across SHM worker processes."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
for _path in (_ROOT, _ROOT / "core", _ROOT / "adapters"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from adapters import claude_upload  # noqa: E402

fum = claude_upload._fum


class TestClaudeSharedUploadMetadata(unittest.TestCase):
    def setUp(self) -> None:
        self._old_dir = fum._UPLOAD_SAVE_DIR
        self._tmp = tempfile.TemporaryDirectory()
        fum._UPLOAD_SAVE_DIR = self._tmp.name
        self._clear_memory()

    def tearDown(self) -> None:
        self._clear_memory()
        fum._UPLOAD_SAVE_DIR = self._old_dir
        self._tmp.cleanup()

    @staticmethod
    def _clear_memory() -> None:
        fum._upload_id_map.clear()
        fum._saved_file_paths.clear()
        fum._saved_file_sizes.clear()
        fum._upload_expiry_heap.clear()
        claude_upload._CLAUDE_FILE_UUID_INDEX.clear()
        claude_upload._CLAUDE_FILE_UUID_ORDER.clear()

    def test_three_filenames_survive_worker_memory_reset(self) -> None:
        rows = [
            ("084150ae-4318-4e4c-ac1b-3993d476b5a6", "첫번째.png"),
            ("f8e60cb0-923a-4de5-aff8-62abc4c8b387", "두번째.png"),
            ("92ac1f8e-6cf7-44cf-ab2f-7a5f52159bd5", "세번째.png"),
        ]
        for uid, name in rows:
            self.assertTrue(fum.register_upload_mapping(uid, name, {}))
            self.assertTrue(fum.append_upload_payload(uid, b"image-bytes", {}))

        # A completion request is commonly handled by another SHM worker.
        self._clear_memory()

        req = {
            "direction": "request",
            "meta": {
                "host": "claude.ai",
                "path": (
                    "/api/organizations/11111111-1111-1111-1111-111111111111/"
                    "chat_conversations/22222222-2222-2222-2222-222222222222/completion"
                ),
            },
        }
        attachments = claude_upload.build_policy_attachments(
            req,
            {"files": [uid for uid, _ in rows]},
            b"{}",
        )
        self.assertEqual(len(attachments), 3)
        self.assertEqual([row["name"] for row in attachments], [name for _, name in rows])
        self.assertTrue(all(row["id"].startswith(self._tmp.name) for row in attachments))


if __name__ == "__main__":
    unittest.main()
