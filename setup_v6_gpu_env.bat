@echo off
REM ============================================================
REM  setup_v6_gpu_env.bat (v1.9.2a)
REM  Create the external GPU-v6 inference environment:
REM  isolated venv + onnxruntime-gpu 1.20.2 + rapidocr-onnxruntime
REM  1.4.4 + PP-OCRv6 medium weights (sha256 verified).
REM
REM  NOTE: this script is intentionally pure ASCII with NO `chcp`
REM  call. A UTF-8 batch file with non-ASCII comments + mid-file
REM  `chcp 65001` derails cmd's batch line parser (byte-offset
REM  misalignment -> garbage commands), observed live on Win11
REM  zh-CN; and on old Win10 certutil prints space-separated
REM  hashes, so verification now uses PowerShell Get-FileHash.
REM
REM  Usage:   setup_v6_gpu_env.bat [target_dir] [/force]
REM           default target: [this script dir]\v6_gpu_env
REM           /help : print the usage note and exit.
REM           /force : delete the ready marker (.ready) of the
REM                    target and reinstall from scratch over it.
REM
REM  Lifecycle (since v1.9.2a):
REM   - ready marker present -> prints "already ready" note, exit 0,
REM     nothing is touched (idempotent). To reinstall: delete the
REM     v6_gpu_env folder, or re-run with /force.
REM   - venv present but no ready marker = an earlier run was
REM     interrupted: simply re-run this script, it will continue
REM     from where it left off.
REM   - the ready marker [target]\.ready is written ONLY as the
REM     very last step, after all install steps + the 3-part
REM     self-check passed. An interrupted install therefore never
REM     has a .ready file.
REM
REM  Needs:   local Python 3.11.x; NVIDIA GPU + working driver,
REM           >= 4GB VRAM recommended.
REM  After:   pick "GPU-v6" (advanced) as OCR mode in the tool
REM           (takes effect on next batch).
REM ============================================================

setlocal

REM ---- parse arguments: [target_dir] [/force] [/?] ----
REM  NOTE: capture the script path BEFORE the loop - plain `shift`
REM  also shifts %0 in cmd, so %~f0 would turn into the target dir.
set "SELF=%~f0"
set "TARGET=%~dp0v6_gpu_env"
set "FORCE=0"
:parse_args
if "%~1"=="" goto :args_done
if /i "%~1"=="/?" goto :usage
if /i "%~1"=="/help" goto :usage
if /i "%~1"=="/force" set "FORCE=1"
if /i not "%~1"=="/force" set "TARGET=%~1"
shift
goto :parse_args
:args_done

set "DET_NAME=PP-OCRv6_det_medium.onnx"
set "REC_NAME=PP-OCRv6_rec_medium.onnx"
set "DET_SHA256=92078b7355007ccfffcd4c8cd441a3afd4538904d06881b29a155e1e679907c2"
set "REC_SHA256=eef444829dbbe18d7fea59a3f6eb75647518d2b3a9568d27c92e42940204894b"

echo === GPU-v6 environment setup (v1.9.2a) ===
echo Target: %TARGET%
echo.

REM ---- lifecycle gate: ready marker / resume detection ----
REM  .ready is written only as the very last step of a full pass,
REM  so "no .ready" means the install is fresh or was interrupted.
if exist "%TARGET%\.ready" (
    if not "%FORCE%"=="1" goto :already_ready
    echo [INFO] /force requested: deleting the ready marker and reinstalling ...
    del /f /q "%TARGET%\.ready"
    if errorlevel 1 goto :fail_ready_del
)
set "RESUME_MSG=[INFO] Detected an unfinished installation. Continuing from where it left off ..."
if "%FORCE%"=="1" set "RESUME_MSG=[INFO] /force: reinstalling over the existing environment."
if exist "%TARGET%\Scripts\python.exe" (
    echo %RESUME_MSG%
) else (
    echo [INFO] No existing environment found. Performing a fresh install.
)

REM ---- [STEP 1/8] find Python 3.11 ----
echo [STEP 1/8] Looking for Python 3.11 ...
set "PYBASE="
py -3.11 -c "import sys; sys.exit(0 if sys.version_info[:2]==(3,11) else 1)" >nul 2>&1
if not errorlevel 1 set "PYBASE=py -3.11"
if defined PYBASE goto :have_py
python -c "import sys; sys.exit(0 if sys.version_info[:2]==(3,11) else 1)" >nul 2>&1
if errorlevel 1 goto :fail_py
set "PYBASE=python"
:have_py
echo   Python 3.11 found: %PYBASE%

