from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = Path(os.environ.get("NEO_MOFO_REPO_ROOT", r"E:\Neo-mofox-instance\bot-3693525299\neo-mofox"))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_pkg_name = "mofox_for_astr_plugins"
if _pkg_name not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        _pkg_name,
        str(_PLUGIN_ROOT / "__init__.py"),
        submodule_search_locations=[str(_PLUGIN_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载测试包: {_PLUGIN_ROOT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_pkg_name] = module
    spec.loader.exec_module(module)
