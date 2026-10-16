# 在 Windows x64 / Python 3.12 上构建。仅收集代码、运行依赖和公开默认配置。
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

project_root = Path(SPECPATH).resolve().parents[1]
playwright_data, playwright_binaries, playwright_imports = collect_all("playwright")

analysis = Analysis(
    [str(project_root / "packaging/windows/launcher.py")],
    pathex=[str(project_root)],
    binaries=playwright_binaries,
    datas=playwright_data + [
        (str(project_root / "rush_phone/windows_defaults"), "rush_phone/windows_defaults"),
    ],
    hiddenimports=playwright_imports + [
        "psutil", "win32api", "win32con", "win32security", "ntsecuritycon", "pywintypes",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest"],
    noarchive=False,
)
archive = PYZ(analysis.pure)
executable = EXE(
    archive, analysis.scripts, [], exclude_binaries=True,
    name="RushPhone", console=False, debug=False,
    bootloader_ignore_signals=False, strip=False, upx=False,
)
COLLECT(executable, analysis.binaries, analysis.datas, strip=False, upx=False, name="RushPhone")
