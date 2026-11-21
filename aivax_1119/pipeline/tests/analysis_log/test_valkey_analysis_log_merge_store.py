import orjson
from src.adapters.analysis_log_merge.valkey_analysis_log_merge_store import (
    ValkeyAnalysisLogMergeStore,
)
from src.domain.analysis_log_merge.models import AnalysisLogMessageKey


class _FakeValkey:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.registered_scripts = []

    def register_script(self, script):
        self.registered_scripts.append(script)

        def execute(*, keys, args):
            self.calls.append((script, keys, args))
            return self.results.pop(0)

        return execute


def _store(client):
    return ValkeyAnalysisLogMergeStore(
        client,
        key_prefix="aivax:test:inout_pending:",
        ttl_seconds=2_400,
        input_flush_after_seconds=1_800,
    )


def test_set_input_returns_none_when_output_does_not_exist():
    client = _FakeValkey([[0, False]])
    store = _store(client)

    popped_output = store.set_input_and_pop_output(
        AnalysisLogMessageKey("message-1"),
        {"prompt": "hello"},
    )

    assert popped_output is None
    assert len(client.registered_scripts) == 5
    assert len(client.calls[0][1]) == 6
    assert client.calls[0][1][4] == "aivax:test:inout_pending:outlate:message-1"
    assert client.calls[0][1][5] == "aivax:test:inout_pending:flushed:message-1"
    assert client.calls[0][2][0] == orjson.dumps({"prompt": "hello"})
    assert isinstance(client.calls[0][2][0], bytes)


def test_park_late_output_keeps_output_for_ttl_outside_expiry_index():
    client = _FakeValkey([])
    stored = []
    client.set = lambda key, value, *, ex: stored.append((key, value, ex))
    store = _store(client)

    store.park_late_output(
        AnalysisLogMessageKey("message-1"),
        {"output_text": "늦은 응답"},
    )

    assert stored == [
        (
            "aivax:test:inout_pending:outlate:message-1",
            orjson.dumps({"output_text": "늦은 응답"}),
            2_400,
        )
    ]


def test_resolve_unmatched_output_reports_each_store_outcome():
    parked = _FakeValkey([[0, False]])
    parked_store = _store(parked)
    parked_resolution = parked_store.resolve_unmatched_output(
        AnalysisLogMessageKey("message-1"),
        {"output_text": "응답"},
    )
    assert parked_resolution.action == "park"
    assert parked_resolution.input_log is None
    assert parked.calls[0][1][2] == "aivax:test:inout_pending:flushed:message-1"
    assert "SET" in parked.registered_scripts[3]
    assert "EXISTS" in parked.registered_scripts[3]

    merged = _FakeValkey([[1, orjson.dumps({"prompt": "질문"})]])
    merged_resolution = _store(merged).resolve_unmatched_output(
        AnalysisLogMessageKey("message-1"),
        {"output_text": "응답"},
    )
    assert merged_resolution.action == "merge"
    assert merged_resolution.input_log == {"prompt": "질문"}

    revised = _FakeValkey([[2, False]])
    revised_resolution = _store(revised).resolve_unmatched_output(
        AnalysisLogMessageKey("message-1"),
        {"output_text": "응답"},
    )
    assert revised_resolution.action == "revise"
    assert revised_resolution.input_log is None


def test_pop_input_returns_deserialized_input_when_input_exists():
    client = _FakeValkey([[1, orjson.dumps({"prompt": "hello"})]])
    store = _store(client)

    popped_input = store.pop_input_or_set_output(
        AnalysisLogMessageKey("message-1"),
        {"output_text": "world"},
    )

    assert popped_input == {"prompt": "hello"}
    assert len(client.registered_scripts) == 5
    assert len(client.calls[0][1]) == 5
    assert client.calls[0][2][0] == orjson.dumps({"output_text": "world"})
    assert isinstance(client.calls[0][2][0], bytes)
