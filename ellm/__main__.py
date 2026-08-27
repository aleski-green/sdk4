"""python -m ellm  →  CLI client."""
import os
import runpy
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
runpy.run_path(os.path.join(_ROOT, "ellm.py"), run_name="__main__")
