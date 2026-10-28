@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "%~dp0ai.local.json" (
  echo 请先复制 ai.example.json 为 ai.local.json，并填入学校地址、模型名和密钥。
  pause
  exit /b 1
)
"%~dp0dist\AuroraDB.exe" --web --demo --port 0 --db "%~dp0data\demo" --ai-config "%~dp0ai.local.json"
if errorlevel 1 pause
