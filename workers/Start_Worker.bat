@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul 2>&1
set "HBW_VER="
if exist "%~dp0VERSION" set /p HBW_VER=<"%~dp0VERSION"
if not defined HBW_VER if exist "%~dp0app\VERSION" set /p HBW_VER=<"%~dp0app\VERSION"
title HB Worker Launcher (v!HBW_VER!)

REM 1. Odblokovat stazene soubory (odstrani Zone.Identifier / Mark-of-the-Web)
set "HB_UNBLOCK_DIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath $env:HB_UNBLOCK_DIR -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue" >nul 2>&1

REM 2. Zjistit spravny adresar skriptu (app\ pokud existuje, jinak aktualni slozka)
set "APP_DIR=%~dp0"
if exist "%~dp0app\worker.pyw" set "APP_DIR=%~dp0app\"
cd /d "%APP_DIR%"

REM 3. Vyhledat nadrazeny nebo lokalni HBWorker.exe
set "HB_EXE="
if exist "%APP_DIR%..\HBWorker.exe" set "HB_EXE=%APP_DIR%..\HBWorker.exe"
if exist "%APP_DIR%HBWorker.exe" set "HB_EXE=%APP_DIR%HBWorker.exe"
if exist "%~dp0HBWorker.exe" set "HB_EXE=%~dp0HBWorker.exe"

set "BOOT_ARG=%~1"
set "PY_EXE="
set "WRK_LANG="
if exist "config.json" (
    findstr /i /c:"\"lang\": \"en\"" config.json >nul 2>&1 && set "WRK_LANG=en"
    findstr /i /c:"\"lang\":\"en\"" config.json >nul 2>&1 && set "WRK_LANG=en"
    findstr /i /c:"\"ui_lang\": \"en\"" config.json >nul 2>&1 && set "WRK_LANG=en"
    findstr /i /c:"\"ui_lang\":\"en\"" config.json >nul 2>&1 && set "WRK_LANG=en"
    findstr /i /c:"\"lang\": \"cs\"" config.json >nul 2>&1 && set "WRK_LANG=cs"
    findstr /i /c:"\"lang\":\"cs\"" config.json >nul 2>&1 && set "WRK_LANG=cs"
    findstr /i /c:"\"ui_lang\": \"cs\"" config.json >nul 2>&1 && set "WRK_LANG=cs"
    findstr /i /c:"\"ui_lang\":\"cs\"" config.json >nul 2>&1 && set "WRK_LANG=cs"
)
if not defined WRK_LANG (
    set "HBW_PS_LANG=if ([Globalization.CultureInfo]::CurrentUICulture.TwoLetterISOLanguageName -in @('cs','sk')) { 'cs' } else { 'en' }"
    for /f "delims=" %%L in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "try{iex $env:HBW_PS_LANG}catch{}" 2^>nul') do set "WRK_LANG=%%L"
)
if not defined WRK_LANG set "WRK_LANG=en"

REM 4. Detekce existujiciho funkcniho Pythonu 3.10+
REM A. Zkusit config.json (cesta obsahuje C:\ a muze obsahovat mezery i diakritiku)
if not exist "config.json" goto :check_path
set "HBW_PS_PY=$j = ConvertFrom-Json ([IO.File]::ReadAllText('config.json')); if ($j.python_path) { [string]$j.python_path }"
for /f "delims=" %%a in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "try{iex $env:HBW_PS_PY}catch{}" 2^>nul') do (
    set "cand=%%a"
    if exist "!cand!" (
        echo "!cand!" | findstr /i "WindowsApps" >nul
        if !ERRORLEVEL! NEQ 0 (
            "!cand!" -c "import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
            if !ERRORLEVEL! EQU 0 (
                set "PY_EXE=!cand!"
                goto :python_ready
            )
        )
    )
)

:check_path
for /f "delims=" %%p in ('where python.exe 2^>nul') do (
    set "cand=%%p"
    echo "!cand!" | findstr /i "WindowsApps" >nul
    if !ERRORLEVEL! NEQ 0 (
        if exist "!cand!" (
            "!cand!" -c "import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
            if !ERRORLEVEL! EQU 0 (
                set "PY_EXE=!cand!"
                goto :python_ready
            )
        )
    )
)

