"""Unit tests for the DeepLogSink Protocol and impls."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from obscura.core import deep_log as dl_mod
from obscura.core.deep_log import (
    DeepLogger,
    DeepLogSink,
    JSONLSink,
    NullSink,
    StdoutSink,
    create_deep_log_sink,
)


def test_sink_classes_satisfy_protocol() -> None:
    assert isinstance(StdoutSink(), DeepLogSink)
    assert isinstance(NullSink(), DeepLogSink)
    # JSONLSink does not touch disk until first write, so safe to instantiate.
    assert isinstance(JSONLSink(), DeepLogSink)


def test_factory_default_is_jsonl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OBSCURA_DEEP_LOG_SINK", raising=False)
    assert isinstance(create_deep_log_sink(), JSONLSink)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("jsonl", JSONLSink),
        ("file", JSONLSink),
        ("stdout", StdoutSink),
        ("none", NullSink),
        ("null", NullSink),
        ("off", NullSink),
    ],
)
def test_factory_named_selection(name: str, expected: type) -> None:
    assert isinstance(create_deep_log_sink(name), expected)


def test_factory_unknown_name_falls_back_to_jsonl(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sink = create_deep_log_sink("totally-not-a-sink")
    assert isinstance(sink, JSONLSink)
    assert any("unknown" in rec.message.lower() for rec in caplog.records)


def test_jsonl_round_trip(tmp_path: Path) -> None:
    sink = JSONLSink(log_dir=tmp_path)
    sink.write({"type": "test", "data": {"msg": "hello"}})
    sink.write({"type": "test", "data": {"msg": "world"}})
    sink.close()

    lines = (tmp_path / "deep.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"type": "test", "data": {"msg": "hello"}}
    assert json.loads(lines[1]) == {"type": "test", "data": {"msg": "world"}}


def test_jsonl_rotation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Rotation only fires when the sink (re)opens the file; force a tiny
    # max-size and reopen via close+write to trigger a rollover.
    monkeypatch.setattr(dl_mod, "_MAX_LOG_SIZE", 50)
    sink = JSONLSink(log_dir=tmp_path)
    for i in range(10):
        sink.write({"type": "test", "i": i})
    sink.close()
    # File should now exceed the size threshold.
    assert (tmp_path / "deep.jsonl").stat().st_size >= 50

    # Reopen — this triggers rotation, sending current → deep.1.jsonl.
    sink2 = JSONLSink(log_dir=tmp_path)
    sink2.write({"type": "after-rotate"})
    sink2.close()

    assert (tmp_path / "deep.jsonl").exists()
    assert (tmp_path / "deep.1.jsonl").exists()


def test_stdout_sink_writes_to_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    sink = StdoutSink()
    sink.write({"type": "stdout-test", "n": 1})
    assert json.loads(buf.getvalue().strip()) == {"type": "stdout-test", "n": 1}


def test_null_sink_discards() -> None:
    sink = NullSink()
    sink.write({"anything": "goes"})
    sink.close()
    assert sink.description() == "null"


def test_deeplogger_buffers_and_flushes() -> None:
    sink = NullSink()
    logger = DeepLogger(enabled=True, sink=sink)
    logger.tool_call("foo", {"a": 1})
    assert logger.total_entries == 1
    logger.flush()  # idempotent w/ NullSink


def test_deeplogger_disabled_drops_writes() -> None:
    sink = NullSink()
    logger = DeepLogger(enabled=False, sink=sink)
    logger.tool_call("foo")
    logger.api_request("model")
    assert logger.total_entries == 0


def test_deeplogger_log_path_reports_sink_description(
    tmp_path: Path,
) -> None:
    sink = JSONLSink(log_dir=tmp_path)
    logger = DeepLogger(enabled=True, sink=sink)
    assert logger.log_path == str(tmp_path / "deep.jsonl")

    stdout_logger = DeepLogger(enabled=True, sink=StdoutSink())
    assert stdout_logger.log_path == "stdout"
