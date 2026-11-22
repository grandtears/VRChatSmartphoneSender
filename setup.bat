@echo off
rem 実行場所に依存せず、必ずプロジェクト内でセットアップする。
cd /d "%~dp0"
rem requirements.txtに固定された実行用パッケージをPython 3へインストールする。
py -3 -m pip install -r requirements.txt
if errorlevel 1 (echo Setup failed. Check Python installation. & pause & exit /b 1)
echo Setup complete. Run start.bat.
pause