:check_dirs
for %%v in (14 13 12 11 10) do (
    if exist "%LOCALAPPDATA%\Programs\Python\Python3%%v\python.exe" (
        "%LOCALAPPDATA%\Programs\Python\Python3%%v\python.exe" -c "import sys, tkinter; sys.exit(0)" >nul 2>&1
        if !ERRORLEVEL! EQU 0 (
            set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python3%%v\python.exe"
            goto :python_ready
        )
    )
    if exist "%ProgramFiles%\Python3%%v\python.exe" (
        "%ProgramFiles%\Python3%%v\python.exe" -c "import sys, tkinter; sys.exit(0)" >nul 2>&1
        if !ERRORLEVEL! EQU 0 (
            set "PY_EXE=%ProgramFiles%\Python3%%v\python.exe"
            goto :python_ready
        )
    )
    if exist "C:\Python3%%v\python.exe" (
        "C:\Python3%%v\python.exe" -c "import sys, tkinter; sys.exit(0)" >nul 2>&1
        if !ERRORLEVEL! EQU 0 (
            set "PY_EXE=C:\Python3%%v\python.exe"
            goto :python_ready
        )
    )
)

:check_py_launcher
where py.exe >nul 2>&1
if !ERRORLEVEL! NEQ 0 goto :install_python
for /f "delims=" %%p in ('py.exe -3 -c "import sys; print(sys.executable)" 2^>nul') do (
    set "cand=%%p"
    echo "!cand!" | findstr /i "WindowsApps" >nul
    if !ERRORLEVEL! NEQ 0 (
        if exist "!cand!" (
            "!cand!" -c "import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
            if !ERRORLEVEL! EQU 0 (
                set "PY_EXE=!cand!"
                goto :python_ready
            )
        )
    )
)

:install_python
if "!WRK_LANG!"=="en" goto :install_python_en
echo ============================================================================
echo   HandBrake Cluster Worker v!HBW_VER! - Příprava prostředí Python
echo ============================================================================
echo.
echo Python 3 nebyl nalezen. Zahajuji automatickou instalaci Pythonu...
echo.

where winget.exe >nul 2>&1
if !ERRORLEVEL! NEQ 0 goto :download_python
echo [1/2] Instaluji Python 3.13 přes winget...
winget.exe install --id Python.Python.3.13 --exact --source winget --accept-source-agreements --accept-package-agreements

for %%v in (14 13 12 11 10) do (
    if exist "%LOCALAPPDATA%\Programs\Python\Python3%%v\python.exe" (
        "%LOCALAPPDATA%\Programs\Python\Python3%%v\python.exe" -c "import sys, tkinter; sys.exit(0)" >nul 2>&1
        if !ERRORLEVEL! EQU 0 (
            set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python3%%v\python.exe"
            goto :python_ready
        )
    )
)

:download_python
echo [2/2] Hledám nejnovější Python 3.14 / 3.13 s platným podpisem na python.org...
REM Nejnovejsi vydani rad 3.14 / 3.13 s instalatorem pro Windows; spusti se jen s platnym podpisem
REM Python Software Foundation (jinak zkusi starsi vydani, nakonec 3.12.10). Bez pevne verze = nezastara.
set "INST_EXE=%TEMP%\hb-python-installer.exe"
if exist "!INST_EXE!" del "!INST_EXE!" >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $base='https://www.python.org/ftp/python/'; $out=$env:INST_EXE; $vers=@(); try { $html=(Invoke-WebRequest -UseBasicParsing -Uri $base -TimeoutSec 30).Content; $vers=@([regex]::Matches($html,'href=.(3\.1[34]\.[0-9]+)/') | ForEach-Object { [version]$_.Groups[1].Value } | Sort-Object -Descending -Unique | Select-Object -First 8) } catch { }; $vers += [version]'3.12.10'; foreach ($v in $vers) { $u=$base+$v+'/python-'+$v+'-amd64.exe'; try { Invoke-WebRequest -UseBasicParsing -Uri $u -OutFile $out -TimeoutSec 900; $s=Get-AuthenticodeSignature -LiteralPath $out; if ($s.Status -eq 'Valid' -and $s.SignerCertificate.Subject -match '(^|,\s*)O=(")?Python Software Foundation\2(,|$)') { Write-Host ('Python '+$v); exit 0 } } catch { }; Remove-Item -LiteralPath $out -Force -ErrorAction SilentlyContinue }; exit 1"
if !ERRORLEVEL! NEQ 0 goto :install_sig_bad

