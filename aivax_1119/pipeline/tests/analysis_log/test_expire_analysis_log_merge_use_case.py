"""의존성 분리 후에도 inline 만료 처리 정책을 보존한다."""

from unittest.mock import Mock, patch

from src.adapters.analysis_log_merge.memory_analysis_log_merge_store import (
    MemoryAnalysisLogMergeStore,
)
from src.domain.analysis_log_merge.models.analysis_log_message_key import (
    AnalysisLogMessageKey,
)
from src.application.use_cases.expire_analysis_log_merge import (
    ExpireAnalysisLogMergeUseCase,
)


def test_expiry_delivers_input_only_and_revises_output_after_prompt_flush(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(
        "src.adapters.analysis_log_merge.memory_analysis_log_merge_store.time.monotonic",
        lambda: now[0],
    )
    store = MemoryAnalysisLogMergeStore(ttl_seconds=60, input_flush_after_seconds=10)
    key = AnalysisLogMessageKey("message")
    store.set_input_and_pop_output(key, {"prompt": "질문"})
    merge_service = Mock()
    merge_service.deliver.return_value = 1
    expiry = ExpireAnalysisLogMergeUseCase(store, merge_service)
    expiry.execute()
    merge_service.deliver.assert_not_called()
    now[0] = 11
    expiry.execute()
    merge_service.deliver.assert_called_once_with(
        {"prompt": "질문", "merge_state": "input_only", "output_text": ""}
    )
    # 프롬프트 문서가 적재된 뒤에 도착한 응답만 본문 갱신으로 붙인다.
    store.pop_input_or_set_output(key, {"output_text": "응답"})
    now[0] = 22
    expiry.execute()
    merge_service.revise_output.assert_called_once_with(
        "message", {"output_text": "응답"}
    )
    expiry.execute()
    assert merge_service.deliver.call_count == 1
    assert merge_service.revise_output.call_count == 1


def test_input_finished_after_flush_window_merges_with_parked_output(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(
        "src.adapters.analysis_log_merge.memory_analysis_log_merge_store.time.monotonic",
        lambda: now[0],
    )
    store = MemoryAnalysisLogMergeStore(ttl_seconds=60, input_flush_after_seconds=10)
    key = AnalysisLogMessageKey("slow-input")
    store.pop_input_or_set_output(key, {"output_text": "응답"})
    merge_service = Mock()
    expiry = ExpireAnalysisLogMergeUseCase(store, merge_service)

    now[0] = 11
    expiry.execute()
    # 아직 적재된 프롬프트가 없으므로 갱신 파일을 만들지 않고 보관만 한다.
    merge_service.revise_output.assert_not_called()
    merge_service.deliver.assert_not_called()

    # 입력 검사가 flush 기한을 넘겨 끝나도 보관된 응답과 바로 병합한다.
    now[0] = 40
    assert store.set_input_and_pop_output(key, {"prompt": "질문"}) == {
        "output_text": "응답"
    }
    now[0] = 55
    expiry.execute()
    merge_service.deliver.assert_not_called()


def test_resolve_merges_output_with_input_still_in_the_store(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(
        "src.adapters.analysis_log_merge.memory_analysis_log_merge_store.time.monotonic",
        lambda: now[0],
    )
    store = MemoryAnalysisLogMergeStore(ttl_seconds=200, input_flush_after_seconds=100)
    key = AnalysisLogMessageKey("waiting")
    store.set_input_and_pop_output(key, {"prompt": "질문"})

    resolution = store.resolve_unmatched_output(key, {"output_text": "응답"})

    assert resolution.action == "merge"
    assert resolution.input_log == {"prompt": "질문"}
    now[0] = 101
    assert store.pop_expired_inputs() == []


def test_flushed_input_recovers_output_that_was_parked_directly(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(
        "src.adapters.analysis_log_merge.memory_analysis_log_merge_store.time.monotonic",
        lambda: now[0],
    )
    store = MemoryAnalysisLogMergeStore(ttl_seconds=60, input_flush_after_seconds=10)
    key = AnalysisLogMessageKey("raced")
    store.set_input_and_pop_output(key, {"prompt": "질문"})
    store.park_late_output(key, {"output_text": "응답"})
    merge_service = Mock()
    expiry = ExpireAnalysisLogMergeUseCase(store, merge_service)

    now[0] = 11
    expiry.execute()

    delivered = merge_service.deliver.call_args.args[0]
    assert delivered["prompt"] == "질문"
    assert delivered["output_text"] == "응답"
    assert "merge_state" not in delivered
    merge_service.revise_output.assert_not_called()


def test_parked_output_outlives_the_merge_ttl(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(
        "src.adapters.analysis_log_merge.memory_analysis_log_merge_store.time.monotonic",
        lambda: now[0],
    )
    store = MemoryAnalysisLogMergeStore(
        ttl_seconds=60,
        input_flush_after_seconds=10,
        late_output_ttl_seconds=120,
    )
    key = AnalysisLogMessageKey("slow")
    store.park_late_output(key, {"output_text": "응답"})

    now[0] = 90
    assert store.set_input_and_pop_output(key, {"prompt": "질문"}) == {
        "output_text": "응답"
    }


def test_parked_output_expires_after_ttl(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(
        "src.adapters.analysis_log_merge.memory_analysis_log_merge_store.time.monotonic",
        lambda: now[0],
    )
    store = MemoryAnalysisLogMergeStore(ttl_seconds=60, input_flush_after_seconds=10)
    key = AnalysisLogMessageKey("gone")
    store.park_late_output(key, {"output_text": "응답"})

    now[0] = 61
    assert store.set_input_and_pop_output(key, {"prompt": "질문"}) is None


def test_expiry_discards_unmatched_output_without_response_text(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(
        "src.adapters.analysis_log_merge.memory_analysis_log_merge_store.time.monotonic",
        lambda: now[0],
    )
    store = MemoryAnalysisLogMergeStore(ttl_seconds=60, input_flush_after_seconds=10)
    store.pop_input_or_set_output(
        AnalysisLogMessageKey("output"), {"output_text": "   "}
    )
    merge_service = Mock()
    expiry = ExpireAnalysisLogMergeUseCase(store, merge_service)
    now[0] = 11
    expiry.execute()
    merge_service.revise_output.assert_not_called()
    merge_service.deliver.assert_not_called()


def test_use_case_does_not_own_scheduling():
    with patch("threading.Thread") as thread:
        use_case = ExpireAnalysisLogMergeUseCase(
            MemoryAnalysisLogMergeStore(),
            Mock(),
        )
        use_case.execute()
    thread.assert_not_called()
    assert not hasattr(use_case, "Start")
    assert not hasattr(use_case, "Shutdown")
