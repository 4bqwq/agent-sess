from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from sess.errors import ProviderError
from sess.providers.common import iter_jsonl_snapshot_reverse


TRANSCRIPT_PATH = Path("transcript.jsonl")


def jsonl_bytes(events: list[object], *, trailing_newline: bool = True) -> bytes:
    text = "\n".join(json.dumps(event, ensure_ascii=False) for event in events)
    return (text + ("\n" if trailing_newline else "")).encode("utf-8")


def read_reverse(data: bytes) -> tuple[list[dict[str, object]], list[str]]:
    warnings: list[str] = []
    events = list(
        iter_jsonl_snapshot_reverse(
            io.BytesIO(data), len(data), warnings, "Codex", TRANSCRIPT_PATH
        )
    )
    return events, warnings


@pytest.mark.parametrize("trailing_newline", [True, False])
def test_reverse_snapshot_yields_records_backwards(trailing_newline):
    events = [{"timestamp": "first"}, {"timestamp": "second"}, {"timestamp": "last"}]

    result, warnings = read_reverse(
        jsonl_bytes(events, trailing_newline=trailing_newline)
    )

    assert result == list(reversed(events))
    assert warnings == []


@pytest.mark.parametrize("data", [b"", b"\n", b" \n\t\n\r\n"])
def test_reverse_snapshot_skips_empty_and_whitespace_lines(data):
    assert read_reverse(data) == ([], [])


def test_reverse_snapshot_skips_nonobjects_but_keeps_objects_without_timestamp():
    events = [{"timestamp": "first"}, None, ["array"], "string", 42, {}, {"type": "other"}]

    result, warnings = read_reverse(jsonl_bytes(events))

    assert result == [{"type": "other"}, {}, {"timestamp": "first"}]
    assert warnings == []


@pytest.mark.parametrize("trailing_newline", [True, False])
def test_reverse_snapshot_joins_long_utf8_lines_before_decoding(trailing_newline):
    events = [
        {"timestamp": "first"},
        {"timestamp": "long", "content": "深度模块🧪" * 6000},
        {"timestamp": "last"},
    ]

    result, warnings = read_reverse(
        jsonl_bytes(events, trailing_newline=trailing_newline)
    )

    assert result == list(reversed(events))
    assert warnings == []


@pytest.mark.parametrize("length", [8140, 8190, 8191, 8192, 8193, 16384])
def test_reverse_snapshot_handles_lines_near_block_boundaries(length):
    events = [{"first": True}, {"content": "x" * length}, {"last": True}]

    result, warnings = read_reverse(jsonl_bytes(events))

    assert result == list(reversed(events))
    assert warnings == []


def test_reverse_snapshot_limits_reads_to_eight_kibibytes():
    class RecordingHandle(io.BytesIO):
        def __init__(self, data):
            super().__init__(data)
            self.read_sizes: list[int] = []

        def read(self, size=-1):
            self.read_sizes.append(size)
            return super().read(size)

    events = [{"content": "x" * 100000}, {"timestamp": "last"}]
    data = jsonl_bytes(events)
    handle = RecordingHandle(data)
    warnings: list[str] = []

    result = list(
        iter_jsonl_snapshot_reverse(
            handle, len(data), warnings, "Codex", TRANSCRIPT_PATH
        )
    )

    assert result == list(reversed(events))
    assert all(0 < size <= 8192 for size in handle.read_sizes)
    assert len(handle.read_sizes) > 10
    assert warnings == []
    assert not handle.closed


def test_reverse_snapshot_stops_reading_when_caller_finds_latest_record():
    class RecordingHandle(io.BytesIO):
        def __init__(self, data):
            super().__init__(data)
            self.read_count = 0

        def read(self, size=-1):
            self.read_count += 1
            return super().read(size)

    data = b"{old-invalid\n" * 3000 + jsonl_bytes([{"timestamp": "last"}])
    handle = RecordingHandle(data)
    warnings: list[str] = []
    events = iter_jsonl_snapshot_reverse(
        handle, len(data), warnings, "Codex", TRANSCRIPT_PATH
    )

    assert next(events) == {"timestamp": "last"}
    assert handle.read_count == 1
    assert warnings == []


def test_reverse_snapshot_reports_bad_json_with_byte_offset():
    first = jsonl_bytes([{"content": "中文"}])
    data = first + b"{bad\n" + jsonl_bytes([{"timestamp": "last"}])

    result, warnings = read_reverse(data)

    assert result == [{"timestamp": "last"}, {"content": "中文"}]
    assert len(warnings) == 1
    assert "无效 JSON 行" in warnings[0]
    assert f"字节偏移 {len(first)}" in warnings[0]


