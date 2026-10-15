@echo off
chcp 65001 > nul
title Personal-CIO Launcher

echo ==========================================================
echo   Personal-CIO (자산 증식 의사결정 시스템) 원클릭 실행기
echo ==========================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"

pause
