param([string]$Python = "python")
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
    throw "Build the Windows package on Windows x64."
}

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$BuildRoot = Join-Path $ProjectRoot ".build/windows"
$BuildPython = Join-Path $BuildRoot "venv/Scripts/python.exe"
$DistRoot = Join-Path $ProjectRoot "dist"

Push-Location $ProjectRoot
try {
    & $Python -c 'import sys; assert sys.version_info[:2] == (3, 12) and sys.maxsize > 2**32'
    if ($LASTEXITCODE -ne 0) { throw "Python 3.12 x64 is required on the build machine." }
    & $Python -m venv (Join-Path $BuildRoot "venv")
    if ($LASTEXITCODE -ne 0) { throw "Could not create the build environment." }
    & $BuildPython -m pip install --disable-pip-version-check . "pyinstaller==6.22.3"
    if ($LASTEXITCODE -ne 0) { throw "Could not install build dependencies." }
    & $BuildPython -m unittest discover -s tests
    if ($LASTEXITCODE -ne 0) { throw "Unit tests failed." }
    $env:RUSH_PHONE_BROWSER_TESTS = "1"
    try {
        & $BuildPython -m unittest discover -s tests -p test_session_purchase_browser.py
        if ($LASTEXITCODE -ne 0) { throw "Offline Chrome/CDP tests failed." }
    } finally {
        Remove-Item Env:RUSH_PHONE_BROWSER_TESTS -ErrorAction SilentlyContinue
    }
    & $BuildPython -m PyInstaller --clean --noconfirm --workpath (Join-Path $BuildRoot "pyinstaller") --distpath $DistRoot (Join-Path $PSScriptRoot "RushPhone.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

    # 在中文、含空格的路径中运行打包产物，并移除 Python 相关环境。
    $SmokeRoot = Join-Path $BuildRoot "smoke/中文目录 with spaces"
    New-Item -ItemType Directory -Path $SmokeRoot -Force | Out-Null
    $SmokeApp = Join-Path $SmokeRoot "RushPhone"
    if (Test-Path $SmokeApp) { Remove-Item $SmokeApp -Recurse -Force }
    Copy-Item (Join-Path $DistRoot "RushPhone") $SmokeApp -Recurse
    $ReportPath = Join-Path $BuildRoot "smoke-report.json"
    if (Test-Path $ReportPath) { Remove-Item $ReportPath }
    $SavedEnvironment = @{}
    foreach ($Name in @("PATH", "PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV")) {
        $SavedEnvironment[$Name] = [Environment]::GetEnvironmentVariable($Name, "Process")
    }
    try {
        $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
        foreach ($Name in @("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV")) {
            [Environment]::SetEnvironmentVariable($Name, $null, "Process")
        }
        $Process = Start-Process -FilePath (Join-Path $SmokeApp "RushPhone.exe") -ArgumentList "--self-test `"$ReportPath`"" -WorkingDirectory $env:TEMP -PassThru
        $null = $Process.Handle
        if (-not $Process.WaitForExit(120000)) {
            $Process.Kill()
            throw "Packaged smoke test timed out."
        }
        if ($Process.ExitCode -ne 0 -or -not (Test-Path $ReportPath)) {
            throw "Packaged smoke test failed. See .build/windows/smoke-report.json."
        }
        $Report = Get-Content $ReportPath -Raw | ConvertFrom-Json
        if (-not $Report.ok) { throw "Packaged smoke test failed." }
        Get-Content $ReportPath -Raw
    } finally {
        foreach ($Name in $SavedEnvironment.Keys) {
            [Environment]::SetEnvironmentVariable($Name, $SavedEnvironment[$Name], "Process")
        }
    }

    Copy-Item (Join-Path $PSScriptRoot "使用说明.txt") (Join-Path $DistRoot "RushPhone/使用说明.txt")
    $ZipPath = Join-Path $DistRoot "RushPhone-Windows-x64.zip"
    Compress-Archive -Path (Join-Path $DistRoot "RushPhone") -DestinationPath $ZipPath -Force
    $Digest = (Get-FileHash $ZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    [IO.File]::WriteAllText("$ZipPath.sha256", "$Digest  RushPhone-Windows-x64.zip`n", [Text.Encoding]::ASCII)
    Write-Host "Ready: $ZipPath"
} finally {
    Pop-Location
}
