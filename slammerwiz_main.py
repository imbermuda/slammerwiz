"""PyInstaller entry point.

When ``src/main.py`` is invoked directly by PyInstaller, Python has no
parent package, so the ``from .catalog_client import ...`` style of
relative import inside the src package fails with::

    ImportError: attempted relative import with no known parent package

This shim runs the package the same way ``python -m src.main`` does —
with ``src`` correctly recognised as a package — and forwards the exit
code.
"""

from src.main import main

if __name__ == "__main__":
    import sys
    sys.exit(main())