REM ---- [STEP 2/8] create venv (idempotent: reuse existing) ----
if exist "%TARGET%\Scripts\python.exe" (
    echo [STEP 2/8] venv already exists, reuse: %TARGET%
    goto :have_venv
)
echo [STEP 2/8] Creating venv ...
%PYBASE% -m venv "%TARGET%"
if errorlevel 1 goto :fail_venv
:have_venv
set "VPY=%TARGET%\Scripts\python.exe"
if not exist "%VPY%" goto :fail_venv

REM ---- [STEP 3/8] install pinned packages ----
REM  The onnxruntime-gpu Windows wheel ships WITHOUT CUDA/cuDNN DLLs
REM  (without them the CUDA EP fails with LoadLibrary error 126 and
REM  silently falls back to CPU), so the nvidia-*-cu12 runtimes are
REM  installed alongside. rapidocr-onnxruntime MUST be installed with
REM  --no-deps: its `onnxruntime` (CPU build) dependency would clobber
REM  the onnxruntime-gpu package files (two dist-infos, unpack order
REM  decides the winner). Remaining deps are installed explicitly so
REM  the venv keeps exactly ONE onnxruntime - the GPU build.
echo [STEP 3/8] Installing onnxruntime-gpu==1.20.2 + CUDA12/cuDNN9 runtime + modelscope ...
"%VPY%" -m pip install --disable-pip-version-check "onnxruntime-gpu==1.20.2" "nvidia-cuda-runtime-cu12==12.9.79" "nvidia-cuda-nvrtc-cu12==12.9.86" "nvidia-cublas-cu12==12.9.2.10" "nvidia-cufft-cu12==11.4.1.4" "nvidia-cudnn-cu12==9.25.1.1" modelscope
if errorlevel 1 goto :fail_pip
echo [STEP 3/8] Installing rapidocr-onnxruntime==1.4.4 (--no-deps) + its deps ...
"%VPY%" -m pip install --disable-pip-version-check --no-deps "rapidocr-onnxruntime==1.4.4"
if errorlevel 1 goto :fail_pip
"%VPY%" -m pip install --disable-pip-version-check "numpy==1.26.4" opencv-python Pillow pyclipper PyYAML Shapely six tqdm
if errorlevel 1 goto :fail_pip

REM ---- [STEP 4/8] download PP-OCRv6 medium weights (ModelScope, ~132MB) ----
set "MS_EXE=%TARGET%\Scripts\modelscope.exe"
if exist "%MS_EXE%" goto :have_ms
goto :fail_ms
:have_ms
echo [STEP 4/8] Downloading PP-OCRv6 medium weights from ModelScope ...
"%MS_EXE%" download RapidAI/RapidOCR onnx/PP-OCRv6/det/%DET_NAME% onnx/PP-OCRv6/rec/%REC_NAME% --local_dir "%TARGET%\models_dl"
if errorlevel 1 goto :fail_dl

REM ---- [STEP 5/8] place weights into models\onnx_v6 ----
echo [STEP 5/8] Placing weights ...
if not exist "%TARGET%\models\onnx_v6" (
    mkdir "%TARGET%\models\onnx_v6"
    if errorlevel 1 goto :fail_move
)
set "DET_SRC="
set "REC_SRC="
REM  Locate downloaded weights with `dir /s /b`: `for /r ... in (<fixed
REM  name>)` joins EVERY walked subdirectory with the name (no existence
REM  check, last subdir wins) - observed to point DET_SRC at the
REM  nonexistent rec\PP-OCRv6_det_medium.onnx, breaking step 5 always.
for /f "delims=" %%f in ('dir /s /b "%TARGET%\models_dl\%DET_NAME%" 2^>nul') do set "DET_SRC=%%f"
for /f "delims=" %%f in ('dir /s /b "%TARGET%\models_dl\%REC_NAME%" 2^>nul') do set "REC_SRC=%%f"
if not defined DET_SRC goto :fail_move
if not defined REC_SRC goto :fail_move
move /y "%DET_SRC%" "%TARGET%\models\onnx_v6\%DET_NAME%" >nul
if errorlevel 1 goto :fail_move
move /y "%REC_SRC%" "%TARGET%\models\onnx_v6\%REC_NAME%" >nul
if errorlevel 1 goto :fail_move

REM ---- [STEP 6/8] verify sha256 ----
REM  Use PowerShell Get-FileHash: certutil on old Win10 prints the
REM  hash space-separated (contiguous `find` never matches), and
REM  with Git-for-Windows usr/bin on PATH `find` resolves to GNU
REM  find and errors out. Both pitfalls avoided this way.
echo [STEP 6/8] Verifying sha256 ...
set "DET_HASH="
set "REC_HASH="
for /f "delims=" %%h in ('powershell -NoProfile -Command "(Get-FileHash -Algorithm SHA256 -LiteralPath '%TARGET%\models\onnx_v6\%DET_NAME%').Hash.ToLower()"') do set "DET_HASH=%%h"
if /i not "%DET_HASH%"=="%DET_SHA256%" goto :fail_sha
for /f "delims=" %%h in ('powershell -NoProfile -Command "(Get-FileHash -Algorithm SHA256 -LiteralPath '%TARGET%\models\onnx_v6\%REC_NAME%').Hash.ToLower()"') do set "REC_HASH=%%h"
if /i not "%REC_HASH%"=="%REC_SHA256%" goto :fail_sha

