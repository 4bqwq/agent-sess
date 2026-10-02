from __future__ import annotations

import sys

import pytest

from sess.errors import ProviderError
from sess.process import NativeProcessRunner, ProjectLaunchLock


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


def test_native_process_can_be_observed_before_it_exits(tmp_path):
    marker = tmp_path / "session-created"
    script = (
        "import pathlib, sys, time; "
        "time.sleep(0.05); "
        "pathlib.Path(sys.argv[1]).touch(); "
        "time.sleep(0.15); "
        "raise SystemExit(7)"
    )
    observations = []

    def observe():
        exists = marker.exists()
        observations.append(exists)
        return exists

    return_code = NativeProcessRunner(observation_interval=0.01).run_observed(
        [sys.executable, "-c", script, str(marker)], observe
    )

    assert return_code == 7
    assert observations[-1] is True


def test_project_launch_lock_rejects_a_second_unbound_launch(tmp_path):
    first = ProjectLaunchLock(tmp_path)
    second = ProjectLaunchLock(tmp_path)
    first.acquire()
    try:
        with pytest.raises(ProviderError, match="尚未生成 transcript"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()
