@echo off
setlocal enabledelayedexpansion

REM Get the script directory and project root
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"
cd ..
set "PROJECT_ROOT=%CD%"
cd /d "%SCRIPT_DIR%"
set "PATHS_FILE=%SCRIPT_DIR%PATHS.txt"

REM Load paths from PATHS.txt (if it exists)
if exist "%PATHS_FILE%" (
    for /f "usebackq tokens=1,* delims==" %%a in ("%PATHS_FILE%") do (
        REM Skip empty lines and comments (lines starting with #)
        if not "%%a"=="" if not "%%a"==" " (
            set "LINE_START=%%a"
            set "LINE_START=!LINE_START:~0,1!"
            if not "!LINE_START!"=="#" (
                set "%%a=%%b"
            )
        )
    )
    
    REM Expand variables in paths (replace ${VAR} with actual values)
    if defined PROJECT_ROOT (
        if defined SERVER_SCRIPT set "SERVER_SCRIPT=!SERVER_SCRIPT:${PROJECT_ROOT}=%PROJECT_ROOT%!"
        if defined CONFIG_DIR set "CONFIG_DIR=!CONFIG_DIR:${PROJECT_ROOT}=%PROJECT_ROOT%!"
        if defined LOG_DIR set "LOG_DIR=!LOG_DIR:${PROJECT_ROOT}=%PROJECT_ROOT%!"
        if defined VENV_DIR set "VENV_DIR=!VENV_DIR:${PROJECT_ROOT}=%PROJECT_ROOT%!"
    )
)

REM Initialize variables
set "INSTALL_MODE=0"
set "LAUNCH_MODE=0"

REM Parse command line arguments
if "%1"=="" goto show_help
if "%1"=="--help" goto show_help

REM Parse arguments (supports --install, --launch, or both)
if "%1"=="--install" set INSTALL_MODE=1
if "%1"=="--launch" set LAUNCH_MODE=1
if "%2"=="--install" set INSTALL_MODE=1
if "%2"=="--launch" set LAUNCH_MODE=1
REM Check if at least one option is selected
if "%INSTALL_MODE%"=="0" if "%LAUNCH_MODE%"=="0" (
    echo ERROR: No option specified.
    echo.
    goto show_help
)

echo ========================================
echo NautilusTrader Windows Build Script
echo ========================================
echo Project Root: %PROJECT_ROOT%
echo.

REM Check for Python (use explicit path from PATHS.txt if provided)
if defined PYTHON_EXE (
    if exist "!PYTHON_EXE!" (
        set "PYTHON_CMD=!PYTHON_EXE!"
        echo Using Python from PATHS.txt: !PYTHON_EXE!
    ) else (
        echo WARNING: PYTHON_EXE in PATHS.txt not found: !PYTHON_EXE!
        echo Falling back to PATH lookup...
        set "PYTHON_CMD=python"
    )
) else (
    set "PYTHON_CMD=python"
)

REM Check for Python
where !PYTHON_CMD! >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Please install Python 3.12+ and add it to PATH, or set PYTHON_EXE in PATHS.txt
    exit /b 1
)
!PYTHON_CMD! --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python found but --version failed
    exit /b 1
)

REM Check for uv (use explicit path from PATHS.txt if provided)
set "USE_UV=0"
if defined UV_EXE (
    if exist "!UV_EXE!" (
        set "UV_CMD=!UV_EXE!"
        echo Found uv from PATHS.txt: !UV_EXE!
        set "USE_UV=1"
    ) else (
        echo WARNING: UV_EXE in PATHS.txt not found: !UV_EXE!
        echo Falling back to PATH lookup...
        where uv >nul 2>&1
        if not errorlevel 1 (
            set "UV_CMD=uv"
            set "USE_UV=1"
        )
    )
) else (
    where uv >nul 2>&1
    if not errorlevel 1 (
        set "UV_CMD=uv"
        echo Found uv package manager
        set "USE_UV=1"
    )
)

if !USE_UV!==0 (
    echo WARNING: uv not found in PATH. Attempting to use Python/pip instead...
)

