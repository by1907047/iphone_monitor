# Third-party notices

iPhone Monitor is licensed under the **GNU General Public License v3.0 or later** (see `LICENSE`), because it builds on GPL-licensed libraries such as pymobiledevice3.

The release package (`iPhoneMonitor.zip`) bundles the Python runtime and the following components. Their license files are included next to the corresponding packages inside the `_internal` folder where the upstream package ships them.

| Component | License | Notes |
|---|---|---|
| [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) | GPL-3.0-or-later | iOS device communication (lockdown, diagnostics, developer services) |
| pmd-pytcp, pytun-pmd3, ipsw-parser, developer-disk-image | GPL-3.0-or-later | Dependencies of pymobiledevice3 |
| [Wintun](https://www.wintun.net/) `wintun.dll` | Wintun Prebuilt Binaries License (WireGuard LLC) | Loaded by pytun-pmd3; redistributed unmodified alongside software that uses it only via its API. Full text: `third_party/wintun/LICENSE.txt` |
| [pystray](https://github.com/moses-palmer/pystray) | LGPL-3.0 | Tray icon |
| [Pillow](https://python-pillow.org/) | MIT-CMU (HPND) | Image rendering |
| [comtypes](https://github.com/enthought/comtypes) | MIT | Windows UI Automation (taskbar layout) |
| [PyWinRT](https://github.com/pywinrt/pywinrt) (`winrt-*`) | MIT | Bluetooth audio (AudioPlaybackConnection) |
| construct, bpylist2, typer, six | MIT | |
| cryptography | Apache-2.0 OR BSD-3-Clause | |
| pycryptodome | BSD-2-Clause AND Unlicense | |
| requests | Apache-2.0 | |
| click, pygments, prompt-toolkit, qh3 | BSD | |
| Python | PSF License | Bundled interpreter |
| [PyInstaller](https://pyinstaller.org/) bootloader | GPL-2.0 with bootloader exception | The exception allows distributing the bundled program under its own license |

## Downloads at runtime

- **Developer disk image** (only when CPU / memory monitoring is enabled and the image is not yet mounted): downloaded from [doronz88/DeveloperDiskImage](https://github.com/doronz88/DeveloperDiskImage) and personalized by Apple's signing server (`gs.apple.com`), exactly as Xcode does.

No other data leaves your computer. The program has no telemetry.

## Trademarks

iPhone, iOS, and MagSafe are trademarks of Apple Inc. This project is not affiliated with, endorsed by, or sponsored by Apple Inc. Windows is a trademark of Microsoft Corporation.
