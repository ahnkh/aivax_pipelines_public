#!/usr/bin/env python3
"""Perplexity output must come from structural variant=answer / final SSE only."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "core") not in sys.path:
    sys.path.insert(0, str(_ROOT / "core"))

from adapters import perplexity_engine as pplx  # noqa: E402


PLANNER = (
    "The user seems to be having fun with the Kim Jennie (BLACKPINK member) "
    "image they uploaded, saying \"Jennie, let's go home!!! Let's go have a beer!\" "
    "This is a playful, fan-style message.\n\n"
    "Let me look at the image first to understand what they're working with."
)

ANSWER = "제니랑 맥주 한잔 각이네요! 사진 분위기 살려서 답할게요."


def _sse(obj: dict) -> str:
    return "event: message\ndata: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _workflow_item(text: str, *, variant: str | None = None, item_type: str = "WORKFLOW_ITEM_TEXT") -> dict:
    item: dict = {
        "id": "i1",
        "type": item_type,
        "payload": {"text_payload": {"text": text}},
    }
    if variant is not None:
        item["variant"] = variant
        item["display_mode"] = "WORKFLOW_ITEM_ALWAYS_EXPAND"
    return item


def _workflow_event(text: str, *, variant: str | None = None, final: bool = False) -> dict:
    obj: dict = {
        "blocks": [
            {
                "intended_usage": "workflow_root",
                "workflow_block": {
                    "steps": [
                        {
                            "id": "s1",
                            "title": "",
                            "tool_name": "",
                            "status": "WORKFLOW_COMPLETED" if final else "WORKFLOW_RUNNING",
                            "items": [_workflow_item(text, variant=variant)],
                        }
                    ]
                },
            }
        ]
    }
    if final:
        obj["final_sse_message"] = True
        obj["text_completed"] = True
        obj["status"] = "COMPLETED"
        obj["final"] = True
    return obj


def _ask_req() -> dict:
    return {
        "direction": "response",
        "unit_type": "sse_event",
        "content_type": "text/event-stream",
        "meta": {"host": "www.perplexity.ai", "path": "/rest/sse/perplexity_ask"},
    }


class TestPerplexityModelPreference(unittest.TestCase):
    def test_model_preference_is_stored_as_sent(self) -> None:
        req = {
            "direction": "request",
            "unit_type": "json_body",
            "content_type": "application/json",
            "meta": {"host": "www.perplexity.ai", "path": "/rest/sse/perplexity_ask"},
        }
        payload = json.dumps(
            {
                "query_str": "Hello World!",
                "params": {"mode": "concise", "model_preference": "pplx_pro"},
            }
        ).encode()
        self.assertEqual(pplx.extract(req, payload), "Hello World!")
        self.assertEqual(req.get("_perplexity_model"), "pplx_pro")
        from core import ai_policy_engine

        pol: dict = {}
        ai_policy_engine.stamp_policy_model(req, payload, pol, pplx)
        self.assertEqual(pol.get("model"), "pplx_pro")


class TestPerplexityStructuralAnswer(unittest.TestCase):
    def test_ignores_planner_text_without_variant_answer(self) -> None:
        req = _ask_req()
        payload = _sse(_workflow_event(PLANNER, variant=None, final=True)).encode("utf-8")
        out = pplx.extract_full_body(req, payload)
        self.assertIn(out, ("", None))
        self.assertFalse(req.get("_extract_final"))

    def test_takes_variant_answer_on_final(self) -> None:
        req = _ask_req()
        payload = (
            _sse(_workflow_event(PLANNER, variant=None, final=False))
            + _sse(_workflow_event(ANSWER, variant="answer", final=True))
        ).encode("utf-8")
        out = pplx.extract_full_body(req, payload)
        self.assertEqual(out, ANSWER)
        self.assertTrue(req.get("_extract_final"))

    def test_skips_queries_item_type(self) -> None:
        block = {
            "workflow_block": {
                "steps": [
                    {
                        "items": [
                            _workflow_item("search query", variant="answer", item_type="WORKFLOW_ITEM_QUERIES")
                        ]
                    }
                ]
            }
        }
        self.assertEqual(pplx._workflow_answer_from_block(block), "")

    def test_live_dump_still_extracts(self) -> None:
        dump = Path(
            "/home1/aivax/inline_engine/aivax/evidence/PROVE/I350_BYPASS_1G/"
            "perplexity_ask_sse_empty.dump"
        )
        if not dump.is_file():
            self.skipTest("dump missing")
        req = _ask_req()
        out = pplx.extract_full_body(req, dump.read_bytes())
        self.assertTrue(out and "보고하는 게 핵심" in out)
        self.assertTrue(req.get("_extract_final"))


if __name__ == "__main__":
    unittest.main()
