"""Guards for token_kit.router.

An `__init__.py` is not optional here: unittest discovery walks packages, and
a test directory without one is skipped SILENTLY -- which has already happened
once in this kit.
"""

import sys
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parent.parent.parent
if str(KIT_DIR / "src") not in sys.path:
    sys.path.insert(0, str(KIT_DIR / "src"))
