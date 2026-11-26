@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"
set "HBW_VER="
if exist "%~dp0VERSION" set /p HBW_VER=<"%~dp0VERSION"
if not defined HBW_VER if exist "%~dp0app\VERSION" set /p HBW_VER=<"%~dp0app\VERSION"

set "HBW_SELF=%~f0"
set "TARGET_DIR=%~dp0"
rem Run from app\Uninstall_Worker.bat: the install root is one folder up (full path without "..", otherwise the steps below find nothing)
if exist "%~dp0..\HBWorker.exe" for %%I in ("%~dp0..") do set "TARGET_DIR=%%~fI\"
if "!TARGET_DIR:~-1!"=="\" set "TARGET_DIR=!TARGET_DIR:~0,-1!"
set "HBW_ROOT=!TARGET_DIR!"

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
if exist "app\config.json" (
    findstr /i /c:"\"lang\": \"en\"" app\config.json >nul 2>&1 && set "WRK_LANG=en"
    findstr /i /c:"\"lang\":\"en\"" app\config.json >nul 2>&1 && set "WRK_LANG=en"
    findstr /i /c:"\"ui_lang\": \"en\"" app\config.json >nul 2>&1 && set "WRK_LANG=en"
    findstr /i /c:"\"ui_lang\":\"en\"" app\config.json >nul 2>&1 && set "WRK_LANG=en"
    findstr /i /c:"\"lang\": \"cs\"" app\config.json >nul 2>&1 && set "WRK_LANG=cs"
    findstr /i /c:"\"lang\":\"cs\"" app\config.json >nul 2>&1 && set "WRK_LANG=cs"
    findstr /i /c:"\"ui_lang\": \"cs\"" app\config.json >nul 2>&1 && set "WRK_LANG=cs"
    findstr /i /c:"\"ui_lang\":\"cs\"" app\config.json >nul 2>&1 && set "WRK_LANG=cs"
)
if not defined WRK_LANG (
    set "HBW_PS_LANG=if ([Globalization.CultureInfo]::CurrentUICulture.TwoLetterISOLanguageName -in @('cs','sk')) { 'cs' } else { 'en' }"
    for /f "delims=" %%L in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "try{iex $env:HBW_PS_LANG}catch{}" 2^>nul') do set "WRK_LANG=%%L"
)
if not defined WRK_LANG set "WRK_LANG=en"

if /i "%~1"=="--silent" goto :do_uninstall
if /i "%~1"=="/y" goto :do_uninstall
if /i "%~1"=="-y" goto :do_uninstall

if "!WRK_LANG!"=="en" goto :prompt_en

:prompt_cs
title HB Cluster Worker - Čistá odinstalace (v!HBW_VER!)
echo ==================================================================
echo  HB CLUSTER WORKER - ČISTÁ ODINSTALACE
echo ==================================================================
echo.
set "CONFIRM="
set /p CONFIRM="Opravdu chcete odinstalovat HB Cluster Worker z tohoto PC? [A/N]: "
if /i "!CONFIRM!"=="A" goto :do_uninstall
if /i "!CONFIRM!"=="Y" goto :do_uninstall
if /i "!CONFIRM!"=="ANO" goto :do_uninstall
if /i "!CONFIRM!"=="YES" goto :do_uninstall
echo.
echo Odinstalace byla zrušena uživatelem.
exit /b 0

:prompt_en
title HB Cluster Worker - Clean Uninstall (v!HBW_VER!)
echo ==================================================================
echo  HB CLUSTER WORKER - CLEAN UNINSTALL
echo ==================================================================
echo.
set "CONFIRM="
set /p CONFIRM="Are you sure you want to uninstall HB Cluster Worker from this PC? [Y/N]: "
if /i "!CONFIRM!"=="Y" goto :do_uninstall
if /i "!CONFIRM!"=="YES" goto :do_uninstall
echo.
echo Uninstallation was cancelled by user.
exit /b 0

