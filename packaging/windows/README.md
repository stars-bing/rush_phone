# Windows 发布包构建

使用者只需系统 Google Chrome，完整解压发布包后双击 `RushPhone.exe`。Python、Playwright 驱动、Tk、psutil 和 Windows 权限依赖均在包内，用户无需配置 Python 环境。Mac 继续使用项目原来的命令。

## GitHub Actions

项目提交到 GitHub 后，在 Actions 中运行 **Build Windows launcher**（也支持代码推送和 PR 自动构建）。构建成功后下载 `RushPhone-Windows-x64` artifact，解压其中的 `RushPhone-Windows-x64.zip`，再打开 `RushPhone/RushPhone.exe`。

工作流在 Windows 上运行单元测试、离线 Chrome/CDP 流程测试，构建后移除 Python 环境变量并在中文、带空格的目录运行实际 EXE 自检。自检成功后才生成发布 ZIP。工作流只上传发布 ZIP 与校验文件，不上传本地配置、日志、截图或登录资料。

## 本地构建（仅打包者需要）

构建机器需要 Windows x64、Python 3.12 x64 和系统 Google Chrome，在项目根目录的 PowerShell 执行：

```powershell
powershell -ExecutionPolicy Bypass -File packaging/windows/build.ps1
```

构建环境位于 `.build/windows/venv`，产物为 `dist/RushPhone-Windows-x64.zip` 和 `.sha256`。脚本不会安装或替换 Chrome，不打包 Playwright 的 Chromium，也不读取 `config.toml`、本地 Pro Max 配置、`.env` 或 `.profiles`。Chrome 保留原有手动登录流程。

采用 PyInstaller 目录包，`_internal` 必须与 EXE 一起分发。首次启动将公开默认配置复制到 `%LOCALAPPDATA%\RushPhone`，后续升级不覆盖该目录。窗口默认不允许最终提交；用户勾选后才开启真实下单。

## 验证范围

Windows 11 x64 为首版目标；CI 使用 Windows Server 2022 验证运行时和打包。发布前仍需在目标 Windows 桌面机器验证人工登录、原窗口复用和实际 Apple 页面，离线测试不能保证真实网站结账选择器一直有效。Mac 无法直接生成 Windows EXE；须使用上述 Windows 构建流程。

打包依据：[PyInstaller](https://pyinstaller.org/en/stable/)、[Playwright](https://playwright.dev/python/docs/library#pyinstaller)、[系统程序 DLL 环境处理](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html#launching-external-programs-from-the-frozen-application)。
