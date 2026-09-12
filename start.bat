@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 写作共创坊
echo ✍️ 写作共创坊启动中...
python app.py
pause
