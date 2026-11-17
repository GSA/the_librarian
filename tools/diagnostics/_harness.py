"""Point a benchmark script at the checkout it measures.

The benchmarks run as scripts (``python3 tools/diagnostics/<name>.py --tree CHECKOUT``) so that
``tools`` is imported from ``CHECKOUT`` rather than from whichever tree holds the script. Import this
module with ``from _harness import use_tree`` inside the function that bootstraps (the script
directory is ``sys.path[0]``); never through ``tools.diagnostics``, which would bind ``tools`` to the
wrong checkout before ``use_tree`` runs. ``python -c`` child scripts cannot reach this module and
inline the same two statements.
"""

import os
from pathlib import Path
import sys


def use_tree(tree):
    """Import ``tools`` from ``tree`` and run from it; returns the resolved path as a string."""
    tree = str(Path(tree).resolve())
    sys.path.insert(0, tree)
    os.chdir(tree)
    return tree
