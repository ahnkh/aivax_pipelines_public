"""OpenSearch 색인 준비 중에도 통합 테스트가 문서 조회를 기다리는지 검증한다."""

import runpy
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

PipelineStack = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "integration/conftest.py")
)["PipelineStack"]


@pytest.fixture
def stack(tmp_path, monkeypatch):
    elapsed = 0.0

    def sleep(seconds):
        nonlocal elapsed
        elapsed += seconds

    monkeypatch.setitem(
        PipelineStack.indexed_document.__globals__,
        "time",
        SimpleNamespace(monotonic=lambda: elapsed, sleep=sleep),
    )
    clients = []

    def create(handler):
        search = httpx.Client(
            base_url="https://opensearch.test",
            transport=httpx.MockTransport(handler),
        )
        clients.append(search)
        return PipelineStack(None, search, tmp_path, tmp_path, None, (), None)

    yield create
    for client in clients:
        client.close()


def test_document_lookup_waits_for_index_and_shards(stack):
    hit = {"_id": "message-1", "_source": {"user_consent": "on"}}
    responses = iter(
        [
            httpx.Response(404, json={"error": "index_not_found_exception"}),
            httpx.Response(503, json={"error": "all shards failed"}),
            httpx.Response(200, json={"hits": {"hits": []}}),
            httpx.Response(
                200,
                json={"hits": {"hits": [{"_id": "message-1", "_source": {}}]}},
            ),
            httpx.Response(200, json={"hits": {"hits": [hit]}}),
        ]
    )
    requests = []

    def search(request):
        requests.append(request)
        return next(responses)

    result = stack(search).indexed_document(
        "message-1", expected_fields={"user_consent": "on"}
    )

    assert result == hit
    assert len(requests) == 5


@pytest.mark.parametrize("status", [400, 401, 403, 500])
def test_document_lookup_does_not_retry_other_http_errors(stack, status):
    requests = []

    def search(request):
        requests.append(request)
        return httpx.Response(status, json={"error": "unexpected failure"})

    with pytest.raises(httpx.HTTPStatusError) as caught:
        stack(search).indexed_document("message-1")

    assert caught.value.response.status_code == status
    assert len(requests) == 1


def test_document_lookup_still_times_out_when_shards_never_start(stack):
    with pytest.raises(
        AssertionError, match="Timed out waiting for OpenSearch document"
    ):
        stack(
            lambda request: httpx.Response(503, json={"error": "all shards failed"})
        ).indexed_document("message-1")
