@echo off
rem Start iPhone monitor in background (no console window)
cd /d "%~dp0"
start "" pythonw app.py