echo(

REM Install NautilusTrader
if !INSTALL_MODE!==1 goto do_install
goto skip_install

:do_install
echo [INSTALL] Installing NautilusTrader...
echo.

if !USE_UV!==1 goto install_uv
goto install_pip

:install_uv
echo Using uv to install from source...
cd /d "%PROJECT_ROOT%"
!UV_CMD! sync --active --all-groups --all-extras --verbose
if errorlevel 1 (
    echo ERROR: Failed to install NautilusTrader using uv
    exit /b 1
)
echo.
echo NautilusTrader installed successfully using uv
goto install_done

:install_pip
echo Using pip to install from PyPI...
echo Attempting to upgrade pip (may fail due to permissions, continuing anyway)...
!PYTHON_CMD! -m pip install --upgrade pip --user >nul 2>&1
!PYTHON_CMD! -m pip install --upgrade pip >nul 2>&1

echo Installing NautilusTrader with --user flag to avoid permission issues...
!PYTHON_CMD! -m pip install --user nautilus_trader
if errorlevel 1 (
    echo WARNING: Installation with --user flag failed, trying without --user flag...
    !PYTHON_CMD! -m pip install nautilus_trader
    if errorlevel 1 (
        echo ERROR: Failed to install NautilusTrader using pip
        exit /b 1
    )
)

echo Verifying installation...
!PYTHON_CMD! -c "import nautilus_trader" 2>nul
if errorlevel 1 (
    echo ERROR: Installation completed but nautilus_trader cannot be imported
    echo This may indicate a corrupted Python installation or missing dependencies
    exit /b 1
)
echo NautilusTrader module imported successfully

echo.
echo NautilusTrader installed successfully using pip

:install_done
echo.
echo Installation complete!
echo.

:skip_install

REM Launch NautilusTrader
if !LAUNCH_MODE!==1 goto do_launch
goto skip_launch

:do_launch
echo [LAUNCH] Launching NautilusTrader...
echo.

REM Check if installation is required first
!PYTHON_CMD! -c "import nautilus_trader" >nul 2>&1
if errorlevel 1 (
    echo ERROR: NautilusTrader is not installed. Please run with --install first.
    exit /b 1
)

REM Check if server script is defined in PATHS.txt
if defined SERVER_SCRIPT (
    if exist "!SERVER_SCRIPT!" (
        echo Starting NautilusTrader server from: !SERVER_SCRIPT!
        echo.
        if defined CONFIG_DIR (
            if exist "!CONFIG_DIR!" (
                echo Using config directory: !CONFIG_DIR!
            )
        )
        if defined LOG_DIR (
            if not exist "!LOG_DIR!" mkdir "!LOG_DIR!" 2>nul
            echo Logs will be written to: !LOG_DIR!
        )
        echo.
        REM Launch the server script
        !PYTHON_CMD! "!SERVER_SCRIPT!"
        goto launch_done
    ) else (
        echo WARNING: SERVER_SCRIPT in PATHS.txt not found: !SERVER_SCRIPT!
        echo Falling back to placeholder mode...
    )
)

REM Placeholder mode - server script not configured
echo NautilusTrader is ready to receive trade data from MT5.
echo.
echo NOTE: To launch a server, set SERVER_SCRIPT in PATHS.txt
echo       pointing to your server script that receives MT5 trade data.
echo.
echo For now, you can:
echo   1. Use the Python API to create a custom server/daemon
echo   2. Use the CLI tool: nautilus --help
echo   3. Run examples: !PYTHON_CMD! -m nautilus_trader.examples.backtest
echo.
echo Example PATHS.txt entries:
echo   SERVER_SCRIPT=${PROJECT_ROOT}\server\mt5_trade_logger.py
echo   CONFIG_DIR=${PROJECT_ROOT}\config
echo   LOG_DIR=${PROJECT_ROOT}\logs
echo.

:launch_done
:skip_launch

echo ========================================
echo Operation completed successfully!
echo ========================================
exit /b 0

:show_help
echo NautilusTrader Windows Build Script
echo.
echo Usage: build.bat [OPTIONS]
echo.
echo Options:
echo   --help          Show this help message
echo   --install       Install NautilusTrader (from source using uv, or from PyPI using pip)
echo   --launch        Launch NautilusTrader (placeholder for MT5 trade logging integration)
echo.
echo Examples:
echo   build.bat --install
echo   build.bat --launch
echo   build.bat --install --launch
echo.
echo Notes:
echo   - Installation prefers uv if available, otherwise falls back to pip
echo   - Launch mode requires NautilusTrader to be installed first
echo   - This script is designed for Windows MT5 integration
echo   - Paths are configured in: PATHS.txt
echo     (Edit PATHS.txt to set PYTHON_EXE, UV_EXE, SERVER_SCRIPT, etc.)
echo.
exit /b 0

