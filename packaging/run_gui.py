"""Packaged-build entry point (Nuitka, PyInstaller): calls the same `main()` as `c2mo2-gui`."""

from collections2mo2.gui.app import main

if __name__ == "__main__":
    raise SystemExit(main())
