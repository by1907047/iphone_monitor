@echo off
rem Build dist\iPhoneMonitor\iPhoneMonitor.exe (one-folder build: faster startup, fewer antivirus false positives).
rem Uses a dedicated virtual env (.build-venv) so unrelated global packages (TensorFlow etc.) are not pulled in.
cd /d "%~dp0"
if not exist .build-venv\Scripts\python.exe (
  echo Creating build environment...
  python -m venv .build-venv || exit /b 1
  .build-venv\Scripts\python -m pip install --upgrade pip >nul
)
.build-venv\Scripts\python -m pip install -q -r requirements.txt pyinstaller || exit /b 1
rem comtypes generates the UI Automation wrapper at runtime; generate it now so it can be bundled
.build-venv\Scripts\python -c "import comtypes.client; comtypes.client.GetModule('UIAutomationCore.dll')" || exit /b 1

.build-venv\Scripts\pyinstaller --noconfirm --clean --windowed ^
  --name iPhoneMonitor ^
  --icon icon.ico ^
  --add-data "icon.ico;." ^
  --collect-data pymobiledevice3 ^
  --collect-submodules pmd_pytcp ^
  --collect-submodules winrt ^
  --collect-submodules comtypes.gen ^
  --copy-metadata pyimg4 ^
  --add-binary ".build-venv\Lib\site-packages\pytun_pmd3\wintun\bin\amd64\wintun.dll;pytun_pmd3\wintun\bin\amd64" ^
  --hidden-import pymobiledevice3.remote.userspace_tunnel ^
  --hidden-import pymobiledevice3.services.cryptexd ^
  --hidden-import pymobiledevice3.services.mobile_image_mounter ^
  --hidden-import pymobiledevice3.services.dvt.instruments.sysmontap ^
  --hidden-import pymobiledevice3.services.dvt.instruments.device_info ^
  --hidden-import pymobiledevice3.services.dvt.instruments.dvt_provider ^
  --exclude-module IPython ^
  --exclude-module jedi ^
  --exclude-module parso ^
  app.py
if errorlevel 1 exit /b 1

rem License files must ship with the binaries (GPL-3.0, Wintun prebuilt binaries license)
copy /y LICENSE dist\iPhoneMonitor\LICENSE.txt >nul
copy /y THIRD_PARTY_NOTICES.md dist\iPhoneMonitor\ >nul
if not exist dist\iPhoneMonitor\third_party\wintun mkdir dist\iPhoneMonitor\third_party\wintun
copy /y third_party\wintun\LICENSE.txt dist\iPhoneMonitor\third_party\wintun\ >nul

rem Zip + SHA256 for the release page
.build-venv\Scripts\python -c "import hashlib, os, zipfile; z = zipfile.ZipFile('dist/iPhoneMonitor.zip', 'w', zipfile.ZIP_DEFLATED, compresslevel=9); [z.write(os.path.join(r, f), os.path.relpath(os.path.join(r, f), 'dist')) for r, _, fs in os.walk('dist/iPhoneMonitor') for f in fs]; z.close(); h = hashlib.sha256(open('dist/iPhoneMonitor.zip', 'rb').read()).hexdigest(); open('dist/iPhoneMonitor.zip.sha256', 'w').write(h + '  iPhoneMonitor.zip\n'); print('SHA256', h)" || exit /b 1
echo.
echo Built: dist\iPhoneMonitor\iPhoneMonitor.exe
echo        dist\iPhoneMonitor.zip (+ .sha256)
