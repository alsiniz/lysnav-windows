# Changelog

## 1.1.0 (2026-11-28)

- Colour temperature sliders use a fixed warm-to-cool gradient. RGB mode dims the track while keeping the slider usable.
- Room controls identify mixed temperatures and mixed modes. Adjusting the room slider switches all approved lights in that room to white within their shared temperature range.
- The temperature track and thumb have clean edges, and the live colour wheel is antialiased at the display's scale.

## 1.0.0 (2026-11-28)

- Dialogs adapt to font size and available screen space. Long forms scroll with their action buttons visible.
- Hub settings gives the DHCP note room to wrap. The two pairing instructions stay together beside the hub illustration.
- The existing colour temperature slider shows the selected Kelvin value as a live tint. Its track is muted in RGB mode, and keyboard adjustments use the existing temperature command.

## 1.0.0-rc.3 (2026-11-28)

- Redesigned Hub settings and token pairing with token reveal/copy controls, a two-action pairing guide, palette-adaptive vector illustration, and static IP/DHCP reservation guidance.
- Render native tray icon pixmaps at common Windows shell sizes; validate packaged icon colours and show approved-light power and icon variant in tray diagnostics.
- Separated tray appearance controls and disclaimer into independent layout rows with reserved vertical clearance, including at the dialog's minimum size.
- Prevented vertically compressed tray appearance buttons with a shared minimum height that accounts for native font and style metrics.
- Redesigned Tray settings into separate room, tray appearance and mouse-control sections with native styling, an icon preview and aligned mouse illustrations.
- Restored Windows custom notification-area icons through the native .ico file picker, with persisted selection, invalid-file handling, a dynamic-status disclaimer and a one-click default reset. Save applies changes; Cancel preserves the current tray.
- The default tray lamp turns orange when any approved light in the tray selection is on. Choose one room or All approved lights for the indicator, middle-click and brightness scrolling.
- Automatic refresh retries after the first hub check fails.
- Full-colour Lysnav logo is used for application windows, launchers and package icons; raster package icons are generated from the canonical SVG.

- Includes the refresh-time memory fix from 1.0.0-rc.2 for long-running tray sessions. The portable executable and installer use the Qt frontend. The WinUI preview has been retired.
- Adopted the Table Lamp logo and monochrome light- and dark-panel icons. The running app follows the Windows system colour mode.
- Renamed the app to Lysnav for Windows. The README and About view now identify Lysnav as an independent third-party app.
- Renamed the installed executable to `Lysnav.exe` and future installer and portable downloads to use the `Lysnav-Windows-` prefix. Existing installs retain their installer identity, pairing, and device approvals.

## 1.0.0-rc.2 (2026-11-26)

- Fixed memory growth during long-running tray sessions. Hub refreshes now dispose of replaced room and climate controls.
- Reused the notification-area menu across refreshes and updated its room choices when the approved rooms change.
- Released the Tray Settings dialog after Save or Cancel.
- Added Qt regression tests for repeated refreshes, room selection, and dialog disposal.

## 1.0.0-rc.1

First release candidate with a Windows x64 installer and portable executable, per-room lighting controls, climate readings, and notification-area shortcuts.
