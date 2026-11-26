$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the build environment.' }
}
& $projectPython -m pip install --disable-pip-version-check -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) { throw 'Build dependency installation failed.' }
& $projectPython tools\create_icon.py
if ($LASTEXITCODE -ne 0) { throw 'Icon generation failed.' }
# The suite includes the extension safety checks and, when Node.js is present, the worker behavior tests.
& $projectPython -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw 'Tests failed; nothing was built.' }
$version = (& $projectPython -c "import dotdm; print(dotdm.__version__)").Trim()
& $projectPython tools\write_version_info.py build\version_info.txt
if ($LASTEXITCODE -ne 0) { throw 'Version resource failed.' }
# One-folder build: DOT.exe with its DLLs in _internal (nothing is unpacked to %TEMP% at start).
& $projectPython -m PyInstaller --noconfirm --clean --onedir --windowed --name DOT `
    --icon "$PSScriptRoot\assets\dot.ico" --add-data "$PSScriptRoot\assets;assets" `
    --version-file "$PSScriptRoot\build\version_info.txt" `
    --distpath "$PSScriptRoot\build\app" --workpath "$PSScriptRoot\build\pyinstaller" --specpath "$PSScriptRoot\build" `
    "$PSScriptRoot\launcher.pyw"
if ($LASTEXITCODE -ne 0) { throw 'Executable packaging failed.' }
& $projectPython tools\smoke_packaged.py
if ($LASTEXITCODE -ne 0) { throw 'The packaged application failed its transfer check.' }
# yt-dlp, Deno and FFmpeg: pinned in installer\tools.json, verified against the publishers' SHA-256.
& $projectPython tools\fetch_tools.py
if ($LASTEXITCODE -ne 0) { throw 'Bundled tools could not be prepared.' }
$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
          "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $iscc) { throw 'Inno Setup 6 is needed to build the installer: winget install JRSoftware.InnoSetup' }
& $iscc "/DAppVersion=$version" "$PSScriptRoot\installer\DOT.iss"
if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed.' }
$setup = "dist\DOT-Setup-$version.exe"
Get-FileHash -LiteralPath $setup -Algorithm SHA256 | Format-List
Write-Output "Ready: $setup"
