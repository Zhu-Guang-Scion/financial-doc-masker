@echo off
REM ============================================================
REM  setup_cpu_deps_env.bat (v2.1.1)
REM  CPU dependency detect / auto-install / package-repair check.
REM
REM  Usage:
REM    setup_cpu_deps_env.bat
REM        Source-version mode: detect + auto-install the optional
REM        deps used by the masking pipelines (python + pip needed).
REM    setup_cpu_deps_env.bat /exe "D:\path\to\<program dir>"
REM        Packaged-EXE mode: NO python needed. Verifies the program
REM        dir layout and key bundled packages. Use this when the
REM        EXE reports "missing python-docx": that message is emitted
REM        when the docx/lxml import chain fails at runtime, which on
REM        EXE machines is usually an incomplete extract (antivirus
REM        quarantine) or a missing VC++ x64 runtime (lxml .pyd DLL
REM        load failure wrapped into the docx message).
REM    setup_cpu_deps_env.bat /help
REM
REM  Conventions (GPU bat lesson): no chcp; this file is GBK(CP936)
REM  encoded with CRLF endings; no delayed expansion; no quotes
REM  inside ( ) blocks.
REM ============================================================

setlocal

if "%~1"=="/help" goto :help
if "%~1"=="/exe" goto :exemode
goto :pymode

:help
echo Usage:
echo   setup_cpu_deps_env.bat                  source detect + install
echo   setup_cpu_deps_env.bat /exe "progdir"   packaged-exe integrity check
echo   setup_cpu_deps_env.bat /help
exit /b 0

REM ============================================================
REM  MODE A: packaged-EXE integrity check (no python required)
REM ============================================================
:exemode
set "PROGDIR=%~2"
if "%PROGDIR%"=="" (
    echo [FAIL] /exe mode needs the program directory.
    exit /b 2
)
if not exist "%PROGDIR%" (
    echo [FAIL] directory not found: %PROGDIR%
    exit /b 2
)
echo == Packaged-EXE integrity check ==
echo dir: %PROGDIR%
set BAD=0
for %%D in (docx lxml openpyxl onnxruntime rapidocr_onnxruntime cv2 numpy jieba pymupdf) do (
    if exist "%PROGDIR%\_internal\%%D" (
        echo   [ OK ] _internal\%%D
    ) else (
        echo   [FAIL] _internal\%%D MISSING - package incomplete or quarantined
        set BAD=1
    )
)
if exist "%PROGDIR%\_internal\*.pyd" (
    echo   [ OK ] _internal pyd binaries present
) else (
    echo   [FAIL] no pyd binaries under _internal - package incomplete
    set BAD=1
)
echo.
echo exe MD5 - compare with the official release note:
powershell -NoProfile -Command "Get-FileHash -Algorithm MD5 -LiteralPath (Join-Path '%PROGDIR%' '财税文档脱敏工具.exe') | Format-List Hash"
echo.
if "%BAD%"=="1" goto :exe_advice
echo Layout check: all key packages present.
echo If the tool STILL reports missing python-docx on this machine:
echo   1. Install Microsoft Visual C++ 2015-2022 Redistributable x64.
echo      python-docx needs lxml; its DLL load failure gets wrapped
echo      into the misleading python-docx message.
echo   2. Compare the exe MD5 above with the official release.
echo   3. Add an antivirus exclusion, then re-extract the release zip.
exit /b 0

:exe_advice
echo.
echo ADVICE: the package is incomplete on this machine.
echo   1. Check antivirus quarantine for files under _internal.
echo   2. Re-extract the official release zip to a local disk path,
echo      then re-run this check.
exit /b 4

REM ============================================================
REM  MODE B: source-version detect + auto-install (python needed)
REM ============================================================
:pymode
echo == Source-version dependency detect + auto-install ==
set "PY=%PYTHON%"
if "%PY%"=="" (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=py -3"
)
if "%PY%"=="" (
    python -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if "%PY%"=="" (
    echo [FAIL] No Python on PATH. Install Python 3.10+ or set PYTHON.
    exit /b 2
)
echo [1] Python: %PY%
%PY% -m pip --version >nul 2>&1
if errorlevel 1 (
    echo [FAIL] pip unavailable. Run: %PY% -m ensurepip --upgrade
    exit /b 2
)
echo [2] pip OK

call :ensure pymupdf      pymupdf          "PyMuPDF - PDF"
call :ensure docx         python-docx      "python-docx - Word"
call :ensure openpyxl     openpyxl         "openpyxl - Excel"
call :ensure pptx         python-pptx      "python-pptx - PPT"
call :ensure jieba        jieba            "jieba"
call :ensure rapidocr_onnxruntime rapidocr-onnxruntime "RapidOCR - OCR v6 CPU"
call :ensure onnxruntime  onnxruntime      "onnxruntime"
call :ensure cv2          opencv-python    "opencv-python - stamp"
call :ensure numpy        numpy            "numpy"
call :ensure xlrd         xlrd             "xlrd - legacy xls, optional"
call :ensure win32com     pywin32          "pywin32 - xls COM, optional"

echo.
echo [4] Final re-probe:
%PY% -c "import fitz, docx, openpyxl, pptx, jieba, rapidocr_onnxruntime, onnxruntime, cv2, numpy"
if errorlevel 1 (
    echo RESULT: FAIL - some dependencies still missing. Check the pip
    echo output above, then re-run this script.
    exit /b 4
)
echo RESULT: OK - all CPU dependencies present.
echo Re-run the tool; the startup log should show the
echo all-components-available line.
exit /b 0

REM ---- helper: ensure one module -------------------------------
REM %1 import name, %2 pip dist name, %3 display label
:ensure
%PY% -c "import %~1" >nul 2>&1
if not errorlevel 1 (
    echo   [ OK ] %~3
    goto :eof
)
echo   [MISS] %~3 - installing %~2 ...
%PY% -m pip install %~2
if errorlevel 1 (
    echo   [FAIL] pip install %~2 failed - see output above.
    goto :eof
)
echo   [ OK ] installed %~2
goto :eof
