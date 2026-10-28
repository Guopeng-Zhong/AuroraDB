@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "%~dp0ai.local.json" (
  "%~dp0dist\AuroraDB.exe" --web --demo --db "%~dp0data\demo" --ai-config "%~dp0ai.local.json"
) else (
  "%~dp0dist\AuroraDB.exe" --web --demo --db "%~dp0data\demo"
)
if errorlevel 1 pause
