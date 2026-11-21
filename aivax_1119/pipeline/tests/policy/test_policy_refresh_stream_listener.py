from __future__ import annotations

import threading
from unittest.mock import Mock

from src.schedulers.policy_refresh_stream_listener import (
    PolicyRefreshStreamListener,
)


def test_prepare_starts_after_current_stream_tail() -> None:
    client = Mock()
    client.xrevrange.return_value = [(b"1740000000000-7", {})]
    listener = PolicyRefreshStreamListener(
        client, "pipeline:policy:refresh:v1", lambda: None
    )

    listener.prepare()

    assert listener._last_id == "1740000000000-7"


def test_each_listener_reads_the_same_refresh_event() -> None:
    completed = threading.Event()
    refreshes: list[str] = []

    def client_for(worker: str) -> Mock:
        client = Mock()
        calls = 0

        def xread(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return [
                    (
                        "pipeline:policy:refresh:v1",
                        [("1-0", {"source": "management"})],
                    )
                ]
            completed.wait(0.01)
            return []

        client.xread.side_effect = xread
        return client

    def refresh(worker: str) -> None:
        refreshes.append(worker)
        if len(refreshes) == 2:
            completed.set()

    first = PolicyRefreshStreamListener(
        client_for("one"), "pipeline:policy:refresh:v1", lambda: refresh("one")
    )
    second = PolicyRefreshStreamListener(
        client_for("two"), "pipeline:policy:refresh:v1", lambda: refresh("two")
    )
    first.start()
    second.start()
    assert completed.wait(1)

    assert first.shutdown()
    assert second.shutdown()
    assert sorted(refreshes) == ["one", "two"]
