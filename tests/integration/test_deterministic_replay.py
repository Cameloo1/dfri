from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from dfri.replay import ReplayError, replay, tree_hash


def test_frozen_replay_is_byte_identical_in_disposable_directories(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_hash = replay(as_of=date(2026, 5, 31), output=first)
    second_hash = replay(as_of=date(2026, 5, 31), output=second)

    assert first_hash == second_hash
    assert tree_hash(first) == tree_hash(second)
    assert first_hash == "1215d458ce5cbea757c5fda599800fa96aba13b8ae5d248c5b3db1e3dba90faf"
    assert replay(as_of=date(2026, 5, 31), output=first) == first_hash


def test_replay_is_point_in_time_and_append_only(tmp_path: Path) -> None:
    output = tmp_path / "publication"
    replay(as_of=date(2026, 4, 30), output=output)
    assert b"2026-05-31" not in (output / "observations.json").read_bytes()

    (output / "manifest.json").write_text("changed", encoding="utf-8")
    with pytest.raises(ReplayError, match="collision"):
        replay(as_of=date(2026, 4, 30), output=output)


def test_replay_rejects_file_destination(tmp_path: Path) -> None:
    output = tmp_path / "publication"
    output.write_text("occupied", encoding="utf-8")
    with pytest.raises(ReplayError, match="not a directory"):
        replay(as_of=date(2026, 5, 31), output=output)
