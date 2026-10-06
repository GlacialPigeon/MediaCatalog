# MediaCatalog
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import sys

from modules.application import run_application


def main() -> int:
    if sys.platform == "win32":
        from modules.platform_windows import WindowsPlatform
        platform = WindowsPlatform()
        return run_application(platform)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
