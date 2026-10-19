"""Load the standalone asset under a unique test module name."""
import importlib.util
from pathlib import Path
import sys

NAME = "yehbp_iptv_reference"
if NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        NAME, Path(__file__).resolve().parents[1] / "assets/iptv/iptv.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[NAME] = module
    spec.loader.exec_module(module)
iptv = sys.modules[NAME]