echo Spouštím instalaci Pythonu...
start /wait "" "!INST_EXE!" /passive InstallAllUsers=0 PrependPath=1 Include_test=0 Include_tcltk=1 Include_pip=1
del "!INST_EXE!" >nul 2>&1

for %%v in (14 13 12 11 10) do (
    if exist "%LOCALAPPDATA%\Programs\Python\Python3%%v\python.exe" (
        "%LOCALAPPDATA%\Programs\Python\Python3%%v\python.exe" -c "import sys, tkinter; sys.exit(0)" >nul 2>&1
        if !ERRORLEVEL! EQU 0 (
            set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python3%%v\python.exe"
            goto :python_ready
        )
    )
)

:install_failed
echo.
echo [CHYBA] Python 3 se nepodařilo automaticky nalézt ani nainstalovat.
echo Nainstalujte prosím Python 3.10+ ručně z https://www.python.org (zaškrtněte 'Add python.exe to PATH').
echo.
echo Stiskněte libovolnou klávesu pro pokračování . . .
pause >nul
exit /b 1

:install_sig_bad
del "!INST_EXE!" >nul 2>&1
echo.
echo [CHYBA] Nepodařilo se stáhnout instalátor Pythonu s platným podpisem Python Software Foundation – nic se nespustilo.
echo Zkuste to znovu později, nebo nainstalujte Python 3.10+ ručně z https://www.python.org.
goto :install_failed

:install_python_en
echo ============================================================================
echo   HandBrake Cluster Worker v!HBW_VER! - Python Environment Setup
echo ============================================================================
echo.
echo Python 3 was not found. Starting automatic installation of Python...
echo.

where winget.exe >nul 2>&1
if !ERRORLEVEL! NEQ 0 goto :download_python_en
echo [1/2] Installing Python 3.13 via winget...
winget.exe install --id Python.Python.3.13 --exact --source winget --accept-source-agreements --accept-package-agreements

for %%v in (14 13 12 11 10) do (
    if exist "%LOCALAPPDATA%\Programs\Python\Python3%%v\python.exe" (
        "%LOCALAPPDATA%\Programs\Python\Python3%%v\python.exe" -c "import sys, tkinter; sys.exit(0)" >nul 2>&1
        if !ERRORLEVEL! EQU 0 (
            set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python3%%v\python.exe"
            goto :python_ready
        )
    )
)

:download_python_en
echo [2/2] Looking for the newest Python 3.14 / 3.13 with a valid signature on python.org...
REM Newest 3.14 / 3.13 release with a Windows installer; runs only with a valid Python Software Foundation
REM signature (otherwise an older release, finally 3.12.10). No fixed version = never goes stale.
set "INST_EXE=%TEMP%\hb-python-installer.exe"
if exist "!INST_EXE!" del "!INST_EXE!" >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $base='https://www.python.org/ftp/python/'; $out=$env:INST_EXE; $vers=@(); try { $html=(Invoke-WebRequest -UseBasicParsing -Uri $base -TimeoutSec 30).Content; $vers=@([regex]::Matches($html,'href=.(3\.1[34]\.[0-9]+)/') | ForEach-Object { [version]$_.Groups[1].Value } | Sort-Object -Descending -Unique | Select-Object -First 8) } catch { }; $vers += [version]'3.12.10'; foreach ($v in $vers) { $u=$base+$v+'/python-'+$v+'-amd64.exe'; try { Invoke-WebRequest -UseBasicParsing -Uri $u -OutFile $out -TimeoutSec 900; $s=Get-AuthenticodeSignature -LiteralPath $out; if ($s.Status -eq 'Valid' -and $s.SignerCertificate.Subject -match '(^|,\s*)O=(")?Python Software Foundation\2(,|$)') { Write-Host ('Python '+$v); exit 0 } } catch { }; Remove-Item -LiteralPath $out -Force -ErrorAction SilentlyContinue }; exit 1"
if !ERRORLEVEL! NEQ 0 goto :install_sig_bad_en

