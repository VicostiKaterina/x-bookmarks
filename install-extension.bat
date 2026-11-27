@echo off
chcp 65001 >nul
setlocal
set "HERE=%~dp0"
set "EXT=%HERE%extension"

echo.
echo  X 书签桥 · 浏览器扩展安装
echo  ==========================================
echo.
echo  扩展目录：
echo    %EXT%
echo.
echo  步骤：
echo    1. 浏览器打开  chrome://extensions
echo    2. 右上角打开「开发者模式」
echo    3. 点「加载已解压的扩展程序」，选上面这个 extension 目录
echo       （选的是 extension 这一层，不要进到里面的文件）
echo    4. 装好后，回到已经打开的 x.com 标签页按一次 F5 刷新
echo    5. 回 x.com 任意页面，点工具栏的「X 书签桥」图标，点「抓取并同步到 App」
echo.
echo  注意：改了扩展文件（比如更新代码）后，要回 chrome://extensions
echo        点那个卡片上的刷新图标，扩展才会用新代码。
echo.
echo  抓不到时：先点扩展弹窗里的「刷新页面」，再点抓取。
echo.

start "" chrome.exe "chrome://extensions" 2>nul || start "" "chrome://extensions"
start "" "%EXT%"
echo.
pause
