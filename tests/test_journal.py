from pathlib import Path

import pytest

from aegentra import (
    JournalInUse,
    Runtime,
)


def test_second_runtime_is_rejected_and_lock_is_released(tmp_path: Path) -> None:
    path = tmp_path / "agent.sqlite3"
    with Runtime(path):
        with pytest.raises(JournalInUse):
            Runtime(path)
    with Runtime(path):
        pass
