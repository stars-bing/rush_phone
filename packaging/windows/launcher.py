"""PyInstaller 专用入口；不替换 rush_phone/__main__.py。"""
from rush_phone.windows_app import main

if __name__ == "__main__":
    raise SystemExit(main())
