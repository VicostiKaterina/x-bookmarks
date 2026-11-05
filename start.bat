@echo off
rem X Bookmarks launcher for Windows: start the local server and open the app window.
cd /d "%~dp0"
start "" pythonw serve.py --open