REM ---- [STEP 7/8] self-check inside the venv (3 checks) ----
REM  import onnxruntime / import rapidocr_onnxruntime /
REM  "CUDAExecutionProvider" in providers - each printed as OK/FAIL.
REM  Any failure exits here WITHOUT writing the ready marker (see
REM  lifecycle note at the top). Version numbers are printed for the
REM  user and re-measured live in STEP 8 for the marker itself.
echo [STEP 7/8] Self-check: imports + CUDAExecutionProvider ...
"%VPY%" -c "import onnxruntime as ort; print('[OK] import onnxruntime, version ' + ort.__version__)"
if errorlevel 1 goto :fail_sc_ort
"%VPY%" -c "from importlib.metadata import version as mdver; import rapidocr_onnxruntime; print('[OK] import rapidocr_onnxruntime, version ' + mdver('rapidocr-onnxruntime'))"
if errorlevel 1 goto :fail_sc_rapid
"%VPY%" -c "import onnxruntime as ort; assert 'CUDAExecutionProvider' in ort.get_available_providers(), 'CUDAExecutionProvider missing'; print('[OK] CUDAExecutionProvider in available providers')"
if errorlevel 1 goto :fail_sc_cuda

REM ---- [STEP 8/8] write ready marker (LAST step) ----
REM  JSON, UTF-8 without BOM. The two version fields are measured
REM  live in this venv right here - never hardcoded.
echo [STEP 8/8] Writing ready marker ...
"%VPY%" -c "import sys, json, datetime, importlib.metadata as md, onnxruntime as ort; data = {'ort_version': ort.__version__, 'rapidocr_version': md.version('rapidocr-onnxruntime'), 'cuda_ep': True, 'created_at': datetime.datetime.now().isoformat(timespec='seconds'), 'bat_version': '1.9.2a'}; fh = open(sys.argv[1], 'w', encoding='utf-8', newline='\n'); fh.write(json.dumps(data, indent=2) + '\n'); fh.close(); print('[OK] ready marker written')" "%TARGET%\.ready"
if errorlevel 1 goto :fail_ready

REM ---- cleanup & done ----
rmdir /s /q "%TARGET%\models_dl" 2>nul
echo.
echo Environment is ready.
echo.
echo ============================================================
echo  SETUP OK %TARGET%
echo  Ready marker: %TARGET%\.ready
echo  Now pick "GPU-v6" (advanced) as the OCR mode in the mask
echo  tool (takes effect on next batch). If the environment
echo  misbehaves, switch back to "Auto" in the tool.
echo ============================================================
endlocal
exit /b 0

:fail_ready_del
echo [FAIL] STEP 0 (lifecycle): could not delete the stale ready marker:
echo        %TARGET%\.ready
echo        What it means: the file is locked by another program or a
echo        permission problem - a reinstall cannot be tracked safely.
echo        How to resume: close whatever holds the file (or delete it
echo        by hand), then simply re-run this script, it will continue
echo        from where it left off.
echo SETUP FAIL step0_ready_marker_delete
goto :fail_exit

:fail_py
echo [FAIL] STEP 1/8 (find Python 3.11): no usable Python 3.11 was found.
echo        What it means: the venv needs Python 3.11 (manual section 6);
echo        neither the py launcher nor python on PATH provides it.
echo        How to resume: install Python 3.11 (check "Add to PATH"), then
echo        simply re-run this script, it will continue from where it
echo        left off.
echo SETUP FAIL step1_python311
goto :fail_exit

:fail_venv
echo [FAIL] STEP 2/8 (create venv): failed to create the venv at:
echo        %TARGET%
echo        What it means: python -m venv did not finish (disk full,
echo        antivirus blocking, or the folder is not writable).
echo        How to resume: simply re-run this script, it will continue
echo        from where it left off.
echo SETUP FAIL step2_venv
goto :fail_exit

:fail_pip
echo [FAIL] STEP 3/8 (pip install): a pip install command failed.
echo        What it means: usually a network/proxy problem; offline
echo        machines can use an internal mirror, e.g.:
echo        "%VPY%" -m pip install -i ^<internal-mirror^> onnxruntime-gpu==1.20.2 rapidocr-onnxruntime==1.4.4 modelscope
echo        How to resume: fix the network, then simply re-run this
echo        script, it will continue from where it left off.
echo SETUP FAIL step3_pip
goto :fail_exit

