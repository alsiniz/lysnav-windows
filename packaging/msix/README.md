# Microsoft Store package

The Windows builds workflow produces an unsigned x64 MSIX alongside the installer and portable EXE. All three use the commit checked out by the workflow and the version in `version.py`. The MSIX uses the Partner Center identity for Lysnav and Store product ID `9NX7S31MZ1F4`. The Store signs the customer download after certification.

## Build on Windows 11

Use x64 Python 3.12 and install the Windows SDK. Run these commands from the repository root.

```powershell
python -m venv .venv-msix
.\.venv-msix\Scripts\python.exe -m pip install -r requirements-msix.txt
.\.venv-msix\Scripts\python.exe -m unittest test_store
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv-msix\Scripts\python.exe packaging/build_msix.py
```

The result is `release/msix/Lysnav-Windows-<version>-x64.msix`. The CI run also exposes it in the `lysnav-windows-x64-msix` artifact. Submit the MSIX file inside the artifact ZIP.

## First submission and validation

1. In Partner Center, open **Lysnav** and create an MSIX submission. Upload the package, choose a paid price tier, and set **Free trial** to **Time-limited → 7 days**. Microsoft handles licensing and purchases.
2. Complete the listing and certification information. Explain that Lysnav controls an IKEA DIRIGERA hub over the local network and requires access to a hub to test pairing and control. Arrange a test path with certification if reviewers lack a hub. The `runFullTrust` declaration lets the existing desktop client reach the hub over the LAN and use the Windows notification area, including its tray mouse gestures.
3. Use a private audience for the first Store installation. Verify trial and purchased licenses, offline launch with an active cached license, expiry while running in the tray, and purchase after expiry. Verify pairing, approved-device controls, tray gestures and an update on Windows 11 with a real DIRIGERA hub.

The tests simulate license states. The package uses Windows' actual license and depends on Store acquisition to validate production trial and purchase behavior. The Store build imports an existing standalone configuration on first launch if it has an address and token. Subsequent Store settings live in the package's local data folder. To opt in to automatic startup, choose **Start at sign-in…** in the tray menu and enable Lysnav in Windows Startup apps.

## Third-party libraries

The MSIX includes third-party notices, license texts, and the versions of the bundled dependencies. The public source and the build commands above provide the inputs for building with modified compatible Qt/PySide6 libraries. Review the Qt/PySide6 LGPL distribution terms for the Store package, make the matching library source available, and validate installation of a rebuilt package before public distribution. A rebuilt package can be signed for sideloading with a local test certificate, using a matching Publisher value. Keep the Store package's license and notice files when rebuilding.

[Partner Center submission guide](https://learn.microsoft.com/en-us/windows/apps/publish/publish-your-app/msix/create-app-submission) · [Store pricing and trials](https://learn.microsoft.com/en-us/windows/apps/publish/publish-your-app/msix/price-and-availability) · [Qt licensing obligations](https://www.qt.io/development/open-source-lgpl-obligations)
