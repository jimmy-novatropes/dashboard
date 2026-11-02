@echo off
rem Double-click: Claude Code on Ollama (backup when the Claude limit is used up). Settings: claude_ollama.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0claude_ollama.ps1" %*
if errorlevel 1 pause