:do_uninstall
echo.
if "!WRK_LANG!"=="en" goto :msg_proc_en
echo [1/5] Ukončuji běžící procesy HB Workeru...
goto :kill_proc
:msg_proc_en
echo [1/5] Terminating running HB Worker processes...
:kill_proc
rem Jen procesy z TÉTO složky – worker z jiné složky (novější kopie) běží dál.
call :hbw_ps procs

if "!WRK_LANG!"=="en" goto :msg_auto_en
echo [2/5] Odebírám automatické spouštění z Windows...
goto :rm_auto
:msg_auto_en
echo [2/5] Removing Windows autostart entries...
:rm_auto
rem Automatické spouštění má pevný název: smaže se, jen když spouští TUTO instalaci (nebo už neexistující).
call :hbw_ps autostart

if "!WRK_LANG!"=="en" goto :msg_sc_en
echo [3/5] Odebírám zástupce z Plochy a nabídky Start...
goto :rm_sc
:msg_sc_en
echo [3/5] Removing Desktop and Start Menu shortcuts...
:rm_sc
call :hbw_ps shortcuts

if "!WRK_LANG!"=="en" goto :msg_cert_en
echo [4/5] Odebírám certifikát MasekLab...
goto :rm_cert
:msg_cert_en
echo [4/5] Removing MasekLab certificate...
:rm_cert
rem Certifikát zůstane, pokud je na PC ještě jiná instalace HB Workeru.
call :hbw_ps certs
call :hbw_ps registry

if "!WRK_LANG!"=="en" goto :msg_del_en
echo [5/5] Odstraňuji aplikační složku HB Workeru: !TARGET_DIR!
goto :do_del
:msg_del_en
echo [5/5] Removing HB Worker application folder: !TARGET_DIR!
:do_del

if "!WRK_LANG!"=="en" goto :done_en
echo.
echo INFO: Samotný Python 3 byl v systému ponechán pro ostatní aplikace.
echo INFO: Mažou se jen soubory HB Workeru. Složka se odstraní, jen pokud potom zůstane prázdná.
echo Hotovo! HB Worker byl úspěšně odinstalován.
goto :run_delete

:done_en
echo.
echo INFO: Python 3 was preserved on this system for other apps.
echo INFO: Only HB Worker files are removed. The folder itself is removed only if it is empty afterwards.
echo Done! HB Worker was successfully uninstalled.

:run_delete
rem Mazání probíhá až po skončení tohoto skriptu (soubor nesmí mazat sám sebe za běhu).
rem 1) smaže se jen to, co patří HB Workeru, 2) složka se odstraní bez /s, tj. jen když je prázdná.
set "LEGACY_LAYOUT=0"
if exist "!TARGET_DIR!\worker.pyw" set "LEGACY_LAYOUT=1"
set "HBW_TARGET_DIR=!TARGET_DIR!"
set "HBW_HAS_APP=0"
if exist "!TARGET_DIR!\app\worker.pyw" set "HBW_HAS_APP=1"
set "CLEAN_BAT=!TEMP!\hbw_uninstall_!RANDOM!!RANDOM!.cmd"
(
    echo @echo off
    echo setlocal DisableDelayedExpansion
    echo chcp 65001 ^>nul 2^>^&1
    echo ping 127.0.0.1 -n 3 ^>nul
    echo cd /d "%%TEMP%%"
    for %%F in (HBWorker.exe Uninstall_Worker.bat Start_Worker.bat README.txt LICENSE THIRD-PARTY-NOTICES.md VERSION update_worker.bat) do echo del /f /q "%%HBW_TARGET_DIR%%\%%F" ^>nul 2^>^&1
    echo if "%%HBW_HAS_APP%%"=="1" rmdir /s /q "%%HBW_TARGET_DIR%%\app" ^>nul 2^>^&1
    echo if "%%LEGACY_LAYOUT%%"=="1" ^(
    for %%F in (worker.pyw worker_core.py locales.js hbw.ico hbw-gradient.svg maseklab_codesign.cer config.json worker_config.json cluster_debug.log .worker_instance.json .worker_ipc_cmd.json worker.pyw.tmp worker_core.py.tmp locales.js.tmp VERSION.tmp worker.pyw.prev worker_core.py.prev locales.js.prev VERSION.prev update_attempt.json ota_ok.txt ota_pid.txt worker_identity.json manager_binding.json) do echo     del /f /q "%%HBW_TARGET_DIR%%\%%F" ^>nul 2^>^&1
    for %%D in (bin HB_Temp temp ota_cache ota_stage local_hold __pycache__) do echo     if exist "%%HBW_TARGET_DIR%%\%%D\" rmdir /s /q "%%HBW_TARGET_DIR%%\%%D" ^>nul 2^>^&1
    echo ^)
    echo rmdir "%%HBW_TARGET_DIR%%" ^>nul 2^>^&1
    echo del /f /q "%%~f0" ^>nul 2^>^&1
) > "%CLEAN_BAT%"
cd /d "!TEMP!"
start "" /min cmd.exe /c ""%CLEAN_BAT%""
exit /b 0

