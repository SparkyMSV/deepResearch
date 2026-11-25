import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os
import tempfile

# keep test runs from writing into the repo's logs/ when the API lifespan sets up logging
os.environ.setdefault("DR_LOG_FILE", os.path.join(tempfile.gettempdir(), "dr-test.log"))


@pytest.fixture(autouse=True)
def no_real_local_models():
    """A test that forgets to fake the local models fails at once instead of waiting on a real MTPLX server."""
    from app import local_llm

    def refuse(role):
        raise RuntimeError(f"test tried to reach the real local {role} model server")

    local_llm.set_client_factory(refuse)
    yield
    local_llm.set_client_factory(refuse)
