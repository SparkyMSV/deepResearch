import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os
import tempfile

# keep test runs from writing into the repo's logs/ when the API lifespan sets up logging
os.environ.setdefault("DR_LOG_FILE", os.path.join(tempfile.gettempdir(), "dr-test.log"))
