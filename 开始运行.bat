@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if exist "%~dp0runtime\python.exe" goto embedded

set "PY="
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 set "PY=python"
if not defined PY (
  py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY goto nopython

call :prepare
%PY% app.py
goto after

:embedded
call :prepare
"%~dp0runtime\python.exe" app.py
goto after

:after
echo.
echo 程序已退出。
echo 如果是闪退，请把上面的文字截图。
pause
exit /b 0

:prepare
if not exist data mkdir data
if not exist config.json copy /y config.example.json config.json >nul
echo ==========================================
echo OTA监控工具
echo.
echo 启动后请在浏览器打开：
echo     http://127.0.0.1:8790
echo.
echo 不能关掉这个窗口，关掉就没有了喵（但是打开就又有了喵）
echo ==========================================
echo.
exit /b 0

:nopython
cls
echo ==========================================
echo 没有找到可用的 Python
echo.
echo 这个工具需要 Python 3.10 或更高版本。
echo.
echo 接下来会打开下载页面：
echo     https://www.python.org/downloads/
echo.
echo 安装时一定要勾选：
echo     [x] Add Python to PATH
echo.
echo 安装完成后，重新双击 开始运行.bat。
echo ==========================================
echo.
echo 按任意键打开下载页面...
pause >nul
start "" "https://www.python.org/downloads/"
echo.
echo 下载页面已打开。
echo 安装完成后，请重新双击 开始运行.bat。
pause
exit /b 1
