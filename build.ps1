<#Requires -Version 5.1
<#
.SYNOPSIS
  Сборка YtDoWnLoAdEr.exe через PyInstaller.
.EXAMPLE
  .\build.ps1            # onedir  -> dist\YtDoWnLoAdEr\YtDoWnLoAdEr.exe (быстрый старт)
  .\build.ps1 -OneFile   # onefile -> dist\YtDoWnLoAdEr.exe (один файл, старт дольше)
#>
param([switch]$OneFile)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath "main.py")) {
    throw "Запусти скрипт из папки проекта (рядом с main.py)."
}

python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller

if ($OneFile) {
    pyinstaller YtDoWnLoAdEr.spec --noconfirm --clean -- --onefile
    Write-Host ""
    Write-Host "Готово: dist\YtDoWnLoAdEr.exe" -ForegroundColor Green
} else {
    pyinstaller YtDoWnLoAdEr.spec --noconfirm --clean
    Write-Host ""
    Write-Host "Готово: dist\YtDoWnLoAdEr\YtDoWnLoAdEr.exe" -ForegroundColor Green
}
Write-Host "Не забудь положить ffmpeg.exe рядом с exe для слияния 1080p+/MP3." -ForegroundColor Yellow
