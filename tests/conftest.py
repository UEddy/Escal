"""Put src/ on sys.path so tests import the modules directly.

The project is not packaged yet. This keeps the import surface honest without
committing to a layout decision the rest of the build has not made.
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
