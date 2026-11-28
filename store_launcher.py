"""Entry point used only by the Microsoft Store package."""

from datetime import datetime, timezone
from math import ceil
from pathlib import Path
import sys

from PySide6.QtCore import QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton, QVBoxLayout, QWidget

import app
from core import read_config, write_config
from store_license import read_license


STORE_ID = "9NX7S31MZ1F4"


class LicenseCheck(QThread):
    checked = Signal(object)

    def run(self):
        try:
            result = read_license()
        except (OSError, RuntimeError, TimeoutError):
            result = None
        self.checked.emit(result)


class StoreWindow(app.MainWindow):
    def __init__(self, *, settings_file):
        self._license = None
        self._checked = False
        self._has_access = False
        self._retiring_worker = None
        if not settings_file.exists():
            try:
                config = read_config()
                if config["ip"] and config["token"]:
                    write_config(config, settings_file)
            except (OSError, ValueError):
                # Keep the old profile untouched and let the user pair again.
                pass
        super().__init__(settings_file=settings_file, start_hub=False, can_access=self._allows_access)
        self.centralWidget().setEnabled(False)
        startup = QAction("Start at sign-in…", self)
        startup.triggered.connect(lambda: QDesktopServices.openUrl(QUrl("ms-settings:startupapps")))
        self._tray_menu.insertAction(self._tray_menu.actions()[-2], startup)

        banner = QWidget(self)
        layout = QVBoxLayout(banner)
        self._license_message = app.wrapped_label("Checking your Microsoft Store license…")
        layout.addWidget(self._license_message)
        actions = app.WrappingLayout()
        buy = QPushButton("Open Microsoft Store")
        buy.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(f"ms-windows-store://pdp/?ProductId={STORE_ID}")))
        actions.addWidget(buy)
        verify = QPushButton("Check license again")
        verify.clicked.connect(self._check_license)
        actions.addWidget(verify)
        layout.addLayout(actions)
        self.setMenuWidget(banner)

        self._license_check = LicenseCheck(self)
        self._license_check.checked.connect(self._on_license_checked)
        self._license_timer = QTimer(self, interval=60_000)
        self._license_timer.timeout.connect(self._check_license)
        self._license_timer.start()
        self._expiry_timer = QTimer(self, singleShot=True)
        self._expiry_timer.timeout.connect(self._show_license)
        QTimer.singleShot(0, self._check_license)

    def _allows_access(self):
        license = self._license
        return license is not None and license.allows_access()

    def _check_license(self):
        if not self._license_check.isRunning():
            self._license_check.start()

    def _on_license_checked(self, license):
        self._checked = True
        if license is not None:
            self._license = license
        self._show_license()

    def _show_license(self):
        active = self._allows_access()
        trial_until = self._license.trial_until if self._license is not None else None
        self._expiry_timer.stop()
        if active and trial_until is not None:
            self._license_message.setText(
                f"Microsoft Store trial ends {trial_until.astimezone():%d %b %Y, %H:%M}."
            )
            remaining = trial_until - datetime.now(timezone.utc)
            self._expiry_timer.start(min(86_400_000, max(1, ceil(remaining.total_seconds() * 1000))))
        elif not active and trial_until is not None and trial_until <= datetime.now(timezone.utc):
            self._license_message.setText("Your free trial has ended. Buy Lysnav in Microsoft Store to resume hub controls.")
        elif not active and self._license is not None:
            self._license_message.setText("Open Microsoft Store to start a trial or buy Lysnav. Hub controls are paused.")
        elif not active and self._checked:
            self._license_message.setText("Could not verify your Microsoft Store license. Open the Store or try again.")
        self.menuWidget().setVisible(not active or trial_until is not None)

        if active == self._has_access:
            return
        self._has_access = active
        self.centralWidget().setEnabled(active)
        if active:
            if self._retiring_worker is None:
                self.begin_hub_session()
            return

        self.refresh_timer.stop()
        self._tray_brightness_timer.stop()
        self._slider_wheel_timer.stop()
        self._slider_wheel_pending.clear()
        self._tray_pending_level = None
        self._tray_pending_ids = ()
        self._tray_scroll_accumulator = 0
        self._tray_state_stale = True
        self._room_power_action.setEnabled(False)
        if self.worker is not None:
            self._retiring_worker, self.worker = self.worker, None
            self._retiring_worker.finished.connect(self._worker_stopped)
            self._retiring_worker.requestInterruption()
            self._retiring_worker.stop()
            if self._retiring_worker.isFinished():
                self._worker_stopped()
        if self.color_dialog is not None:
            self.color_dialog.close()
        for dialog_type in (app.TokenPairingDialog, app.ConnectionDialog):
            for dialog in self.findChildren(dialog_type):
                dialog.reject()
        self.update_tray_tooltip()
        self.status.setText("Hub controls paused")
        self.tray.showMessage("Lysnav", "Hub controls are paused. Open Lysnav to check your Store license.")

    def _worker_stopped(self):
        if self._retiring_worker is None or self._retiring_worker.isRunning():
            return
        self._retiring_worker = None
        if self._allows_access() and not self._really_quit:
            self.begin_hub_session()

    def update_tray_menu(self):
        super().update_tray_menu()
        if not self._allows_access():
            self._room_power_action.setEnabled(False)

    def change_connection(self):
        if self._allows_access():
            super().change_connection()

    def start_worker(self):
        if self._allows_access() and self._retiring_worker is None:
            super().start_worker()

    def on_loaded(self, result):
        if self.sender() is self.worker and self._allows_access():
            super().on_loaded(result)

    def on_completed(self, message):
        if self.sender() is self.worker and self._allows_access():
            super().on_completed(message)

    def on_failed(self, error):
        if self.sender() is self.worker and self._allows_access():
            super().on_failed(error)

    def closeEvent(self, event):
        super().closeEvent(event)
        if event.isAccepted():
            self._license_timer.stop()
            self._expiry_timer.stop()
            self._license_check.wait()
            if self._retiring_worker is not None:
                self._retiring_worker.wait()


def launch():
    from winrt.runtime import ApartmentType, init_apartment, uninit_apartment
    from winrt.windows.applicationmodel import AppInstance
    from winrt.windows.applicationmodel.activation import ActivationKind
    from winrt.windows.services.store import StoreContext
    from winrt.windows.storage import ApplicationData

    if sys.argv[1:] == ["--check-runtime"]:
        # Also exercise the bundled WinRT projections before packaging.
        if not all((StoreContext, ApplicationData, AppInstance, ActivationKind.STARTUP_TASK)):
            raise RuntimeError("Store runtime not available")
        app.main()
        return

    init_apartment(ApartmentType.SINGLE_THREADED)
    try:
        settings_file = Path(ApplicationData.current.local_folder.path) / "config.json"
        try:
            activation = AppInstance.get_activated_event_args()
        except OSError:
            activation = None
        if activation is not None and activation.kind == ActivationKind.STARTUP_TASK:
            sys.argv.append("--tray")
    except OSError:
        qt_app = QApplication(sys.argv)
        QMessageBox.critical(None, "Lysnav", "Open this version of Lysnav from its Microsoft Store installation.")
        return
    finally:
        uninit_apartment()
    app.main(StoreWindow, settings_file=settings_file)


if __name__ == "__main__":
    app.run_entry_point(launch)
