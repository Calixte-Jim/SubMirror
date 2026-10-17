@echo off
rem =============================================================================
rem  SubMirror v0.1 - portable build script (Windows)
rem
rem  Produces a self-contained folder that can be copied to any machine and run
rem  without an installer:
rem      dist\SubMirror-portable\SubMirror.exe      (plus _internal\ and the docs)
rem      dist\SubMirror-<version>-portable.zip      (unless /NOZIP is given)
rem
rem  Usage:
rem      build_portable.bat                       build + zip
rem      build_portable.bat /NOZIP                skip the zip
rem      build_portable.bat /CONSOLE              keep a console window (debugging)
rem      build_portable.bat /KEEPBUILD            reuse build\pyinstaller
rem      build_portable.bat /PYTHON "C:\...\python.exe"
rem
rem  Optional bundled payloads (environment variables; nothing is stored in the
rem  repository, so the shipped file manifest stays exactly as released):
rem      SUBMIRROR_PORTABLE_MODEL  folder holding a CTranslate2 Whisper model
rem                                -> copied to <portable>\models\faster-whisper-base
rem      SUBMIRROR_PORTABLE_EXTRAS folder whose files are copied to <portable>\
rem                                -> carries start_submirror.bat + credits
rem
rem  Prerequisites:
rem      * Python 3.10 - 3.12 with the packages from requirements.txt installed.
rem      * PyInstaller (the script tells you the exact pip command if it is missing).
rem      * The speech model is bundled when SUBMIRROR_PORTABLE_MODEL points at a
rem        model folder; start_submirror.bat then writes it into the user config.
rem
rem  Note on paths: every path below is quoted, so spaces are safe. Non-ASCII
rem  build paths (e.g. "D:\新建文件夹") are best avoided - PyInstaller and the
rem  ctranslate2 runtime have a history of problems with them. If your checkout
rem  lives under one, copy it to a plain ASCII path first.
rem
rem  Design note: the app is built with --windowed (no console window popping up
rem  over a game), but src/core/context.py::setup_console_logging() attaches to
rem  the parent console when one exists, so running the exe from a terminal still
rem  shows the log. Use /CONSOLE to get an always-on console while debugging.
rem =============================================================================

setlocal

pushd "%~dp0"

set "APP_NAME=SubMirror"
if not defined PYTHON set "PYTHON=python"
set "DIST_DIR=dist"
set "WORK_DIR=build\pyinstaller"
set "PORTABLE_DIR=dist\SubMirror-portable"
set "MAKE_ZIP=1"
set "CLEAN_BUILD=1"
set "EXIT_CODE=0"
set "WINDOWED=--windowed"
set "COLLECT="
set "EXCLUDES=--exclude-module tkinter --exclude-module matplotlib --exclude-module torch --exclude-module torchaudio --exclude-module torchvision --exclude-module pytest --exclude-module IPython --exclude-module notebook"
set "ADD_DATA="

rem -----------------------------------------------------------------------------
rem Command line
rem -----------------------------------------------------------------------------
:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="/?" goto usage
if /i "%~1"=="/PYTHON" (set "PYTHON=%~2" & shift & shift & goto parse_args)
if /i "%~1"=="/NOZIP" (set "MAKE_ZIP=0" & shift & goto parse_args)
if /i "%~1"=="/KEEPBUILD" (set "CLEAN_BUILD=0" & shift & goto parse_args)
if /i "%~1"=="/CONSOLE" (set "WINDOWED=" & shift & goto parse_args)
echo [WARN] Unknown option ignored: %~1
shift
goto parse_args
:args_done

rem -----------------------------------------------------------------------------
rem [1/8] Workspace sanity
rem -----------------------------------------------------------------------------
echo [1/8] Checking workspace...
if not exist "main.py" goto no_main
if not exist "src\core\config.py" goto no_src

set "APP_VERSION=0.0.0"
for /f "usebackq tokens=1,2 delims== " %%a in (`findstr /b /c:"APP_VERSION" "src\core\config.py"`) do set "APP_VERSION=%%~b"
echo       SubMirror %APP_VERSION%

rem -----------------------------------------------------------------------------
rem [2/8] Interpreter
rem -----------------------------------------------------------------------------
echo [2/8] Locating the Python interpreter...
"%PYTHON%" -c "import sys;print('      '+' '.join(sys.version.split()[:3]))"
if errorlevel 1 goto no_python

rem -----------------------------------------------------------------------------
rem [3/8] Dependencies
rem -----------------------------------------------------------------------------
echo [3/8] Checking runtime dependencies...
"%PYTHON%" -c "import importlib.util as u,sys;sys.exit(0 if u.find_spec('PySide6') else 1)"
if errorlevel 1 goto no_pyside

