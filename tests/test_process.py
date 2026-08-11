from __future__ import annotations

import sys

from sess.process import NativeProcessRunner


def test_native_process_inherits_environment_and_exit_code(tmp_path, monkeypatch):
    output = tmp_path / "environment.txt"
    monkeypatch.setenv("SESS_TEST_INHERITED", "inherited-value")
    script = (
        "import os, pathlib, sys; "
        "pathlib.Path(sys.argv[1]).write_text(os.environ['SESS_TEST_INHERITED']); "
        "raise SystemExit(7)"
    )

    return_code = NativeProcessRunner().run(
        [sys.executable, "-c", script, str(output)]
    )

    assert return_code == 7
    assert output.read_text(encoding="utf-8") == "inherited-value"

