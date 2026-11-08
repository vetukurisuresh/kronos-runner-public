"""Vendored Kronos architecture code.

Source: https://github.com/shiyu-coder/Kronos (MIT licence), files
``model/kronos.py`` and ``model/module.py``, retrieved 2026-11-08.

Only one change was made: the upstream ``sys.path.append("../")`` +
``from model.module import *`` was rewritten to ``from .module import *`` so the
package can be imported without shadowing this project's own ``model`` package.

The HuggingFace repos ship only ``config.json`` + ``model.safetensors``, so this
source is required to instantiate the weights. Nothing here is modified
behaviourally.
"""

from .kronos import Kronos, KronosPredictor, KronosTokenizer

__all__ = ["Kronos", "KronosPredictor", "KronosTokenizer"]