"%PYTHON%" -c "import importlib.util as u;mods=[('numpy (required for audio math)','numpy'),('PyAudioWPatch (WASAPI loopback)','pyaudiowpatch'),('soundcard (fallback backend)','soundcard'),('faster_whisper (speech to text)','faster_whisper'),('winsdk (Windows OCR / WinRT)','winsdk'),('mss (screen backend)','mss'),('requests (HTTP transport)','requests')];[print('      {:<38} {}'.format(l,'OK' if u.find_spec(m) else '- missing -')) for l,m in mods]"

rem --- extra payloads for packages PyInstaller cannot always discover alone ----
set "COLLECT=--collect-all pyaudiowpatch"
"%PYTHON%" -c "import importlib.util as u,sys;sys.exit(0 if u.find_spec('faster_whisper') else 1)"
if not errorlevel 1 set "COLLECT=%COLLECT% --collect-all faster_whisper"
"%PYTHON%" -c "import importlib.util as u,sys;sys.exit(0 if u.find_spec('soundcard') else 1)"
if not errorlevel 1 set "COLLECT=%COLLECT% --collect-all soundcard"
"%PYTHON%" -c "import importlib.util as u,sys;sys.exit(0 if u.find_spec('winsdk') else 1)"
if not errorlevel 1 set "COLLECT=%COLLECT% --collect-all winsdk --collect-all winrt"
if exist "assets" set "ADD_DATA=--add-data "%CD%\assets;assets""

rem -----------------------------------------------------------------------------
rem [4/8] PyInstaller
rem -----------------------------------------------------------------------------
echo [4/8] Checking PyInstaller...
"%PYTHON%" -m PyInstaller --version >nul 2>&1
if errorlevel 1 goto no_pyinstaller

rem -----------------------------------------------------------------------------
rem [5/8] Clean previous output
rem -----------------------------------------------------------------------------
echo [5/8] Cleaning previous build output...
if "%CLEAN_BUILD%"=="1" (
  if exist "%WORK_DIR%" (
    echo       removing %WORK_DIR%
    rmdir /s /q "%WORK_DIR%"
  )
)
if exist "%PORTABLE_DIR%" (
  echo       removing %PORTABLE_DIR%
  rmdir /s /q "%PORTABLE_DIR%"
)
if exist "%DIST_DIR%\%APP_NAME%" (
  echo       removing %DIST_DIR%\%APP_NAME%
  rmdir /s /q "%DIST_DIR%\%APP_NAME%"
)

rem -----------------------------------------------------------------------------
rem [6/8] Build
rem -----------------------------------------------------------------------------
echo [6/8] Running PyInstaller (this can take several minutes)...
"%PYTHON%" -m PyInstaller ^
  --noconfirm --clean --log-level WARN ^
  --name "%APP_NAME%" --distpath "%DIST_DIR%" --workpath "%WORK_DIR%" --specpath "build" ^
  %WINDOWED% %EXCLUDES% %COLLECT% %ADD_DATA% ^
  main.py
if errorlevel 1 goto build_failed
if not exist "%DIST_DIR%\%APP_NAME%\%APP_NAME%.exe" goto no_exe

rem -----------------------------------------------------------------------------
rem [7/8] Assemble the portable folder
rem -----------------------------------------------------------------------------
echo [7/8] Assembling the portable folder...
rem robocopy exit codes 0-7 mean success; 8 and above are real failures.
robocopy "%DIST_DIR%\%APP_NAME%" "%PORTABLE_DIR%" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto no_copy

rem The MIT licence must travel with the binaries; the rest is for the user.
for %%f in (README.md LICENSE PRIVACY.md requirements.txt) do if exist "%%f" copy /y "%%f" "%PORTABLE_DIR%" >nul

rem Optional bundled speech model. Keeping it outside the repository means the
rem source tree stays identical to the released manifest; the build only copies.
if defined SUBMIRROR_PORTABLE_MODEL (
  if exist "%SUBMIRROR_PORTABLE_MODEL%\config.json" (
    echo       bundling speech model from %SUBMIRROR_PORTABLE_MODEL%
    robocopy "%SUBMIRROR_PORTABLE_MODEL%" "%PORTABLE_DIR%\models\faster-whisper-base" /E /NFL /NDL /NJH /NJS /NP >nul
    if errorlevel 8 goto no_copy
  ) else (
    echo [WARN] SUBMIRROR_PORTABLE_MODEL has no config.json, skipped: %SUBMIRROR_PORTABLE_MODEL%
  )
)

rem Voice-activity model (Silero VAD, MIT). It is small enough to live in the
rem source tree, so the portable build simply carries it along:
rem models\silero_vad.onnx -> <portable>\models\silero_vad.onnx
if exist "models\silero_vad.onnx" (
  echo       bundling voice-activity model
  if not exist "%PORTABLE_DIR%\models" mkdir "%PORTABLE_DIR%\models" >nul 2>&1
  copy /y "models\silero_vad.onnx" "%PORTABLE_DIR%\models\silero_vad.onnx" >nul
  if errorlevel 1 goto no_copy
) else (
  echo [WARN] models\silero_vad.onnx not found; voice-activity detection falls back
)

