import atexit
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_temporary = tempfile.TemporaryDirectory(prefix="tool-prompts-tests-")
atexit.register(_temporary.cleanup)
os.environ.setdefault("ASTRBOT_ROOT", _temporary.name)
os.environ["ASTRBOT_DISABLE_METRICS"] = "1"


@pytest.fixture
def anyio_backend():
    return "asyncio"
