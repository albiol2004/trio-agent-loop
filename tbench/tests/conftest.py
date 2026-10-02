"""tbench/'s modules import each other bare (``import goal``, ``import
shell``, ...), matching how Harbor loads the agent with ``PYTHONPATH=tbench``
(see run_job.sh and trio_tbench_agent.py's module docstring). Insert the
tbench/ directory itself -- not its parent -- at the front of ``sys.path``
so tests resolve imports the same way, regardless of the cwd pytest is
invoked from.
"""
from __future__ import annotations

import sys
from pathlib import Path

_TBENCH_DIR = Path(__file__).resolve().parents[1]
if str(_TBENCH_DIR) not in sys.path:
    sys.path.insert(0, str(_TBENCH_DIR))
