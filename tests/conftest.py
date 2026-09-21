"""Run tests without touching the user's AstrBot runtime data."""

import os
import site
import sys
import tempfile
from pathlib import Path

import pytest

WORKSPACE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKSPACE))
ASTRBOT = WORKSPACE / "AstrBot"
if ASTRBOT.is_dir():
    sys.path.insert(0, str(ASTRBOT))
    dependencies = ASTRBOT / ".venv" / "Lib" / "site-packages"
    if dependencies.exists():
        site.addsitedir(str(dependencies))

_runtime = tempfile.TemporaryDirectory(prefix="custom-plan-test-")
_previous_root = os.environ.get("ASTRBOT_ROOT")
os.environ["ASTRBOT_ROOT"] = _runtime.name


@pytest.fixture
async def storage(tmp_path):
    from astrbot_plugin_custom_plan.storage import Storage

    store = Storage(tmp_path / "plans.sqlite3")
    await store.initialize()
    return store


def pytest_unconfigure(config):
    if _previous_root is None:
        os.environ.pop("ASTRBOT_ROOT", None)
    else:
        os.environ["ASTRBOT_ROOT"] = _previous_root
    _runtime.cleanup()
