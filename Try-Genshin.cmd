@echo off
setlocal
chcp 65001 >nul
set "STARTLUX_INSTALLER=%~dp0windows\StartLux-Genshin-0.3.0-preview-win-x64.exe"
if not exist "%STARTLUX_INSTALLER%" (
  echo 找不到安装包，请先在此仓库运行 git lfs pull，或从 GitHub 分支的 windows 目录下载安装程序。
  pause
  exit /b 1
)
powershell.exe -NoProfile -NonInteractive -Command "$p=$env:STARTLUX_INSTALLER; if ((Get-Item -LiteralPath $p).Length -lt 100000000) { Write-Host '安装包尚未完整下载，请运行 git lfs pull，或在 GitHub 文件页点击 Download raw file。'; exit 2 }; if ((Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLowerInvariant() -ne '22f9eeb20ed77e45b58bc3f023106bdef5612e964e899a0e6839482374c5db7e') { Write-Host '安装包校验失败，请重新下载。'; exit 3 }"
if errorlevel 1 (
  pause
  exit /b 1
)
start "" "%STARTLUX_INSTALLER%"
exit /b 0