:fail_ms
echo [FAIL] STEP 4/8 (weights download): modelscope CLI not found:
echo        %MS_EXE%
echo        What it means: the modelscope package was not installed into
echo        the venv in STEP 3 (pip only partially succeeded earlier).
echo        How to resume: simply re-run this script, it will continue
echo        from where it left off.
echo SETUP FAIL step4_modelscope
goto :fail_exit

:fail_dl
echo [FAIL] STEP 4/8 (weights download): ModelScope download failed.
echo        What it means: network problem or ModelScope unreachable.
echo        Manual fallback: download the two PP-OCRv6 medium onnx files
echo        yourself and put them into %TARGET%\models\onnx_v6 keeping
echo        the file names.
echo        How to resume: simply re-run this script, it will continue
echo        from where it left off.
echo SETUP FAIL step4_modelscope
goto :fail_exit

:fail_move
echo [FAIL] STEP 5/8 (place weights): could not locate or move the
echo        downloaded weights under %TARGET%\models_dl
echo        What it means: the download folder does not contain the two
echo        expected .onnx files (download incomplete or cleaned up).
echo        How to resume: simply re-run this script, it will continue
echo        from where it left off (it re-downloads what is missing).
echo SETUP FAIL step5_move
goto :fail_exit

:fail_sha
echo [FAIL] STEP 6/8 (sha256): hash mismatch! Downloaded weights differ
echo        from the official release (corrupted or tampered download).
echo        What it means: the files in %TARGET%\models\onnx_v6 are not
echo        the official PP-OCRv6 medium weights.
echo        How to resume: delete %TARGET%\models\onnx_v6 and
echo        %TARGET%\models_dl, then simply re-run this script, it will
echo        continue from where it left off.
echo SETUP FAIL step6_sha256
goto :fail_exit

:fail_sc_ort
echo [FAIL] STEP 7/8 (self-check): "import onnxruntime" failed inside the
echo        venv (see the Python error printed above).
echo        What it means: the pinned GPU onnxruntime package is missing
echo        or broken in this venv.
echo        How to resume: simply re-run this script, it will continue
echo        from where it left off; if it fails again, delete the
echo        v6_gpu_env folder and start over.
echo SETUP FAIL step7_selfcheck_onnxruntime
goto :fail_exit

:fail_sc_rapid
echo [FAIL] STEP 7/8 (self-check): "import rapidocr_onnxruntime" failed
echo        inside the venv (see the Python error printed above).
echo        What it means: the OCR package or one of its explicit
echo        dependencies is missing or broken in this venv.
echo        How to resume: simply re-run this script, it will continue
echo        from where it left off; if it fails again, delete the
echo        v6_gpu_env folder and start over.
echo SETUP FAIL step7_selfcheck_rapidocr
goto :fail_exit

:fail_sc_cuda
echo [FAIL] STEP 7/8 (self-check): "CUDAExecutionProvider" is NOT in
echo        onnxruntime.get_available_providers().
echo        What it means: the NVIDIA driver is missing or too old, or
echo        the CUDA runtime DLLs failed to load - inference would
echo        silently fall back to CPU.
echo        How to resume: install/update the NVIDIA GPU driver, then
echo        simply re-run this script, it will continue from where it
echo        left off.
echo SETUP FAIL step7_selfcheck_cuda_ep
goto :fail_exit

:fail_ready
echo [FAIL] STEP 8/8 (write ready marker): could not write:
echo        %TARGET%\.ready
echo        What it means: disk full or permission denied. The
echo        environment itself is installed, but the tool will NOT
echo        detect it as ready without this marker.
echo        How to resume: simply re-run this script, it will continue
echo        from where it left off.
echo SETUP FAIL step8_ready_marker
goto :fail_exit

:already_ready
echo Environment is already ready. To reinstall, delete the v6_gpu_env
echo folder first, or re-run with /force:
echo   "%SELF%" /force
echo Target: %TARGET%
endlocal
exit /b 0

:usage
echo === GPU-v6 environment setup (setup_v6_gpu_env.bat, v1.9.2a) ===
echo.
echo Usage: setup_v6_gpu_env.bat [target_dir] [/force]
echo   target_dir  where to build the environment
echo               (default: this script folder + \v6_gpu_env)
echo   /force      delete the ready marker .ready and reinstall
echo.
echo Lifecycle:
echo   - ready marker present: prints "already ready" and exits 0,
echo     nothing is reinstalled (idempotent).
echo   - interrupted earlier: simply re-run this script, it will
echo     continue from where it left off.
echo   - the ready marker is written only after every install step
echo     and the 3-part self-check passed.
echo.
echo Needs: local Python 3.11.x; NVIDIA GPU + working driver.
endlocal
exit /b 0

:fail_exit
echo SETUP FAIL %TARGET%
endlocal
exit /b 1