if defined SUBMIRROR_PORTABLE_EXTRAS (
  if exist "%SUBMIRROR_PORTABLE_EXTRAS%\*" (
    echo       bundling extras from %SUBMIRROR_PORTABLE_EXTRAS%
    robocopy "%SUBMIRROR_PORTABLE_EXTRAS%" "%PORTABLE_DIR%" /E /XF README.md /NFL /NDL /NJH /NJS /NP >nul
    if errorlevel 8 goto no_copy
  )
)

rem Portable mode only becomes meaningful when the application is built for it.
findstr /r /c:"^PORTABLE_MODE_ENABLED *= *True" "src\core\config.py" >nul 2>&1
if errorlevel 1 goto marker_skip
echo. > "%PORTABLE_DIR%\%APP_NAME%.portable"
echo       portable marker written (%APP_NAME%.portable)
goto marker_done
:marker_skip
echo       portable marker skipped: PORTABLE_MODE_ENABLED is False in src\core\config.py
echo       (config will live in %%APPDATA%%\SubMirror; set it to True to change that)
:marker_done

if "%MAKE_ZIP%"=="0" goto zip_skip
echo       compressing the portable folder...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path '%PORTABLE_DIR%' -DestinationPath '%DIST_DIR%\%APP_NAME%-%APP_VERSION%-portable.zip' -Force"
if errorlevel 1 goto zip_failed
:zip_skip

rem -----------------------------------------------------------------------------
rem [8/8] Summary
rem -----------------------------------------------------------------------------
echo [8/8] Done.
echo.
echo   Portable folder : %CD%\%PORTABLE_DIR%
echo   Executable      : %CD%\%PORTABLE_DIR%\%APP_NAME%.exe
if "%MAKE_ZIP%"=="1" echo   Archive         : %CD%\%DIST_DIR%\%APP_NAME%-%APP_VERSION%-portable.zip
echo.
echo   便携版说明 / Portable notes:
echo     * Copy the whole folder - '%APP_NAME%.exe' alone will not run.
if defined SUBMIRROR_PORTABLE_MODEL (
  echo     * A Whisper model ships in 'models\faster-whisper-base' and is picked
  echo       up automatically; 'start_submirror.bat' is optional - it writes
  echo       asr.model_path first, then starts the app.
) else (
  echo     * No speech model is bundled. Point the settings panel at an existing
  echo       model folder on first run.
)
echo     * OCR uses the OCR engine built into Windows (WinRT, package 'winsdk');
echo       install the matching Windows language pack, otherwise OCR is disabled
echo       and audio subtitles keep working.
echo     * User config lives in %%APPDATA%%\SubMirror\config.json.
echo.
popd
endlocal & exit /b %EXIT_CODE%

rem =============================================================================
rem Error handlers
rem =============================================================================
:usage
echo SubMirror portable build
echo.
echo   build_portable.bat [/NOZIP] [/CONSOLE] [/KEEPBUILD] [/PYTHON ^<path^>]
echo.
echo   /NOZIP      do not create the .zip archive
echo   /CONSOLE    build with a console window (debugging)
echo   /KEEPBUILD  keep build\pyinstaller from the previous run
echo   /PYTHON     path to the Python interpreter to use
echo.
echo   Environment variables (optional bundled payloads):
echo     SUBMIRROR_PORTABLE_MODEL   CTranslate2 Whisper model folder to bundle
echo     SUBMIRROR_PORTABLE_EXTRAS  folder copied into the portable root
popd
endlocal
exit /b 0

:no_main
echo [ERROR] main.py not found. Run this script from the SubMirror repository root.
goto fail

:no_src
echo [ERROR] src\core\config.py not found. This is not the full SubMirror source tree.
goto fail

:no_python
echo [ERROR] "%PYTHON%" is not a working Python interpreter.
echo         Install Python 3.10-3.12, or pass the path explicitly:
echo             build_portable.bat /PYTHON "C:\Python311\python.exe"
goto fail

:no_pyside
echo [ERROR] PySide6 is missing - it is the GUI layer and cannot be skipped.
echo             "%PYTHON%" -m pip install -r requirements.txt
goto fail

:no_pyinstaller
echo [ERROR] PyInstaller is not installed for this interpreter.
echo             "%PYTHON%" -m pip install --upgrade pyinstaller
goto fail

:build_failed
echo [ERROR] PyInstaller failed. Scroll up for the first error it printed.
goto fail

:no_exe
echo [ERROR] Build finished but %DIST_DIR%\%APP_NAME%\%APP_NAME%.exe is missing.
echo         Check the PyInstaller output above (usually a missing hidden import).
goto fail

:no_copy
echo [ERROR] robocopy failed while assembling the portable folder.
goto fail

:zip_failed
set "EXIT_CODE=1"
echo [WARN] The archive could not be created; the portable folder itself is complete.
echo        On very large builds Compress-Archive can hit its own limits - zip the
echo        folder manually if needed.
goto zip_skip

:fail
popd
endlocal
exit /b 1
