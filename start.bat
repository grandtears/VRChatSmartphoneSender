@echo off
rem このbatファイルが置かれているフォルダーへ移動してからPython版を起動する。
cd /d "%~dp0"
py -3 app.py
rem 起動エラーが発生した場合だけ画面を残し、エラーメッセージを確認できるようにする。
if errorlevel 1 pause
