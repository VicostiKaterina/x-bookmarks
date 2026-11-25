@echo off
rem X Bookmarks · Windows 启动器
rem 优先用 launch.vbs（自动找 pythonw，不会弹出黑框）；找不到时才退回 pythonw serve.py --open。
cd /d "%~dp0"
if exist "launch.vbs" (
  start "" wscript.exe //nologo "launch.vbs"
) else (
  start "" pythonw serve.py --open
)
