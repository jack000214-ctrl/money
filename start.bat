@echo off
chcp 65001 > nul
title SIA Launcher

echo ==========================================================
echo   SIA (자산 증식 의사결정 시스템) 원클릭 실행기
echo ==========================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"

pause
