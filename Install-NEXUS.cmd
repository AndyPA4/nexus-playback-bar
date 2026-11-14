@echo off
setlocal
where py.exe >nul 2>&1
if errorlevel 1 goto :python_missing
for /f "delims=" %%P in ('py.exe -3 -c "import pathlib, sys, tkinter; print(pathlib.Path(sys.executable).with_name('pythonw.exe'))"') do set "NEXUS_PYTHONW=%%P"
if not defined NEXUS_PYTHONW goto :python_missing
if not exist "%NEXUS_PYTHONW%" goto :python_missing

set "NEXUS_APP_DIR=%LOCALAPPDATA%\NexusPlaybackBar\app"
if not exist "%NEXUS_APP_DIR%\" mkdir "%NEXUS_APP_DIR%"
if not exist "%NEXUS_APP_DIR%\" goto :failed
for %%F in (nexus-playback.pyw nexus.py tray.py startup.py preferences.py npb.ico bridge-key.txt NexusTool-LICENSE.txt) do (
    copy /y "%~dp0%%F" "%NEXUS_APP_DIR%\%%F" >nul || goto :failed
)

powershell.exe -NoProfile -Command "$ErrorActionPreference='Stop'; $shell=New-Object -ComObject WScript.Shell; $path=Join-Path ([Environment]::GetFolderPath('Programs')) 'NEXUS Playback Bar.lnk'; $shortcut=$shell.CreateShortcut($path); $shortcut.TargetPath=$env:NEXUS_PYTHONW; $shortcut.Arguments=[char]34+(Join-Path $env:NEXUS_APP_DIR 'nexus-playback.pyw')+[char]34; $shortcut.WorkingDirectory=$env:NEXUS_APP_DIR; $shortcut.IconLocation=(Join-Path $env:NEXUS_APP_DIR 'npb.ico')+',0'; $shortcut.Description='Open NEXUS Playback Bar'; $shortcut.Save()"
if errorlevel 1 goto :failed
echo Installed. Search for NEXUS Playback Bar in the Windows Start menu.
pause
exit /b 0

:python_missing
echo Install Python 3 for Windows with Tkinter and the Python launcher, then try again.
pause
exit /b 1

:failed
echo Installation did not complete. Check the error above and try again.
pause
exit /b 1