rem ------------------------------------------------------------------------------------------------------------
rem Kroky v PowerShellu: kód je na konci tohoto souboru za značkou (cmd.exe na něj nikdy nedojde).
:hbw_ps
set "HBW_STEP=%~1"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=[IO.File]::ReadAllText($env:HBW_SELF); $i=$s.LastIndexOf('#'+'HBPS'+'#'); if ($i -ge 0) { iex $s.Substring($i+6) }" >nul 2>&1
exit /b 0

#HBPS#
$ErrorActionPreference = 'SilentlyContinue'
$root = ([string]$env:HBW_ROOT).Trim().TrimEnd('\')
if ($env:OS -eq 'Windows_NT') { try { $root = [IO.Path]::GetFullPath($root + '\').TrimEnd('\') } catch {} }
$rootL = $root.ToLower()
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$hbwKey = 'HKCU:\Software\MasekLab\HB Cluster Worker'

function Get-Target([string]$cmd) {
    foreach ($m in [regex]::Matches([string]$cmd, '"([^"]+)"|(\S+)')) {
        $p = if ($m.Groups[1].Success) { $m.Groups[1].Value } else { $m.Groups[2].Value }
        $n = ($p -split '[\\/]')[-1].ToLower()
        if ($n -eq 'hbworker.exe' -or $n -eq 'worker.pyw' -or $n -eq 'start_worker.bat') { return $p }
    }
    return ''
}
function Test-Inside([string]$path) {
    $l = ([string]$path).ToLower()
    return ($l -eq $rootL) -or $l.StartsWith($rootL + '\')
}
# smí se smazat: vede sem, nebo na worker, který už neexistuje, nebo z něj nejde cestu vyčíst (dřívější chování)
function Test-Mine([string]$cmd) {
    $t = Get-Target $cmd
    if (-not $t) { return $true }
    if (Test-Inside $t) { return $true }
    return -not (Test-Path -LiteralPath $t)
}
function Get-RunCmd { try { return [string](Get-ItemPropertyValue -Path $runKey -Name 'HBClusterWorker' -ErrorAction Stop) } catch { return '' } }
function Get-TaskCmd {
    try {
        $a = (Get-ScheduledTask -TaskName 'HBClusterWorker' -ErrorAction Stop).Actions | Select-Object -First 1
        # Execute už může být v uvozovkách (cesta s mezerou) – jinak by vznikly dvojité a cesta by nešla přečíst
        return ('"' + ([string]$a.Execute).Trim().Trim('"') + '" ' + [string]$a.Arguments)
    } catch { return $null }
}
function Test-OtherInstall {
    $cands = @()
    try { $cands += [string](Get-ItemPropertyValue -Path $hbwKey -Name 'InstallDir' -ErrorAction Stop) } catch {}
    foreach ($c in @((Get-RunCmd), (Get-TaskCmd))) {
        $t = Get-Target $c
        if ($t) {
            $d = $t.Substring(0, [Math]::Max(0, $t.LastIndexOf('\')))
            if (($d -split '\\')[-1] -ieq 'app') { $d = $d.Substring(0, [Math]::Max(0, $d.LastIndexOf('\'))) }
            $cands += $d
        }
    }
    foreach ($c in $cands) {
        if (-not $c) { continue }
        $c = $c.TrimEnd('\')
        if ($c.ToLower() -eq $rootL) { continue }
        foreach ($f in @('HBWorker.exe', 'app\worker.pyw', 'worker.pyw')) {
            if (Test-Path -LiteralPath ($c + '\' + $f)) { return $true }
        }
    }
    return $false
}

switch ($env:HBW_STEP) {
    'procs' {
        # i worker spuštěný s relativní cestou (python worker.pyw): PID z .worker_instance.json této instalace
        $own = @()
        foreach ($f in @(($root + '\app\.worker_instance.json'), ($root + '\.worker_instance.json'))) {
            try { $own += [int]((Get-Content -LiteralPath $f -Raw | ConvertFrom-Json).pid) } catch {}
        }
        Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe' OR Name='HBWorker.exe'" |
            Where-Object { (Test-Inside (Split-Path -Parent ([string]$_.ExecutablePath))) -or
                           (([string]$_.CommandLine -like '*worker.pyw*') -and ([string]$_.CommandLine).ToLower().Contains($rootL + '\')) -or
                           (($own -contains [int]$_.ProcessId) -and (([string]$_.Name -ieq 'HBWorker.exe') -or ([string]$_.CommandLine -like '*worker.pyw*'))) } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    }
    'autostart' {
        if (Test-Mine (Get-RunCmd)) { Remove-ItemProperty -Path $runKey -Name 'HBClusterWorker' }
        $tc = Get-TaskCmd
        # úlohu nejde přečíst: smazat jen tehdy, když na PC není jiná instalace (té by mohla patřit)
        if ((($null -eq $tc) -and -not (Test-OtherInstall)) -or (($null -ne $tc) -and (Test-Mine $tc))) {
            & schtasks.exe /Query /TN HBClusterWorker 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) {
                & schtasks.exe /Delete /TN HBClusterWorker /F 2>&1 | Out-Null
                & schtasks.exe /Query /TN HBClusterWorker 2>&1 | Out-Null
                if ($LASTEXITCODE -eq 0) {
                    try { Start-Process schtasks.exe -ArgumentList '/Delete /TN "HBClusterWorker" /F' -Verb RunAs -WindowStyle Hidden -Wait } catch {}
                }
            }
        }
    }
    'shortcuts' {
        $sh = New-Object -ComObject WScript.Shell
        foreach ($l in @((Join-Path ([Environment]::GetFolderPath('Desktop')) 'HB Worker.lnk'),
                         (Join-Path ([Environment]::GetFolderPath('Programs')) 'HB Worker.lnk'))) {
            if (Test-Path -LiteralPath $l) {
                $t = [string]$sh.CreateShortcut($l).TargetPath
                if ((-not $t) -or (Test-Inside $t) -or (-not (Test-Path -LiteralPath $t))) { Remove-Item -LiteralPath $l -Force }
            }
        }
    }
    'certs' {
        if (-not (Test-OtherInstall)) {
            foreach ($st in @('Cert:\CurrentUser\Root', 'Cert:\CurrentUser\TrustedPublisher', 'Cert:\LocalMachine\Root', 'Cert:\LocalMachine\TrustedPublisher')) {
                Get-ChildItem -Path $st | Where-Object { $_.Subject -like '*MasekLab*' } | Remove-Item -Force
            }
        }
    }
    'registry' {
        try {
            $d = [string](Get-ItemPropertyValue -Path $hbwKey -Name 'InstallDir' -ErrorAction Stop)
            if ($d.TrimEnd('\').ToLower() -eq $rootL) { Remove-Item -Path $hbwKey -Recurse -Force }
        } catch {}
    }
}
