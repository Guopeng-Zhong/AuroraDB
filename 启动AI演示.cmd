@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "AURORA_AI_MODE=mock"
"%~dp0dist\AuroraDB.exe" --web --demo --port 0 --db "%~dp0data\ai-demo"
if errorlevel 1 pause
