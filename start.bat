@echo off
chcp 65001 > nul
title Money Launcher

echo ==========================================================
echo   Money (Smart Investment Advisor) 원클릭 실행기
echo ==========================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"

pause
