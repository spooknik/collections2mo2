"""PyInstaller entry point: just calls the same `main()` as `c2wj-gui`."""

from collections2wabbajack.gui.app import main

if __name__ == "__main__":
    raise SystemExit(main())