echo Starting the Python installation...
start /wait "" "!INST_EXE!" /passive InstallAllUsers=0 PrependPath=1 Include_test=0 Include_tcltk=1 Include_pip=1
del "!INST_EXE!" >nul 2>&1

for %%v in (14 13 12 11 10) do (
    if exist "%LOCALAPPDATA%\Programs\Python\Python3%%v\python.exe" (
        "%LOCALAPPDATA%\Programs\Python\Python3%%v\python.exe" -c "import sys, tkinter; sys.exit(0)" >nul 2>&1
        if !ERRORLEVEL! EQU 0 (
            set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python3%%v\python.exe"
            goto :python_ready
        )
    )
)

:install_failed_en
echo.
echo [ERROR] Python 3 could not be found or installed automatically.
echo Please install Python 3.10+ manually from https://www.python.org (check 'Add python.exe to PATH').
echo.
echo Press any key to continue . . .
pause >nul
exit /b 1

:install_sig_bad_en
del "!INST_EXE!" >nul 2>&1
echo.
echo [ERROR] Could not download a Python installer with a valid Python Software Foundation signature - nothing was run.
echo Please try again later, or install Python 3.10+ manually from https://www.python.org.
goto :install_failed_en

:python_ready
set "SAVECFG=import json, os; p='config.json'; d=json.load(open(p,'r',encoding='utf-8-sig')) if os.path.isfile(p) else {}; d['python_path']=os.environ.get('PY_EXE'); json.dump(d,open(p,'w',encoding='utf-8'),indent=4,ensure_ascii=False)"
"!PY_EXE!" -c "%SAVECFG%" >nul 2>&1

REM 5. Kontrola a instalace potřebných knihoven (requests, psutil, pillow) s živým výpisem průběhu
"!PY_EXE!" -c "import requests, psutil, PIL" >nul 2>&1
if !ERRORLEVEL! EQU 0 goto :skip_pkg_install

echo.
if "!WRK_LANG!"=="en" goto :install_pkgs_en
echo ============================================================================
echo   Instalace potřebných knihoven Pythonu (requests, psutil, pillow)...
echo ============================================================================
echo.
"!PY_EXE!" -m pip install --disable-pip-version-check requests psutil pillow
if !ERRORLEVEL! NEQ 0 "!PY_EXE!" -m pip install --user --disable-pip-version-check requests psutil pillow
echo.
goto :skip_pkg_install

:install_pkgs_en
echo ============================================================================
echo   Installing required Python packages (requests, psutil, pillow)...
echo ============================================================================
echo.
"!PY_EXE!" -m pip install --disable-pip-version-check requests psutil pillow
if !ERRORLEVEL! NEQ 0 "!PY_EXE!" -m pip install --user --disable-pip-version-check requests psutil pillow
echo.

:skip_pkg_install
if "%BOOT_ARG%"=="--install-python" exit /b 0
if "%BOOT_ARG%"=="--auto-install-python" exit /b 0

if defined HB_EXE (
    if exist "%HB_EXE%" (
        start "" "%HB_EXE%" --from-bat
        exit /b 0
    )
)

for %%i in ("!PY_EXE!") do set "PY_DIR=%%~dpi"
set "PYW_EXE=%PY_DIR%pythonw.exe"
if not exist "!PYW_EXE!" set "PYW_EXE=!PY_EXE!"

rem Full path to worker.pyw: the uninstaller finds the running worker by its folder
start "" "!PYW_EXE!" "!APP_DIR!worker.pyw"
exit /b 0
