from src.adapters.analysis_log_merge import MemoryAnalysisLogMergeStore
from src.domain.analysis_log_merge.models import AnalysisLogMessageKey


def test_input_then_output_pops_the_saved_input_log():
    store = MemoryAnalysisLogMergeStore()
    key = AnalysisLogMessageKey("message-1")

    popped_output = store.set_input_and_pop_output(key, {"prompt": "hello"})
    popped_input = store.pop_input_or_set_output(
        key,
        {"output_text": "world"},
    )

    assert popped_output is None
    assert popped_input == {"prompt": "hello"}


def test_output_then_input_pops_the_saved_output_log():
    store = MemoryAnalysisLogMergeStore()
    key = AnalysisLogMessageKey("message-1")

    popped_input = store.pop_input_or_set_output(
        key,
        {"output_text": "world"},
    )
    popped_output = store.set_input_and_pop_output(key, {"prompt": "hello"})

    assert popped_input is None
    assert popped_output == {"output_text": "world"}
