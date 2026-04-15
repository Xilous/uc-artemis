@echo off
REM Build UC Artemis as a single Windows executable.
REM Run from the repo root with: build_exe.bat

pyinstaller ^
    --onefile ^
    --name "UC Artemis" ^
    --collect-submodules fitz ^
    --collect-data fitz ^
    --add-data "web/templates;web/templates" ^
    --add-data "web/static;web/static" ^
    --add-data "web/static/pdfjs;web/static/pdfjs" ^
    --hidden-import openpyxl.cell._writer ^
    main.py

echo.
echo Build complete. Executable at: dist\UC Artemis.exe