def test_reverse_snapshot_skips_unfinished_tail_and_keeps_previous_record():
    prefix = jsonl_bytes([{"timestamp": "complete"}])

    result, warnings = read_reverse(prefix + b'{"timestamp": "unfinished')

    assert result == [{"timestamp": "complete"}]
    assert len(warnings) == 1
    assert "尚未完整写入的末行" in warnings[0]
    assert f"字节偏移 {len(prefix)}" in warnings[0]


def test_reverse_snapshot_reports_newline_terminated_bad_tail_as_invalid_json():
    data = jsonl_bytes([{"timestamp": "complete"}]) + b'{"timestamp": "bad\n'

    result, warnings = read_reverse(data)

    assert result == [{"timestamp": "complete"}]
    assert len(warnings) == 1
    assert "无效 JSON 行" in warnings[0]
    assert "尚未完整写入" not in warnings[0]


def test_reverse_snapshot_skips_invalid_utf8_line():
    data = jsonl_bytes([{"timestamp": "first"}])
    data += b'{"content": "\xff"}\n'
    data += jsonl_bytes([{"timestamp": "last"}])

    result, warnings = read_reverse(data)

    assert result == [{"timestamp": "last"}, {"timestamp": "first"}]
    assert len(warnings) == 1
    assert "无效 UTF-8 行" in warnings[0]


def test_reverse_snapshot_skips_invalid_utf8_in_long_line():
    data = jsonl_bytes([{"timestamp": "first"}])
    data += b'{"content": "' + b"x" * 20000 + b'\xff"}\n'

    result, warnings = read_reverse(data)

    assert result == [{"timestamp": "first"}]
    assert len(warnings) == 1
    assert "无效 UTF-8 行" in warnings[0]


@pytest.mark.parametrize("trailing_newline", [True, False])
def test_reverse_snapshot_does_not_read_appended_records(trailing_newline):
    original = jsonl_bytes([{"timestamp": "original"}], trailing_newline=trailing_newline)
    handle = io.BytesIO(original)
    snapshot_size = len(original)
    handle.seek(0, io.SEEK_END)
    handle.write(b"\n" + jsonl_bytes([{"timestamp": "appended"}]))
    warnings: list[str] = []

    result = list(
        iter_jsonl_snapshot_reverse(
            handle, snapshot_size, warnings, "Codex", TRANSCRIPT_PATH
        )
    )

    assert result == [{"timestamp": "original"}]
    assert warnings == []


def test_reverse_snapshot_repositions_same_open_handle_after_forward_read():
    events = [{"timestamp": "first"}, {"timestamp": "last"}]
    data = jsonl_bytes(events)
    handle = io.BytesIO(data)
    assert handle.readline()
    warnings: list[str] = []

    result = list(
        iter_jsonl_snapshot_reverse(
            handle, len(data), warnings, "Codex", TRANSCRIPT_PATH
        )
    )

    assert result == list(reversed(events))
    assert warnings == []


def test_reverse_snapshot_fails_if_file_is_shorter_than_snapshot():
    original = jsonl_bytes([{"timestamp": "original"}])
    handle = io.BytesIO(original)
    snapshot_size = len(original)
    handle.truncate(5)

    with pytest.raises(ProviderError, match="截断或读取不完整"):
        list(
            iter_jsonl_snapshot_reverse(
                handle, snapshot_size, [], "Codex", TRANSCRIPT_PATH
            )
        )


def test_reverse_snapshot_fails_on_short_read():
    class ShortReadHandle(io.BytesIO):
        def read(self, size=-1):
            return super().read(max(0, size - 1))

    data = jsonl_bytes([{"timestamp": "original"}])

    with pytest.raises(ProviderError, match="截断或读取不完整"):
        list(
            iter_jsonl_snapshot_reverse(
                ShortReadHandle(data), len(data), [], "Codex", TRANSCRIPT_PATH
            )
        )


def test_reverse_snapshot_fails_if_file_is_truncated_between_blocks():
    class TruncatingHandle(io.BytesIO):
        def read(self, size=-1):
            result = super().read(size)
            self.truncate(1)
            return result

    data = jsonl_bytes([{"content": "x" * 20000}, {"timestamp": "last"}])
    handle = TruncatingHandle(data)
    events = iter_jsonl_snapshot_reverse(
        handle, len(data), [], "Codex", TRANSCRIPT_PATH
    )
    assert next(events) == {"timestamp": "last"}

    with pytest.raises(ProviderError, match="截断或读取不完整"):
        list(events)


def test_reverse_snapshot_leaves_io_errors_for_caller():
    class FailedSeekHandle(io.BytesIO):
        def seek(self, offset, whence=io.SEEK_SET):
            raise OSError("seek failed")

    data = jsonl_bytes([{"timestamp": "original"}])

    with pytest.raises(OSError, match="seek failed"):
        list(
            iter_jsonl_snapshot_reverse(
                FailedSeekHandle(data), len(data), [], "Codex", TRANSCRIPT_PATH
            )
        )
