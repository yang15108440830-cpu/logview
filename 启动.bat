@echo off
chcp 65001 >nul
cd /d %~dp0
rem tkinter 桌面工具：pythonw 无控制台窗口，start 让 bat 立即返回
start "" ".venv\Scripts\pythonw.exe" app.py
