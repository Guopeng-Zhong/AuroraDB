@echo off
setlocal
cd /d "%~dp0"
echo AuroraDB Chinook 公开数据集模式
echo 首次运行会在 data\chinook 中导入 11 张表和约 15,607 行；请等待终端提示工作台地址。
"%~dp0dist\AuroraDB.exe" --dataset chinook --web --port 0 --db "%~dp0data\chinook"
endlocal
