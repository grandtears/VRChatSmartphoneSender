@echo off
setlocal
cd /d "%~dp0"

rem exeの作成にだけ必要なPyInstallerをインストールする。
py -3 -m pip install "pyinstaller>=6.0,<7"
if errorlevel 1 goto :error

if not exist presets.txt (
  echo presets.txt was not found.
  goto :error
)

rem 単体で起動できるGUIアプリとして、スマートフォン画面もexe内へ格納する。
rem このアプリで使わない大型モジュールは除外し、ファイルサイズを抑える。
py -3 -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --name VRCSmartphoneSender ^
  --exclude-module numpy ^
  --exclude-module pygame ^
  --add-data "phone.html;." ^
  --distpath "." ^
  app.py
if errorlevel 1 goto :error

rem 配布に不要な中間生成物を削除し、完成したexeだけをプロジェクト直下へ残す。
if exist build rmdir /s /q build
if exist VRCSmartphoneSender.spec del /q VRCSmartphoneSender.spec
echo.
echo Build complete: VRCSmartphoneSender.exe
pause
exit /b 0

:error
echo.
echo Build failed. Check Python and the messages above.
pause
exit /b 1
