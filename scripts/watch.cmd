@echo off
cd /d "%~dp0.."
set PYTHONUTF8=1
python -m agent_inventory watch --interval 30
