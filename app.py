#!/usr/bin/env python3
"""Local lighting and climate controls for IKEA DIRIGERA on Windows."""
from __future__ import annotations

import math
from hashlib import sha256
from html import escape
import queue
import sys
from pathlib import Path
from collections import defaultdict
from dataclasses import replace
from threading import Lock
from importlib.metadata import PackageNotFoundError, version as package_version
from version import APP_NAME, VERSION, KOFI_URL
from windows_tray import TrayWheelHook
from pairing import valid_hub_address, extract_generated_token, run_token_helper

from PySide6.QtCore import QByteArray, QSize, Qt, QThread, Signal, QTimer, QPointF, QRectF, QUrl, QProcess, QProcessEnvironment, QLockFile, QEvent, QRect
from PySide6.QtGui import QColor, QPainter, QPen, QIcon, QImage, QImageReader, QPixmap, QCursor, QKeySequence, QShortcut, QDesktopServices
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtGui import QConicalGradient, QLinearGradient, QRadialGradient
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QFileDialog, QLayout,
    QLabel, QPushButton, QLineEdit, QDialog, QDialogButtonBox, QPlainTextEdit, QHeaderView, QTreeWidget,
    QTreeWidgetItem, QMessageBox, QTabWidget, QScrollArea, QGroupBox, QSlider,
    QSystemTrayIcon, QMenu, QFrame, QSizePolicy, QComboBox, QToolTip, QStyleOptionSlider, QStyle,
)
from core import (ALL_APPROVED, apply, approved_climate, approved_visible, config_path, environment_snapshot, read_config, snapshot,
                  toggle_power, wheel_steps, stepped_brightness, tray_power_targets,
                  tray_targets, write_config)


ASSETS = Path(__file__).resolve().parent / "assets"


def custom_tray_qicon(filename):
    """Read a real ICO into a standalone QIcon; never execute icon files."""
    if not filename:
        return QIcon()
    path = Path(filename)
    try:
        if not path.is_absolute() or path.suffix.casefold() != ".ico":
            return QIcon()
        with path.open("rb") as source:
            # ICO magic, not a cursor, executable, or renamed image.
            if source.read(4) != b"\x00\x00\x01\x00":
                return QIcon()
        source_icon = QIcon(str(path))
        icon = QIcon()
        for size in source_icon.availableSizes():
            if 0 < size.width() <= 256 and 0 < size.height() <= 256:
                pixmap = source_icon.pixmap(size)
                if not pixmap.isNull():
                    icon.addPixmap(pixmap)
        return icon
    except (OSError, ValueError):
        return QIcon()


def native_tray_icon(filename):
    """Render bounded size variants before handing the icon to Windows Shell."""
    source = QIcon(str(ASSETS / filename))
    icon = QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64):
        pixmap = source.pixmap(size, size)
        if pixmap.isNull():
            raise RuntimeError(f"Cannot render tray icon: {filename}")
        icon.addPixmap(pixmap)
    return icon


def panel_icon_filename(active=False):
    # Windows can use different colour modes for apps and the shell.
    light = QApplication.palette().window().color().lightness() >= 128
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                light = bool(winreg.QueryValueEx(key, "SystemUsesLightTheme")[0])
        except OSError:
            pass
    mode = "light" if light else "dark"
    return f"tray-{mode}{'-on' if active else ''}.svg"


class HubWorker(QThread):
    loaded = Signal(object)
    color_preview_applied = Signal(object)
    completed = Signal(str)
    failed = Signal(str)

    def __init__(self, ip: str, token: str, approved: dict[str, str]):
        super().__init__()
        self.ip = ip
        self.token = token
        self._approved = dict(approved)
        self._lock = Lock()
        self._jobs: queue.Queue = queue.Queue()
        self._latest_preview = {}
        self._preview_queued = False

    def set_approved(self, approved: dict[str, str]):
        with self._lock:
            self._approved = dict(approved)

    def submit(self, kind: str, *args):
        self._jobs.put((kind, args))

    def submit_preview(self, ids: list[str], value: tuple[float, float]):
        """Keep only the most recent live wheel colour per group during drag."""
        with self._lock:
            self._latest_preview[tuple(ids)] = value
            if not self._preview_queued:
                self._preview_queued = True
                self._jobs.put(("preview", ()))

    def stop(self):
        self._jobs.put(("stop", ()))

    def _publish(self, hub):
        lights = snapshot(hub)
        warning = ""
        try:
            sensors = environment_snapshot(hub)
        except Exception as exc:
            # Unsupported sensor types must not break lighting functionality.
            sensors = []
            warning = f"Climate discovery unavailable: {type(exc).__name__}: {exc}"
        self.loaded.emit((lights, sensors, warning))

    def run(self):
        # Import in worker so all network I/O remains outside the UI thread.
        import dirigera
        hub = dirigera.Hub(token=self.token, ip_address=self.ip)
        while True:
            kind, args = self._jobs.get()
            if kind == "stop":
                break
            try:
                if kind == "refresh":
                    self._publish(hub)
                elif kind == "preview":
                    with self._lock:
                        previews = self._latest_preview
                        self._latest_preview = {}
                        self._preview_queued = False
                        approved = dict(self._approved)
                    for ids, value in previews.items():
                        apply(hub, approved, list(ids), "color", value)
                        self.color_preview_applied.emit(ids)
                    # Refresh once when the wheel closes; don't read the whole
                    # hub on every intermediate pointer movement.
                elif kind == "toggle":
                    ids, selection = args
                    with self._lock:
                        approved = dict(self._approved)
                    count, turned_on = toggle_power(hub, approved, ids, all_rooms=selection == ALL_APPROVED)
                    self.completed.emit(f"{'On' if turned_on else 'Off'} · {count} approved light(s)")
                    self._publish(hub)
                elif kind == "control":
                    ids, action, value = args
                    with self._lock:
                        approved = dict(self._approved)
                    changed = apply(hub, approved, ids, action, value)
                    self.completed.emit(f"Updated {changed} approved light(s)")
                    self._publish(hub)
            except Exception as exc:
                self.failed.emit(f"{type(exc).__name__}: {exc}")


class Wheel(QWidget):
    selection_changed = Signal(float, float)
    selection_finished = Signal()

    def __init__(self, hue: float = 25, saturation: float = 1):
        super().__init__()
        self.setFixedSize(252, 252)
        self.hue = float(hue) % 360
        self.saturation = max(0.0, min(1.0, float(saturation)))
        self.r = 113
        self.center = 126
        self.setToolTip("Angle selects hue, distance from centre selects saturation")

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        centre = QPointF(self.center, self.center)
        hue_ring = QConicalGradient(centre, 0)
        for step in range(7):
            hue_ring.setColorAt(step / 6, QColor.fromHsv(step * 60 % 360, 255, 255))
        saturation = QRadialGradient(centre, self.r)
        saturation.setColorAt(0, QColor(255, 255, 255))
        saturation.setColorAt(1, QColor(255, 255, 255, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(hue_ring)
        p.drawEllipse(centre, self.r, self.r)
        p.setBrush(saturation)
        p.drawEllipse(centre, self.r, self.r)
        x = self.center + self.r * self.saturation * math.cos(math.radians(self.hue))
        y = self.center - self.r * self.saturation * math.sin(math.radians(self.hue))
        p.setPen(QPen(QColor("#151515"), 3))
        p.setBrush(QColor.fromHsv(round(self.hue) % 360, round(self.saturation * 255), 255))
        p.drawEllipse(QPointF(x, y), 9, 9)
        p.setPen(QPen(QColor("#ffffff"), 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(x, y), 11, 11)

    def _select_at(self, pos):
        dx, dy = pos.x() - self.center, self.center - pos.y()
        self.hue = math.degrees(math.atan2(dy, dx)) % 360
        self.saturation = min(1.0, math.hypot(dx, dy) / self.r)
        self.selection_changed.emit(self.hue, self.saturation)
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._select_at(event.position())

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._select_at(event.position())

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.selection_finished.emit()


class WheelDialog(QDialog):
    """Modeless colour picker: preview on drag, stay open until Close."""

    def __init__(self, parent, hue, saturation, preview_callback, finished_callback):
        super().__init__(parent)
        self.setWindowTitle("Live light colour preview")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._preview_callback = preview_callback
        self._finished_callback = finished_callback
        self._dirty = False
        outer, scroll, layout = scrollable_form(self)
        layout.addWidget(wrapped_label("Drag around the wheel to preview colours on your approved lights."))
        self.wheel = Wheel(hue, saturation)
        layout.addWidget(self.wheel, alignment=Qt.AlignmentFlag.AlignCenter)
        self.readout = wrapped_label("")
        self.readout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.readout)
        self.wheel.selection_changed.connect(self.update_readout)
        self.wheel.selection_finished.connect(self.flush_preview)
        self.update_readout(self.wheel.hue, self.wheel.saturation, schedule=False)
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(140)
        self.preview_timer.timeout.connect(self.flush_preview)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        outer.addWidget(buttons)
        layout.addWidget(wrapped_label("Colours apply as you move. Close keeps the last selected colour; there is no Cancel/undo."))
        fit_window(self, 54, scroll=scroll)

    def update_readout(self, h, s, schedule=True):
        self.readout.setText(f"Hue {round(h)}°    ·    Saturation {round(s * 100)}%")
        if schedule:
            self._dirty = True
            self.preview_timer.start()

    def flush_preview(self):
        if self._dirty:
            self._dirty = False
            self.preview_timer.stop()
            self._preview_callback((round(self.wheel.hue, 1), round(self.wheel.saturation, 3)))

    def closeEvent(self, event):
        self.flush_preview()
        self._finished_callback()
        super().closeEvent(event)


class WrappingLayout(QLayout):
    """Place controls in rows using their current style and font size hints."""

    def __init__(self):
        super().__init__()
        self.setContentsMargins(0, 0, 0, 0)
        self._items = []

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def hasHeightForWidth(self):
        return True

    def minimumSize(self):
        size = QSize(0, 0)
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def sizeHint(self):
        sizes = [item.sizeHint() for item in self._items if not item.isEmpty()]
        width = sum(size.width() for size in sizes) + max(0, len(sizes) - 1) * self._spacing()
        return QSize(width, max((size.height() for size in sizes), default=0))

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, width, 0), False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, True)

    def _spacing(self):
        return max(0, self.spacing())

    def _arrange(self, rect, place):
        spacing = self._spacing()
        x = y = row_height = 0
        for item in self._items:
            if item.isEmpty():
                continue
            size = item.sizeHint().expandedTo(item.minimumSize())
            if x and x + size.width() > rect.width():
                x = 0
                y += row_height + spacing
                row_height = 0
            if place:
                bounds = QRect(rect.x() + x, rect.y() + y, size.width(), size.height())
                item.setGeometry(QStyle.visualRect(self.parentWidget().layoutDirection(), rect, bounds))
            x += size.width() + spacing
            row_height = max(row_height, size.height())
        return y + row_height


def wrapping_form():
    row = QFormLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setLabelAlignment(Qt.AlignmentFlag.AlignLeading | Qt.AlignmentFlag.AlignTop)
    row.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
    row.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    return row


def wrapping_pair(label, field):
    row = wrapping_form()
    row.addRow(label, field)
    return row


def scrollable_form(parent):
    """Let wrapped content grow at the viewport width and keep actions visible."""
    outer = QVBoxLayout(parent)
    scroll = QScrollArea(parent)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setWidgetResizable(True)
    content = QWidget()
    layout = QVBoxLayout(content)
    layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
    scroll.setWidget(content)
    outer.addWidget(scroll, 1)
    return outer, scroll, layout


def fit_window(window, columns, lines=None, scroll=None):
    """Choose an initial size in font units within the owner's available screen."""
    window.ensurePolished()
    metrics = window.fontMetrics()
    owner = window.parentWidget()
    available = (owner or window).screen().availableGeometry().size()
    style = window.style()
    edge = style.pixelMetric(QStyle.PixelMetric.PM_DefaultFrameWidth, widget=window) * 4
    title = style.pixelMetric(QStyle.PixelMetric.PM_TitleBarHeight, widget=window)
    bounds = available - QSize(edge, title + edge)
    window.layout().activate()
    minimum = window.minimumSizeHint()
    width = min(bounds.width(), max(minimum.width(), metrics.horizontalAdvance("0") * columns))
    if scroll is not None:
        layout = window.layout()
        inner_width = width - layout.contentsMargins().left() - layout.contentsMargins().right()
        body = scroll.widget().layout()
        body.activate()
        height = max(body.minimumSize().height(), body.totalHeightForWidth(inner_width))
        height += layout.sizeHint().height() - scroll.sizeHint().height()
    else:
        height = metrics.lineSpacing() * lines
    window.resize(width, min(bounds.height(), max(height, minimum.height())))


def text_dialog(parent, title, text):
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    layout = QVBoxLayout(dialog)
    details = QPlainTextEdit(text, dialog)
    details.setReadOnly(True)
    details.setAccessibleName(title)
    layout.addWidget(details)
    return dialog, layout


def section_heading(text):
    label = wrapped_label(text)
    font = label.font()
    font.setBold(True)
    if font.pointSize() > 0:
        font.setPointSize(font.pointSize() + 1)
    label.setFont(font)
    return label


def wrapped_label(text, *, rich=False):
    label = QLabel(text)
    label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.RichText if rich else Qt.TextFormat.PlainText)
    return label


def section_divider():
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    return line


def hub_action_svg(palette):
    """Approved B.4 vector: palette-aware line art and a fixed orange action cue."""
    ink = palette.windowText().color().name()
    background = palette.window().color().name()
    muted = QColor(ink)
    base = QColor(background)
    muted.setRgb(*(round(a * .48 + b * .52)
                   for a, b in zip(muted.getRgb()[:3], base.getRgb()[:3])))
    secondary = muted.name()
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="40 10 270 245">
      <g stroke-linecap="round" stroke-linejoin="round">
        <circle cx="190" cy="119" r="99" fill="none" stroke="{ink}" stroke-width="2"/>
        <circle cx="190" cy="119" r="84" fill="none" stroke="{secondary}" stroke-width="1.5"/>
        <path d="M182 36h16v10a12 12 0 1 1-16 0Z" fill="{background}" stroke="{secondary}" stroke-width="1.5"/>
        <path d="M177 202v16h26v-16" fill="{background}" stroke="{secondary}" stroke-width="1.5"/>
        <circle cx="255" cy="119" r="2.8" fill="{secondary}"/>
        <circle cx="125" cy="119" r="12" fill="#FE8019" fill-opacity=".18" stroke="#FE8019" stroke-width="2.5"/>
        <circle cx="125" cy="119" r="21" fill="none" stroke="#FE8019" stroke-width="1.5" stroke-dasharray="4 5"/>
        <g transform="translate(125 119) rotate(35)">
          <path d="M-12 112V94L-27 69Q-33 59-25 54Q-19 50-13 58L-7 67V8Q-7-2 2-2Q11-2 11 8V42Q18 35 25 39Q31 42 31 48Q38 43 44 47Q50 51 50 57Q56 53 62 58Q67 63 67 74V91Q67 103 57 112Z" fill="{background}" stroke="{ink}" stroke-width="2.4"/>
          <path d="M11 42v19M31 48v15M50 57v12" fill="none" stroke="{ink}" stroke-width="1.6"/>
        </g>
        <path d="M125 89v-8M146.2 97.8l6-6M155 119h8" stroke="#FE8019" stroke-width="2.5" fill="none"/>
      </g>
    </svg>'''.encode("utf-8")


class HubActionIllustration(QWidget):
    """Render the vector at the device's actual scale, not as a baked image."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(156, 142)
        self.setAccessibleName("Illustration: press the larger action button under the hub, not the reset pinhole")

    def paintEvent(self, event):
        renderer = QSvgRenderer(QByteArray(hub_action_svg(self.palette())))
        painter = QPainter(self)
        renderer.render(painter)
        painter.end()


class TokenPairingDialog(QDialog):
    """GUI front end to the upstream generate-token pairing handshake.

    Only the explicit helper child receives the hub IP. Its captured output is
    never shown or logged, since the token is a hub-wide credential.
    """

    def __init__(self, parent, ip: str):
        super().__init__(parent)
        self.setWindowTitle("Generate DIRIGERA access token")
        self.generated_token = None
        self.ip = ip
        self._output = bytearray()
        self._retry = False
        self._closing = False

        outer, scroll, layout = scrollable_form(self)
        layout.addWidget(section_heading("Pair with your hub"))
        layout.addWidget(wrapped_label("Generate a new access token for Lysnav to communicate with your DIRIGERA hub locally."))

        status_frame = QFrame()
        status_frame.setFrameShape(QFrame.Shape.StyledPanel)
        status_layout = QHBoxLayout(status_frame)
        status_layout.setContentsMargins(12, 12, 12, 12)
        status_layout.setSpacing(10)
        self.status_icon = QLabel()
        status_layout.addWidget(self.status_icon, alignment=Qt.AlignmentFlag.AlignTop)
        status_text = QVBoxLayout()
        status_text.setSpacing(4)
        self.status_title = section_heading("Preparing pairing…")
        self.status = wrapped_label("Lysnav is starting the local pairing request.")
        status_text.addWidget(self.status_title)
        status_text.addWidget(self.status)
        status_layout.addLayout(status_text, 1)
        layout.addWidget(status_frame)
        layout.addWidget(section_heading("What you need to do"))
        self.hub_illustration = HubActionIllustration()
        steps = QWidget()
        steps_layout = QVBoxLayout(steps)
        steps_layout.setContentsMargins(0, 0, 0, 0)
        steps_layout.addStretch(1)
        for text in ("<b>1. Press the action button</b> on the underside of your DIRIGERA hub.",
                     "<b>2. Click Continue</b> below to complete pairing."):
            steps_layout.addWidget(wrapped_label(text, rich=True))
        steps_layout.addStretch(1)
        layout.addLayout(wrapping_pair(self.hub_illustration, steps))
        layout.addWidget(wrapped_label("Your access token is stored locally when you save your hub settings. Keep it private."))
        layout.addStretch()
        outer.addWidget(section_divider())
        row = QHBoxLayout()
        self.continue_button = QPushButton("Continue")
        self.continue_button.setEnabled(False)
        self.continue_button.clicked.connect(self.continue_pairing)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        row.addStretch()
        row.addWidget(self.continue_button)
        row.addWidget(cancel)
        outer.addLayout(row)
        self._set_status("Preparing pairing…", "Lysnav is starting the local pairing request.")

        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.process.started.connect(self._started)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.readyReadStandardError.connect(self._discard_error_output)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)
        self._start_timer = QTimer(self)
        self._start_timer.setSingleShot(True)
        self._start_timer.timeout.connect(self._start)
        fit_window(self, 76, scroll=scroll)
        self._start_timer.start(0)

    def _set_status(self, title, message, failed=False):
        self.status_title.setText(title)
        self.status.setText(message)
        symbol = QStyle.StandardPixmap.SP_MessageBoxWarning if failed else QStyle.StandardPixmap.SP_MessageBoxInformation
        self.status_icon.setPixmap(self.style().standardIcon(symbol).pixmap(24, 24))

    def _start(self):
        if self._closing:
            return
        self._retry = False
        self._output.clear()
        self.continue_button.setText("Continue")
        self.continue_button.setEnabled(False)
        self._set_status("Preparing pairing…", "Lysnav is starting the local pairing request.")
        # Launch the same app in helper mode, rather than assuming a shell or
        # an installed generate-token executable exists. Works with bundled apps.
        args = ["--token-helper", self.ip]
        executable = sys.executable
        if not getattr(sys, "frozen", False):
            args = ["-u", str(Path(__file__).resolve()), *args]
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONUNBUFFERED", "1")
        self.process.setProcessEnvironment(environment)
        self.process.start(executable, args)

    def _started(self):
        if self._closing:
            return
        self._set_status("Pairing helper started", "Follow the two instructions below.")
        self.continue_button.setEnabled(True)

    def _read_output(self):
        # Upstream prints the access token to stdout. Keep it private.
        self._output.extend(bytes(self.process.readAllStandardOutput()))

    def _discard_error_output(self):
        # Never copy child-process stderr into visible diagnostics: an upstream
        # exception could contain sensitive request information.
        self.process.readAllStandardError()

    def continue_pairing(self):
        if self._retry:
            self._start()
            return
        if self.process.state() != QProcess.ProcessState.Running:
            return
        self.continue_button.setEnabled(False)
        self._set_status("Completing pairing…", "Waiting for your DIRIGERA hub to respond.")
        self.process.write(b"\n")

    def _finished(self, exit_code, exit_status):
        self._read_output()
        if self._closing:
            self._output.clear()
            return
        token = extract_generated_token(self._output.decode("utf-8", errors="replace"))
        self._output.clear()
        if exit_code == 0 and exit_status == QProcess.ExitStatus.NormalExit and token:
            self.generated_token = token
            self.accept()
            return
        self._show_failure("Pairing was not completed. Check the hub address, press the action button, and try again.")

    def _process_error(self, error):
        if not self._closing and error == QProcess.ProcessError.FailedToStart:
            self._show_failure("Unable to start the pairing helper. Please reinstall the app or report this error.")

    def _show_failure(self, message):
        self._set_status("Pairing unsuccessful", message, failed=True)
        self._retry = True
        self.continue_button.setText("Try again")
        self.continue_button.setEnabled(True)

    def _cancel_helper(self):
        self._closing = True
        self._start_timer.stop()
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.kill()
            self.process.waitForFinished(1000)
        self._output.clear()

    def reject(self):
        self._cancel_helper()
        super().reject()

    def closeEvent(self, event):
        self._cancel_helper()
        super().closeEvent(event)


class ConnectionDialog(QDialog):
    def __init__(self, parent, config):
        super().__init__(parent)
        self.setWindowTitle("Hub settings")
        outer, scroll, form = scrollable_form(self)
        form.addWidget(section_heading("Hub connection"))
        form.addWidget(wrapped_label("Connect Lysnav to your local IKEA DIRIGERA hub."))
        address_label = QLabel("Hub address")
        self.ip = QLineEdit(config.get("ip", ""))
        self.ip.setPlaceholderText("192.168.1.20")
        address_label.setBuddy(self.ip)
        form.addWidget(address_label)
        form.addWidget(self.ip)
        self.address_help = wrapped_label("")
        form.addWidget(self.address_help)
        self.ip.textChanged.connect(self.reset_address_help)
        self.reset_address_help()
        form.addWidget(section_divider())

        token_label = section_heading("Access token")
        self.token = QLineEdit(config.get("token", ""))
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("Access token")
        token_label.setBuddy(self.token)
        form.addWidget(token_label)
        token_row = QHBoxLayout()
        token_row.setSpacing(8)
        self.token.setMinimumWidth(self.token.fontMetrics().horizontalAdvance("000000"))
        token_row.addWidget(self.token, 1)
        self.show_token_button = QPushButton("Show")
        self.show_token_button.setCheckable(True)
        self.show_token_button.setAutoDefault(False)
        self.show_token_button.setAccessibleName("Show access token")
        self.show_token_button.toggled.connect(self.set_token_visible)
        token_row.addWidget(self.show_token_button)
        self.copy_token_button = QPushButton("Copy")
        self.copy_token_button.setAutoDefault(False)
        self.copy_token_button.setToolTip("Copy the full access token to the clipboard")
        self.copy_token_button.setAccessibleName("Copy access token")
        self.copy_token_button.clicked.connect(self.copy_token)
        token_row.addWidget(self.copy_token_button)
        form.addLayout(token_row)
        self.token_help = wrapped_label("Keep your access token private. It is saved locally when you click Save.")
        form.addWidget(self.token_help)
        self.token.textChanged.connect(self.update_token_controls)
        self.update_token_controls()

        self.generate_button = QPushButton("Generate token…")
        self.generate_button.setAutoDefault(False)
        self.generate_button.setToolTip("Pair locally with DIRIGERA using its physical action button")
        self.generate_button.clicked.connect(self.generate_token)
        form.addLayout(wrapping_pair(self.generate_button,
                                      wrapped_label("Pair with your hub to obtain a new access token.")))
        # Reserve native font-aware sizes in the independent button rows.
        for button in (self.show_token_button, self.copy_token_button, self.generate_button):
            button.setMinimumHeight(max(32, button.sizeHint().height(), button.fontMetrics().height() + 12))
        self.ip.setMinimumHeight(max(32, self.ip.sizeHint().height()))
        self.token.setMinimumHeight(max(32, self.token.sizeHint().height(),
                                        self.show_token_button.minimumHeight(),
                                        self.copy_token_button.minimumHeight()))
        # Keep the Windows button actionable to show address guidance.

        note = QFrame()
        note.setFrameShape(QFrame.Shape.StyledPanel)
        note_layout = QHBoxLayout(note)
        note_layout.setContentsMargins(12, 12, 12, 12)
        note_layout.setSpacing(10)
        info = QLabel()
        info.setPixmap(self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation).pixmap(20, 20))
        note_layout.addWidget(info, alignment=Qt.AlignmentFlag.AlignTop)
        note_text = QVBoxLayout()
        note_text.setSpacing(6)
        note_text.addWidget(section_heading("Recommended: assign a static IP address to your DIRIGERA"))
        self.ip_note = wrapped_label(
            "Configure a DHCP reservation (also known as a fixed IP assignment) in your router "
            "so DIRIGERA always receives the same local IP address. This helps Lysnav reconnect "
            "reliably after restarts. Refer to your router's manual for instructions.")
        note_text.addWidget(self.ip_note)
        note_layout.addLayout(note_text, 1)
        form.addWidget(note)
        form.addStretch()
        outer.addWidget(section_divider())
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.check_accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        fit_window(self, 82, scroll=scroll)

    def reset_address_help(self, *_):
        self.address_help.setText("Find the hub's local IP address in your router's connected-device list.")

    def update_token_controls(self, *_):
        has_token = bool(self.token.text().strip())
        self.copy_token_button.setEnabled(has_token)
        self.show_token_button.setEnabled(has_token)
        if not has_token:
            self.show_token_button.setChecked(False)
        self.token_help.setText("Keep your access token private. It is saved locally when you click Save.")

    def set_token_visible(self, visible):
        self.token.setEchoMode(QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password)
        self.show_token_button.setText("Hide" if visible else "Show")
        self.show_token_button.setAccessibleName("Hide access token" if visible else "Show access token")

    def copy_token(self):
        token = self.token.text().strip()
        if token:
            QApplication.clipboard().setText(token)
            self.token_help.setText("Access token copied. Keep it private; other apps may access the clipboard.")

    def generate_token(self):
        ip = self.ip.text().strip()
        if not valid_hub_address(ip):
            self.address_help.setText(
                "Enter the hub's local IP address or hostname first, such as 192.168.1.20. "
                "Use the address by itself.")
            self.ip.setFocus()
            self.ip.selectAll()
            return
        pairing = TokenPairingDialog(self, ip)
        try:
            if pairing.exec() == QDialog.DialogCode.Accepted and pairing.generated_token:
                self.show_token_button.setChecked(False)
                self.token.setEchoMode(QLineEdit.EchoMode.Password)
                self.token.setText(pairing.generated_token)
                self.token_help.setText("New access token received. Click Save to apply it.")
                self.token.setFocus()
        finally:
            pairing.deleteLater()

    def check_accept(self):
        ip = self.ip.text().strip()
        token = self.token.text().strip()
        if not valid_hub_address(ip) or not token:
            QMessageBox.warning(self, "Invalid settings", "Enter a valid hub IP/hostname and an access token.")
            return
        self.accept()


class ApprovalDialog(QDialog):
    def __init__(self, parent, lights, sensors, approved_lights, approved_sensors):
        super().__init__(parent)
        self.setWindowTitle("Choose my lights and sensors")
        outer, _, layout = scrollable_form(self)
        message = wrapped_label(
            "Only check devices that belong to you. Room headings cannot be checked, "
            "so selecting a whole shared room accidentally is impossible.\n"
            "New devices always start unapproved. Sensor readings are read-only."
        )
        layout.addWidget(message)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Device", "Status"])
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.tree)
        self._populate("LIGHTS", lights, approved_lights)
        self._populate("CLIMATE SENSORS (read-only)", sensors, approved_sensors)
        self.tree.expandAll()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.check_save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.selection_lights = {}
        self.selection_sensors = {}
        fit_window(self, 74, 34)

    def _populate(self, kind, devices, approvals):
        section = QTreeWidgetItem(self.tree, [kind, ""])
        grouped = defaultdict(list)
        for d in devices:
            grouped[(d.room_id, d.room_name)].append(d)
        for (rid, name), members in sorted(grouped.items(), key=lambda item: item[0][1].casefold()):
            room_item = QTreeWidgetItem(section, [f"{name} ({len(members)})", ""])
            room_item.setToolTip(0, room_item.text(0))
            # Deliberately no checkbox on room headings, even in shared rooms.
            for device in members:
                status = "Online" if device.reachable else "Offline"
                if kind.startswith("CLIMATE"):
                    values = []
                    if device.temperature is not None:
                        values.append(f"{device.temperature:.1f} °C")
                    if device.humidity is not None:
                        values.append(f"{device.humidity:g}% RH")
                    if values:
                        status += " · " + " / ".join(values)
                child = QTreeWidgetItem(room_item, [device.name, status])
                child.setToolTip(0, device.name)
                child.setToolTip(1, status)
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setData(0, Qt.ItemDataRole.UserRole, ("sensor" if kind.startswith("CLIMATE") else "light", device.id, device.room_id))
                child.setCheckState(0, Qt.CheckState.Checked if approvals.get(device.id) == device.room_id else Qt.CheckState.Unchecked)

    def check_save(self):
        lights, sensors, names = {}, {}, []
        for i in range(self.tree.topLevelItemCount()):
            section = self.tree.topLevelItem(i)
            for j in range(section.childCount()):
                room = section.child(j)
                for k in range(room.childCount()):
                    item = room.child(k)
                    if item.checkState(0) == Qt.CheckState.Checked:
                        kind, device_id, rid = item.data(0, Qt.ItemDataRole.UserRole)
                        (sensors if kind == "sensor" else lights)[device_id] = rid
                        names.append(f"• {kind}: {room.text(0)} / {item.text(0)}")
        summary = "\n".join(names) or "Nothing selected: all controls remain disabled."
        dialog, layout = text_dialog(self, "Confirm selection", summary)
        layout.insertWidget(0, wrapped_label(
            f"Approve {len(lights)} light(s) for control and {len(sensors)} sensor(s) for viewing?"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        fit_window(dialog, 70, min(26, max(12, len(names) + 7)))
        reply = dialog.exec()
        dialog.deleteLater()
        if reply == QDialog.DialogCode.Accepted:
            self.selection_lights, self.selection_sensors = lights, sensors
            self.accept()


class ClickToJumpSlider(QSlider):
    """Click-to-jump track and notify the window about mouse-wheel edits.

    Qt changes the slider value on wheel input without emitting sliderReleased.
    The main window owns the debounce timer, so periodic tab re-rendering cannot
    destroy a pending approved-light command when this slider is replaced.
    """
    trackClicked = Signal(int)
    wheelAdjusted = Signal(int)

    def wheelEvent(self, event):
        was = self.value()
        super().wheelEvent(event)
        if self.isEnabled() and self.value() != was:
            self.wheelAdjusted.emit(self.value())

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            option = QStyleOptionSlider()
            self.initStyleOption(option)
            handle = self.style().subControlRect(
                QStyle.ComplexControl.CC_Slider, option,
                QStyle.SubControl.SC_SliderHandle, self)
            point = event.position().toPoint()
            if not handle.contains(point):
                groove = self.style().subControlRect(
                    QStyle.ComplexControl.CC_Slider, option,
                    QStyle.SubControl.SC_SliderGroove, self)
                if self.orientation() == Qt.Orientation.Horizontal:
                    span = max(1, groove.width() - handle.width())
                    position = point.x() - groove.x() - handle.width() // 2
                else:
                    span = max(1, groove.height() - handle.height())
                    position = point.y() - groove.y() - handle.height() // 2
                value = QStyle.sliderValueFromPosition(
                    self.minimum(), self.maximum(), position, span, option.upsideDown)
                self.setValue(value)
                self.trackClicked.emit(value)
                event.accept()
                return
        super().mousePressEvent(event)


TEMPERATURE_COLOURS = (
    (1800, (255, 151, 63)),
    (2700, (255, 191, 128)),
    (4000, (255, 225, 194)),
    (5500, (255, 244, 235)),
    (6500, (232, 242, 255)),
    (8500, (197, 219, 255)),
)


def temperature_colour(kelvin):
    kelvin = max(TEMPERATURE_COLOURS[0][0], kelvin)
    for (low, warm), (high, cool) in zip(TEMPERATURE_COLOURS, TEMPERATURE_COLOURS[1:]):
        if kelvin <= high:
            fraction = (kelvin - low) / (high - low)
            return QColor(*(round(a + fraction * (b - a)) for a, b in zip(warm, cool)))
    return QColor(*TEMPERATURE_COLOURS[-1][1])


class TemperatureSlider(ClickToJumpSlider):
    keyboardAdjusted = Signal(int)

    def __init__(self, light_ids, readout, room_hint=None):
        super().__init__(Qt.Orientation.Horizontal)
        self.light_ids = tuple(light_ids)
        self.readout = readout
        self.room_hint = room_hint
        self._status = ""
        self._dimmed = False
        self.setAccessibleName("Room colour temperature" if room_hint is not None else "Colour temperature")
        states = ["RGB mode", "Outside range", "Temperature unavailable", "8500 K"]
        if room_hint is not None:
            states += ["Mixed modes", "Mixed temperatures"]
        self.readout.setMinimumWidth(max(self.readout.fontMetrics().horizontalAdvance(state) for state in states))
        self.valueChanged.connect(self._show_temperature)
        self.sliderPressed.connect(self._show_temperature)
        self.trackClicked.connect(self._show_temperature)

    def update_from_lights(self, lights):
        known_temperatures = [
            light.temperature for light in lights
            if light.color_mode != "color" and light.temperature is not None
            and light.temp_low <= light.temperature <= light.temp_high
        ] if self.isEnabled() else []
        rgb_count = sum(light.color_mode == "color" for light in lights)
        outside_count = sum(
            light.color_mode != "color" and light.temperature is not None
            and light.temp_low is not None and light.temp_high is not None
            and not light.temp_low <= light.temperature <= light.temp_high
            for light in lights
        )
        self._dimmed = bool(rgb_count or outside_count)
        if not self.isEnabled():
            self._status = "—"
        elif rgb_count == len(lights):
            self._status = "RGB mode"
        elif outside_count == len(lights):
            self._status = "Outside range"
        elif self._dimmed:
            self._status = "Mixed modes"
        elif len(set(known_temperatures)) > 1:
            self._status = "Mixed temperatures"
        elif len(known_temperatures) < len(lights):
            self._status = "Temperature unavailable"
        else:
            self._status = ""

        if not self.isSliderDown():
            if known_temperatures:
                self.setValue(round(sum(known_temperatures) / len(known_temperatures)))
            else:
                self.setValue((self.minimum() + self.maximum()) // 2)
        hint = ""
        if self._dimmed and self.isEnabled():
            hint = ("Adjusting sets all approved lights in this room to white."
                    if self.room_hint is not None else "Adjust to switch this light to white.")
        if self.room_hint is not None:
            self.room_hint.setVisible(bool(hint))
        description = ". ".join(filter(None, (self._status, hint))) if self.isEnabled() else ""
        self.setToolTip(description)
        self.setAccessibleDescription(description)
        self.clear_preview()

    def clear_preview(self):
        if self._status and not self.isSliderDown():
            self.readout.setText(self._status)
        else:
            self._show_temperature()
        self.update()

    def _show_temperature(self):
        self.readout.setText(f"{self.value()} K")
        if self._dimmed:
            self.update()

    def keyPressEvent(self, event):
        previous = self.value()
        super().keyPressEvent(event)
        if self.value() != previous:
            self.keyboardAdjusted.emit(self.value())

    def paintEvent(self, event):
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        control = QStyle.ComplexControl.CC_Slider
        groove = QRectF(self.style().subControlRect(control, option, QStyle.SubControl.SC_SliderGroove, self))
        handle = QRectF(self.style().subControlRect(control, option, QStyle.SubControl.SC_SliderHandle, self))
        if not groove.isValid() or not handle.isValid():
            return

        # Use the native geometry for hit testing and dragging, and paint the
        # whole control together so no native groove shows around the thumb.
        centre = handle.center()
        thickness = max(4.0, min(handle.width(), handle.height()) / 2.4)
        track = QRectF(groove.left() + 1, centre.y() - thickness / 2, groove.width() - 2, thickness)
        warm_end, cool_end = track.left(), track.right()
        if option.upsideDown:
            warm_end, cool_end = cool_end, warm_end
        gradient = QLinearGradient(warm_end, 0, cool_end, 0)
        low, high = self.minimum(), self.maximum()
        stops = [low, *(k for k, _ in TEMPERATURE_COLOURS if low < k < high), high]
        muted = self.palette().mid().color()
        for kelvin in stops:
            colour = temperature_colour(kelvin) if self.isEnabled() else muted
            if self._dimmed and self.isEnabled():
                colour = QColor(*(round(0.30 * channel + 0.70 * neutral)
                                 for channel, neutral in zip(colour.getRgb()[:3], muted.getRgb()[:3])))
            gradient.setColorAt((kelvin - low) / max(1, high - low), colour)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        edge = self.palette().windowText().color()
        edge.setAlpha(140)
        painter.setPen(QPen(edge, 1))
        painter.setBrush(gradient)
        painter.drawRoundedRect(track, thickness / 2, thickness / 2)

        thumb = handle.adjusted(1.5, 1.5, -1.5, -1.5)
        thumb.setHeight(min(thumb.height(), thickness + 7))
        thumb.moveCenter(centre)
        thumb_edge = self.palette().windowText().color()
        stroke = 1
        if self.hasFocus():
            thumb_edge = self.palette().highlight().color()
            stroke = 2
        thumb_colour = temperature_colour(self.value()) if self.isEnabled() else muted
        if self._dimmed and self.readout.text() == self._status:
            thumb_colour = self.palette().button().color()
        painter.setPen(QPen(thumb_edge, stroke))
        painter.setBrush(thumb_colour)
        painter.drawRoundedRect(thumb, 3, 3)


def mouse_control_pixmap(palette, middle_click=False):
    """Small gesture illustrations using the current Qt palette."""
    pixmap = QPixmap(30, 34)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    ink = palette.windowText().color()
    painter.setPen(QPen(ink, 1.5))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(5, 2, 20, 30, 9, 9)
    painter.drawLine(15, 2, 15, 6)
    painter.setBrush(palette.highlight().color() if middle_click else ink)
    painter.drawRoundedRect(12, 6, 6, 10, 3, 3)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    if not middle_click:
        painter.drawLine(15, 20, 12, 23)
        painter.drawLine(15, 20, 18, 23)
        painter.drawLine(15, 27, 12, 24)
        painter.drawLine(15, 27, 18, 24)
    painter.end()
    return pixmap


class TraySettingsDialog(QDialog):
    def __init__(self, parent, room_id, rooms, icon_name="", default_icon=None):
        super().__init__(parent)
        self.setWindowTitle("System tray settings")
        self.icon_name = icon_name
        self._default_icon = default_icon
        outer, scroll, layout = scrollable_form(self)

        layout.addWidget(section_heading("Tray controls"))
        self.room = QComboBox()
        self.room.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.room.setAccessibleName("Tray control selection")
        self.room.currentTextChanged.connect(self.room.setToolTip)
        self.room.setPlaceholderText("Selected room unavailable" if room_id else "Choose lights to control")
        self.room.addItem("All approved lights", ALL_APPROVED)
        for rid, name in sorted(rooms.items(), key=lambda p: p[1].casefold()):
            self.room.addItem(name, rid)
            self.room.setItemData(self.room.count() - 1, name, Qt.ItemDataRole.ToolTipRole)
        idx = self.room.findData(room_id)
        self.room.setCurrentIndex(idx)
        layout.addWidget(self.room)
        layout.addWidget(wrapped_label("Choose a room or all approved lights for tray controls and the dynamic icon."))
        layout.addWidget(section_divider())

        layout.addWidget(section_heading("Tray appearance"))
        appearance = QFrame()
        appearance.setFrameShape(QFrame.Shape.StyledPanel)
        appearance_layout = QVBoxLayout(appearance)
        appearance_layout.setContentsMargins(12, 12, 12, 12)
        appearance_layout.setSpacing(12)
        appearance_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        appearance.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

        icon_row = QHBoxLayout()
        icon_row.setSpacing(12)
        self.icon_preview = QLabel()
        self.icon_preview.setFixedSize(64, 64)
        self.icon_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_preview.setFrameShape(QFrame.Shape.StyledPanel)
        self.icon_preview.setAccessibleName("Selected tray icon preview")
        icon_row.addWidget(self.icon_preview, alignment=Qt.AlignmentFlag.AlignTop)
        details = QVBoxLayout()
        details.setSpacing(6)
        self.icon_label = wrapped_label("")
        policy = self.icon_label.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
        self.icon_label.setSizePolicy(policy)
        font = self.icon_label.font()
        font.setBold(True)
        self.icon_label.setFont(font)
        details.addWidget(self.icon_label)
        self.icon_status = wrapped_label("")
        details.addWidget(self.icon_status)
        details.addStretch()
        icon_row.addLayout(details, stretch=1)
        appearance_layout.addLayout(icon_row)

        # Give the buttons their own full-width widget. The outer layout must
        # reserve their height independently of the fixed-size icon preview.
        action_row = QWidget(appearance)
        actions = WrappingLayout()
        action_row.setLayout(actions)
        self.browse_button = QPushButton("Choose icon file…")
        self.browse_button.setToolTip("Choose a Windows .ico file")
        self.browse_button.setAutoDefault(False)
        self.browse_button.clicked.connect(self.pick_icon)
        actions.addWidget(self.browse_button)
        self.restore_button = QPushButton("Restore default icon")
        self.restore_button.setAccessibleName("Restore default dynamic icon")
        self.restore_button.setAutoDefault(False)
        self.restore_button.clicked.connect(self.restore_default_icon)
        actions.addWidget(self.restore_button)
        # Keep a shared, font-aware height floor for both buttons.
        icon_buttons = (self.browse_button, self.restore_button)
        button_height = max(32, *(max(button.sizeHint().height(),
                                      button.fontMetrics().height() + 12)
                                  for button in icon_buttons))
        for button in icon_buttons:
            button.setMinimumHeight(button_height)
            button.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        appearance_layout.addWidget(action_row)

        # Reserve a separate notice block, with a genuine 12px gap above it.
        notice = QWidget(appearance)
        notice_row = QHBoxLayout(notice)
        notice_row.setContentsMargins(0, 0, 0, 0)
        notice_row.setSpacing(8)
        self.icon_notice_symbol = QLabel()
        notice_row.addWidget(self.icon_notice_symbol, alignment=Qt.AlignmentFlag.AlignTop)
        self.icon_notice = wrapped_label(
            "Custom icons do not reflect whether your lights are on or off. "
            "Restore the default Lysnav icon to re-enable dynamic lighting status."
        )
        notice_row.addWidget(self.icon_notice, stretch=1)
        appearance_layout.addWidget(notice)
        layout.addWidget(appearance)

        layout.addWidget(section_heading("Mouse controls"))
        layout.addWidget(wrapped_label("Use the Lysnav icon by the clock. If hidden, move it out of the system tray overflow."))
        controls = wrapping_form()
        self._gesture_icons = []
        for name, explanation, middle_click in (
            ("Scroll", "Adjust brightness by 5% per notch for approved lights already on.", False),
            ("Middle-click", "Toggle the selected approved lights together.", True),
        ):
            title = QWidget()
            title_layout = QHBoxLayout(title)
            title_layout.setContentsMargins(0, 0, 0, 0)
            glyph = QLabel()
            glyph.setPixmap(mouse_control_pixmap(self.palette(), middle_click))
            self._gesture_icons.append((glyph, middle_click))
            title_layout.addWidget(glyph)
            title_layout.addWidget(QLabel(name))
            controls.addRow(title, wrapped_label(explanation))
        layout.addLayout(controls)
        layout.addWidget(wrapped_label("Only approved lights are affected by these controls."))
        layout.addStretch()
        outer.addWidget(section_divider())
        inspect = QPushButton("Input diagnostics…")
        inspect.setAutoDefault(False)
        inspect.clicked.connect(lambda: parent.show_tray_diagnostics(dialog_parent=self))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Save).setDefault(True)
        outer.addLayout(wrapping_pair(inspect, buttons))
        self.room.currentIndexChanged.connect(self.update_icon_preview)
        self.update_icon_preview()
        fit_window(self, 84, scroll=scroll)

    def update_icon_preview(self):
        custom = custom_tray_qicon(self.icon_name)
        has_custom = bool(self.icon_name)
        valid_custom = has_custom and not custom.pixmap(48, 48).isNull()
        if valid_custom:
            icon = custom
            self.icon_label.setText(Path(self.icon_name).name)
            self.icon_status.setText("Dynamic lighting status disabled")
        else:
            if self._default_icon is not None:
                icon = self._default_icon(self.room.currentData())
            else:
                icon = QIcon(str(ASSETS / panel_icon_filename()))
            self.icon_label.setText("Table Lamp (default)")
            self.icon_status.setText("Custom icon unavailable; using the default dynamic icon"
                                     if has_custom else "Dynamic lighting status enabled")
        self.icon_label.setToolTip(self.icon_name or "")
        self.icon_preview.setPixmap(icon.pixmap(48, 48))
        self.restore_button.setEnabled(has_custom)
        symbol = (QStyle.StandardPixmap.SP_MessageBoxWarning if has_custom
                  else QStyle.StandardPixmap.SP_MessageBoxInformation)
        self.icon_notice_symbol.setPixmap(self.style().standardIcon(symbol).pixmap(18, 18))

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange and hasattr(self, "_gesture_icons"):
            for glyph, middle_click in self._gesture_icons:
                glyph.setPixmap(mouse_control_pixmap(self.palette(), middle_click))
            if hasattr(self, "restore_button"):
                self.update_icon_preview()

    def restore_default_icon(self):
        self.icon_name = ""
        self.update_icon_preview()

    def pick_icon(self):
        # Qt uses the native Windows file-open dialog unless explicitly disabled.
        filename, _ = QFileDialog.getOpenFileName(
            self, "Choose tray icon", self.icon_name or str(Path.home()),
            "Windows icon files (*.ico)")
        if not filename:
            return
        filename = str(Path(filename).absolute())
        if custom_tray_qicon(filename).isNull():
            QMessageBox.warning(self, "Invalid icon", "Select a valid Windows .ico file.")
            return
        self.icon_name = filename
        self.update_icon_preview()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.config = read_config()
        self.lights = []
        self.sensors = []
        self._tray_state_stale = True
        self._panel_icon_cache = {}
        self._custom_icon_path = None
        self._custom_panel_icon = QIcon()
        self.setWindowIcon(QIcon(str(Path(__file__).parent / "assets" / "lysnav-logo.svg")))
        self.sensor_warning = ""
        self.worker = None
        self.color_dialog = None
        self._tray_pending_level = None
        self._tray_pending_ids = ()
        self._tray_scroll_accumulator = 0
        self._tray_last_wheel = None
        self._tray_last_middle = False
        self._tray_brightness_timer = QTimer(self)
        self._tray_brightness_timer.setSingleShot(True)
        self._tray_brightness_timer.setInterval(600)  # feedback remains visible before sending
        self._tray_brightness_timer.timeout.connect(self.flush_tray_brightness)
        # This timer lives on the persistent main window rather than a slider
        # widget recreated by each hub refresh. Wheel edits cannot get dropped
        # merely because the tabs were rebuilt before the debounce elapsed.
        self._slider_wheel_pending = {}
        self._slider_wheel_timer = QTimer(self)
        self._slider_wheel_timer.setSingleShot(True)
        self._slider_wheel_timer.setInterval(250)
        self._slider_wheel_timer.timeout.connect(self.flush_slider_wheel)
        self._really_quit = False
        self._quit_shortcut = QShortcut(QKeySequence("Ctrl+Q"), self)
        self._quit_shortcut.activated.connect(self.quit_application)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(30_000)
        self.refresh_timer.timeout.connect(self.refresh)

        central = QWidget()
        self.setCentralWidget(central)
        _, _, layout = scrollable_form(central)
        layout.addWidget(section_heading("Lysnav  /  My lights"))
        header = WrappingLayout()
        self.approvals_btn = QPushButton("Choose devices")
        self.approvals_btn.clicked.connect(self.choose_devices)
        self.approvals_btn.setEnabled(False)
        header.addWidget(self.approvals_btn)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        header.addWidget(self.refresh_btn)
        self.tray_btn = QPushButton("Tray settings")
        self.tray_btn.clicked.connect(self.change_tray_settings)
        header.addWidget(self.tray_btn)
        self.connect_btn = QPushButton("Hub settings")
        self.connect_btn.clicked.connect(self.change_connection)
        header.addWidget(self.connect_btn)
        self.about_btn = QPushButton("About")
        self.about_btn.clicked.connect(self.show_about)
        header.addWidget(self.about_btn)
        # No extra top-level menu bar: tray diagnostics are in Tray settings.
        # Ctrl+Q remains an emergency quit shortcut if the tray is unavailable.
        layout.addLayout(header)

        self.explainer = wrapped_label("Only approved lights can be controlled. Climate sensors are read-only.")
        layout.addWidget(self.explainer)
        self.tabs = QTabWidget()
        self.tabs.setMinimumHeight(self.tabs.fontMetrics().lineSpacing() * 6)
        self.tabs.setElideMode(Qt.TextElideMode.ElideRight)
        layout.addWidget(self.tabs, stretch=1)
        welcome = wrapped_label("Connect to DIRIGERA and choose your lights to begin.")
        welcome.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tabs.addTab(welcome, "Start")
        self.status = wrapped_label("No hub configured")
        self.status.setObjectName("status")
        layout.addWidget(self.status)
        fit_window(self, 94, 42)
        self._create_tray()
        if self.config.get("ip") and self.config.get("token"):
            self.start_worker()
        else:
            QTimer.singleShot(150, self.change_connection)

    def _create_tray(self):
        self._tray_menu = self.create_tray_menu()
        self.tray = QSystemTrayIcon(self)
        self._panel_icon_name = None
        self._sync_panel_icon()
        self.tray.setContextMenu(self._tray_menu)
        self.tray.activated.connect(self.on_tray_activation)
        self.tray_wheel = TrayWheelHook(self.tray, self._tray_menu)
        # Deliver feedback and lighting work after the Windows hook returns.
        self.tray_wheel.wheel.connect(self.on_tray_scroll, Qt.ConnectionType.QueuedConnection)
        self.update_tray_menu()
        self.update_tray_tooltip()
        if sys.platform == "win32":
            self.tray.show()
            self.tray_wheel.start()

    def _default_panel_qicon(self, selection):
        active = any(light.on for light in tray_power_targets(self.lights, self.config["approved"], selection))
        filename = panel_icon_filename(active)
        if filename not in self._panel_icon_cache:
            self._panel_icon_cache[filename] = native_tray_icon(filename)
        return filename, self._panel_icon_cache[filename]

    def _sync_panel_icon(self):
        path = self.config.get("tray_icon_path", "")
        # Decode only on startup or a new selection; do not reread user files
        # during every brightness gesture or hub refresh.
        if path != self._custom_icon_path:
            self._custom_icon_path = path
            self._custom_panel_icon = custom_tray_qicon(path)
        if not self._custom_panel_icon.isNull():
            key, icon = ("custom", path), self._custom_panel_icon
        else:
            filename, icon = self._default_panel_qicon(self.config.get("tray_room"))
            key = ("default", filename)
        if key != self._panel_icon_name:
            self.tray.setIcon(icon)
            self._panel_icon_name = key

    def nativeEvent(self, event_type, message):
        if sys.platform == "win32" and hasattr(self, "_panel_icon_name"):
            from ctypes.wintypes import MSG
            # WM_SETTINGCHANGE and WM_THEMECHANGED are broadcast to top-level windows.
            if MSG.from_address(int(message)).message in (0x001A, 0x031A):
                self._sync_panel_icon()
        return super().nativeEvent(event_type, message)

    def on_tray_activation(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_and_raise()
        elif reason == QSystemTrayIcon.ActivationReason.MiddleClick:
            self._tray_last_middle = True
            self.on_tray_middle()

    def _tray_selection_name(self):
        selected = self.config.get("tray_room")
        if selected == ALL_APPROVED:
            return "All approved lights"
        return self._approved_rooms().get(selected, "Selected room unavailable" if selected else "No tray selection")

    def _tray_tooltip(self):
        rid = self.config.get("tray_room")
        name = self._tray_selection_name()
        sensors = approved_climate(self.sensors, self.config["approved_sensors"])
        matching = [s for s in sensors if s.room_id == rid and s.reachable] if rid else []
        if not matching:
            matching = [s for s in sensors if s.reachable]
        temperature = next((s.temperature for s in matching if s.temperature is not None), None)
        humidity = next((s.humidity for s in matching if s.humidity is not None), None)
        summary = []
        if temperature is not None:
            summary.append(f"{temperature:.1f} °C")
        if humidity is not None:
            summary.append(f"{humidity:g}% RH")
        climate = " · ".join(summary) if summary else "No approved climate readings"
        # Keep climate readings before the room name within the Shell tooltip limit.
        warning = "\nLight status unverified / last known" if self._tray_state_stale else ""
        return f"{climate}{warning}\n{name}"

    def update_tray_tooltip(self):
        self._sync_panel_icon()
        self.tray.setToolTip(self._tray_tooltip())

    def _approved_rooms(self):
        return {light.room_id: light.room_name
                for light in approved_visible(self.lights, self.config['approved'])}

    def create_tray_menu(self):
        menu = QMenu(self)
        menu.addAction("Show controls", self.show_and_raise)
        self._room_power_action = menu.addAction("Toggle selected lights (middle-click)", self.on_tray_middle)
        menu.addAction("Refresh", self.refresh)
        self._room_menu = menu.addMenu("Tray controls for…")
        self._room_menu.setToolTipsVisible(True)
        self._room_menu.triggered.connect(self.room_action_triggered)
        self._menu_rooms = None
        menu.addAction("Tray settings", self.change_tray_settings)
        menu.addAction(f"About {APP_NAME}…", self.show_about)
        menu.addSeparator()
        menu.addAction(f"Quit {APP_NAME}", self.quit_application)
        return menu

    def room_action_triggered(self, action):
        self.set_tray_room(action.data())

    def update_tray_menu(self):
        selected = self.config.get("tray_room")
        power = tray_power_targets(self.lights, self.config["approved"], selected)
        self._room_power_action.setEnabled(bool(power))
        rooms = self._approved_rooms()
        if rooms != self._menu_rooms:
            self._room_menu.clear()
            choices = [(ALL_APPROVED, "All approved lights")]
            choices.extend(sorted(rooms.items(), key=lambda pair: pair[1].casefold()))
            for room_id, name in choices:
                action = self._room_menu.addAction(name)
                action.setData(room_id)
                action.setToolTip(name)
                action.setCheckable(True)
            self._menu_rooms = rooms
        for action in self._room_menu.actions():
            action.setChecked(action.data() == selected)

    def set_tray_room(self, rid):
        self._tray_brightness_timer.stop()
        self._tray_pending_level = None
        self._tray_pending_ids = ()
        self._tray_scroll_accumulator = 0
        self.config["tray_room"] = rid
        write_config(self.config)
        self.update_tray_tooltip()
        self.update_tray_menu()

    def change_tray_settings(self):
        rooms = self._approved_rooms()
        dialog = TraySettingsDialog(self, self.config.get("tray_room"), rooms,
                                    icon_name=self.config.get("tray_icon_path", ""),
                                    default_icon=lambda selection: self._default_panel_qicon(selection)[1])
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.config["tray_icon_path"] = dialog.icon_name
            # Reload even if the same file was selected (it may have changed).
            self._custom_icon_path = None
            self._panel_icon_name = None
            self.set_tray_room(dialog.room.currentData() or self.config.get("tray_room"))
        dialog.deleteLater()

    def show_about(self):
        def installed_version(name):
            try:
                return package_version(name)
            except PackageNotFoundError:
                return 'not detected'

        from PySide6.QtWidgets import QTextBrowser
        dialog = QDialog(self)
        dialog.setWindowTitle(f'About {APP_NAME}')
        outer, _, layout = scrollable_form(dialog)
        heading = section_heading(f'{APP_NAME}  ·  {VERSION}')
        logo = QLabel()
        logo.setPixmap(QIcon(str(ASSETS / "lysnav-logo.svg")).pixmap(
            QSize(56, 56), dialog.devicePixelRatioF()))
        header = QHBoxLayout()
        header.addWidget(logo)
        header.addWidget(heading, stretch=1)
        layout.addLayout(header)
        text = QTextBrowser()
        text.setOpenExternalLinks(True)
        text.setHtml(f"""
            <p>A local controller for IKEA DIRIGERA on Windows.</p>
            <p>Lysnav is an independent third-party application and is not affiliated with, endorsed by, or sponsored by IKEA.</p>
            <p><b>Features:</b> approved-only lighting controls, live colour selection,
            Kelvin and brightness sliders, read-only climate sensors, and Windows
            system tray controls.</p>
            <p><b>Development:</b> <a href='https://github.com/alsiniz'>alsiniz</a> (creator of Lysnav for KDE Plasma), with implementation and troubleshooting assistance from <a href='https://openai.com/'>OpenAI ChatGPT</a>.</p>
            <p><b>Open-source components:</b></p>
            <ul>
            <li><a href='https://github.com/Leggin/dirigera'>dirigera</a>
                {installed_version('dirigera')}, MIT, by Leggin and contributors;
                reverse-engineered DIRIGERA local HTTPS API.</li>
            <li><a href='https://doc.qt.io/qtforpython-6/'>PySide6 / Qt for Python</a>
                {installed_version('PySide6-Essentials')}, LGPLv3/GPLv3/commercial options,
                by The Qt Company and Qt for Python contributors.</li>
            </ul>
            <p>This Windows port is based on Lysnav for KDE Plasma and shared under the <b>MIT License</b>.
            Free to use, modify, share, and sell, with
            preservation of the copyright and license notice. See the LICENSE file for exact terms.
            Upstream components keep their own licenses. The upstream library maintainers do not endorse this project.</p>
            <p><b>Keep your hub access token private.</b> This app only sends
            lighting commands to the bulbs you explicitly approve. This helps
            prevent accidentally changing other residents’ lights.</p>
            <p>However, the IKEA hub does not limit the access token to those
            approved bulbs. Anyone who obtains your token could use another
            program to control <b>any device connected to the hub</b>.
            Treat the token like a password and never share it.</p>
        """)
        layout.addWidget(text)
        # Use a native Qt control rather than Ko-fi's web-only JavaScript widgets.
        # It opens the user's browser only when clicked; no network access is
        # needed to open the About dialog or run the application.
        if KOFI_URL:
            support = QPushButton("Support on Ko-fi")
            support.setToolTip("Opens the original project's Ko-fi page in your default browser")
            support.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(KOFI_URL)))
            layout.addWidget(support, alignment=Qt.AlignmentFlag.AlignHCenter)
        licenses = Path(__file__).resolve().parent / "licenses"
        if getattr(sys, "frozen", False) and not licenses.is_dir():
            licenses = Path(sys.executable).parent / "licenses"
        if licenses.is_dir():
            notices = QPushButton("Licenses and notices")
            notices.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(licenses))))
            layout.addWidget(notices, alignment=Qt.AlignmentFlag.AlignHCenter)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.accept)
        outer.addWidget(buttons)
        fit_window(dialog, 72, 32)
        dialog.exec()
        dialog.deleteLater()

    def show_tray_diagnostics(self, dialog_parent=None):
        bulbs = tray_power_targets(self.lights, self.config["approved"], self.config.get("tray_room"))
        eligible = tray_targets(self.lights, self.config["approved"], self.config.get("tray_room"))
        registration = "Installed" if self.tray_wheel.installed else "Unavailable"
        active_count = sum(bool(light.on) for light in bulbs)
        mode, variant = self._panel_icon_name or ("unknown", "")
        variant = "Custom ICO" if mode == "custom" else variant
        message = (
            f"System tray available: {QSystemTrayIcon.isSystemTrayAvailable()}\n"
            f"Icon visible by the clock: {self.tray.geometry().isValid()}\n"
            f"Tray icon mode: {mode}\n"
            f"Selected icon: {variant}\n"
            f"Selected lights on: {active_count}/{len(bulbs)}\n"
            f"Light status: {'Unverified / last known' if self._tray_state_stale else 'Current snapshot'}\n"
            f"Wheel hook: {registration}\n"
            f"Wheel events over the icon: {self.tray_wheel.count}\n"
            f"Last wheel delta: {self._tray_last_wheel}\n"
            f"Middle-click received: {self._tray_last_middle}\n"
            f"Wheel hook error: {self.tray_wheel.error or 'None'}\n"
            f"Tray selection: {self._tray_selection_name()}\n"
            f"Approved lights in selection: {len(bulbs)}\n"
            f"On and dimmable: {len(eligible)}\n\n"
            "If wheel input is unavailable, move the icon out of the system tray overflow "
            "and try reinstalling the wheel hook."
        )
        dialog, layout = text_dialog(dialog_parent or self, "System tray diagnostics", message)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        retry = QPushButton("Restart wheel hook")
        retry.clicked.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addLayout(wrapping_pair(retry, buttons))
        fit_window(dialog, 80, 25)
        result = dialog.exec()
        dialog.deleteLater()
        if result == QDialog.DialogCode.Accepted and QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
            self.tray_wheel.restart()
            self.status.setText(self.tray_wheel.error or "Tray wheel hook restarted")

    def _tray_feedback(self, text):
        """Show the new brightness next to the tray before sending it to the hub."""
        QToolTip.showText(QCursor.pos(), f"<p>{escape(text)}</p>", self, msecShowTime=1400)

    def on_tray_scroll(self, delta):
        self._tray_last_wheel = delta
        if not self.worker:
            self.status.setText("Tray scroll received, but the hub is not connected")
            return
        targets = tray_targets(self.lights, self.config["approved"],
                               self.config.get("tray_room"))
        if not targets:
            self.status.setText("Tray scroll received, but no approved ON lights in the tray selection")
            self._tray_feedback("No approved ON bulbs available for dimming")
            return
        ids = tuple(l.id for l in targets)
        if self._tray_pending_ids != ids:
            self._tray_pending_level = None
            self._tray_scroll_accumulator = 0
        self._tray_pending_ids = ids
        steps, self._tray_scroll_accumulator = wheel_steps(int(delta), self._tray_scroll_accumulator)
        if not steps:
            self.status.setText(f"Tray scroll received: partial delta {delta}")
            return
        level = (self._tray_pending_level if self._tray_pending_level is not None
                 else stepped_brightness([l.brightness for l in targets], 0))
        self._tray_pending_level = max(1, min(100, level + steps * 5))
        self._tray_brightness_timer.start()  # debounce rapid wheel turns
        selection = self._tray_selection_name()
        self.status.setText(f"{selection} · target brightness {self._tray_pending_level}%")
        self._tray_feedback(f"{selection} · {self._tray_pending_level}%")

    def flush_tray_brightness(self):
        if self._tray_pending_level is None or not self._tray_pending_ids:
            return
        ids = tuple(l.id for l in tray_targets(self.lights, self.config["approved"],
                                                self.config.get("tray_room")))
        if ids != self._tray_pending_ids:
            self._tray_pending_level = None
            self._tray_pending_ids = ()
            self.update_tray_tooltip()
            return
        self.control(ids, "brightness", self._tray_pending_level)

    def on_tray_middle(self):
        if not self.worker:
            self.status.setText("Middle-click received, but not connected")
            return
        self._tray_brightness_timer.stop()
        self._tray_pending_level = None
        self._tray_pending_ids = ()
        self._tray_scroll_accumulator = 0
        self.update_tray_tooltip()
        selection = self.config.get("tray_room")
        targets = tray_power_targets(self.lights, self.config["approved"], selection)
        if not targets:
            self.status.setText("Middle-click received; no approved lights in the tray selection")
            return
        if not all(l.reachable and l.can("power") for l in targets):
            self.status.setText("Toggle refused: a selected light is offline or lacks on/off control")
            self._tray_feedback("Toggle unavailable: check the selected lights")
            return
        ids = [l.id for l in targets]
        self.worker.submit("toggle", ids, selection)  # Re-fetch and validate every light before writing.
        self.status.setText(f"Toggle requested for {len(ids)} approved lights")

    def show_and_raise(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def change_connection(self):
        dialog = ConnectionDialog(self, self.config)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            new_ip = dialog.ip.text().strip()
            new_token = dialog.token.text().strip()
        finally:
            dialog.deleteLater()
        old = self.worker
        if old:
            # Avoid leaving a system hook installed while the GUI thread waits.
            self.tray_wheel.stop()
            try:
                old.stop()
                stopped = old.wait(30_000)
            finally:
                if self.tray.isVisible():
                    self.tray_wheel.start()
            if not stopped:
                QMessageBox.warning(self, "Hub busy", "An existing request is still running. Try changing hubs again after it finishes.")
                return
        self.worker = None
        self._slider_wheel_timer.stop()
        self._slider_wheel_pending.clear()
        same_hub = new_ip == self.config.get("ip", "")
        self.config = {"ip": new_ip, "token": new_token,
                       "approved": dict(self.config["approved"]) if same_hub else {},
                       "approved_sensors": dict(self.config["approved_sensors"]) if same_hub else {},
                       "tray_room": self.config.get("tray_room") if same_hub else None,
                       "tray_icon_path": self.config.get("tray_icon_path", "")}
        write_config(self.config)
        self.lights = []
        self.sensors = []
        self._tray_state_stale = True
        self._tray_pending_level = None
        self._tray_pending_ids = ()
        self._tray_scroll_accumulator = 0
        self.update_tray_tooltip()
        self.clear_tab_pages()
        self.tabs.addTab(QLabel("Discovering lights. No devices have been approved yet."), "Start")
        self.start_worker()

    def start_worker(self):
        self.worker = HubWorker(self.config["ip"], self.config["token"], self.config["approved"])
        self.worker.loaded.connect(self.on_loaded)
        self.worker.color_preview_applied.connect(self.on_color_preview_applied)
        self.worker.completed.connect(self.on_completed)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()
        self.status.setText(f"Connecting to {self.config['ip']}…")
        # Schedule another read even if the initial request fails.
        self.refresh_timer.start()
        self.refresh()

    def refresh(self):
        if self.worker and not self.worker.isFinished():
            self.worker.submit("refresh")

    def on_loaded(self, result):
        lights, sensors, self.sensor_warning = result
        self.lights = lights
        self.sensors = sensors
        self._tray_state_stale = False
        self.approvals_btn.setEnabled(True)
        self.refresh_timer.start()
        self.render_tabs()
        self.update_tray_menu()
        if self._tray_pending_level is not None and self._tray_pending_ids:
            current = {l.id: l for l in lights}
            if all(i in current and abs(current[i].brightness - self._tray_pending_level) <= 1
                   for i in self._tray_pending_ids):
                self._tray_pending_level = None
                self._tray_pending_ids = ()
        self.update_tray_tooltip()
        eligible = approved_visible(lights, self.config["approved"])
        moved_or_missing = len(self.config["approved"]) - len(eligible)
        eligible_sensors = approved_climate(sensors, self.config["approved_sensors"])
        self.status.setText(f"Connected · {len(eligible)} approved light(s) · {len(eligible_sensors)} visible sensor(s)" +
                            (f" · {moved_or_missing} unavailable/relocated light(s)" if moved_or_missing else "") +
                            (f" · {self.sensor_warning}" if self.sensor_warning else ""))

    def on_color_preview_applied(self, ids):
        if self.sender() is not self.worker:
            return
        ids = set(ids)
        changed = {light.id for light in self.lights if light.id in ids and light.color_mode != "color"}
        if changed:
            self.lights = [replace(light, color_mode="color") if light.id in changed else light
                           for light in self.lights]
            current = {light.id: light for light in self.lights}
            for slider in self.tabs.findChildren(TemperatureSlider):
                if changed.intersection(slider.light_ids) and all(i in current for i in slider.light_ids):
                    slider.update_from_lights([current[i] for i in slider.light_ids])

    def on_completed(self, message):
        self.status.setText(message)

    def on_failed(self, error):
        for slider in self.tabs.findChildren(TemperatureSlider):
            slider.clear_preview()
        # A failed request is not evidence that the lights are off.
        self._tray_state_stale = True
        self.update_tray_tooltip()
        self.status.setText(f"Request failed: {error}")

    def choose_devices(self):
        if not self.lights and not self.sensors:
            QMessageBox.information(self, "Not connected", "No supported lights or sensors have been discovered yet.")
            return
        dialog = ApprovalDialog(self, self.lights, self.sensors,
                                self.config["approved"], self.config["approved_sensors"])
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._slider_wheel_timer.stop()
        self._slider_wheel_pending.clear()
        self.config["approved"] = dialog.selection_lights
        self.config["approved_sensors"] = dialog.selection_sensors
        write_config(self.config)
        if self.worker:
            self.worker.set_approved(dialog.selection_lights)
        self._tray_pending_level = None
        self._tray_pending_ids = ()
        self._tray_scroll_accumulator = 0
        self.render_tabs()
        self.update_tray_tooltip()
        self.update_tray_menu()
        self.status.setText(f"Saved: {len(dialog.selection_lights)} controllable light(s), "
                            f"{len(dialog.selection_sensors)} read-only sensor(s).")

    def queue_slider_wheel(self, ids, action, value):
        key = (tuple(ids), action)
        self._slider_wheel_pending[key] = value
        self._slider_wheel_timer.start()

    def cancel_slider_wheel(self, ids, action):
        self._slider_wheel_pending.pop((tuple(ids), action), None)
        if not self._slider_wheel_pending:
            self._slider_wheel_timer.stop()

    def flush_slider_wheel(self):
        pending = self._slider_wheel_pending
        self._slider_wheel_pending = {}
        # Every command passes through the exact same approved-device check
        # as slider dragging and clicking. No direct hub call from a widget.
        for (ids, action), value in pending.items():
            self.control(list(ids), action, value)

    def commit_slider(self, ids, action, value):
        self.cancel_slider_wheel(ids, action)
        self.control(list(ids), action, value)

    def control(self, ids, action, value):
        allowed = self.config["approved"]
        current = {l.id: l for l in approved_visible(self.lights, allowed)}
        if not ids or any(id_ not in current for id_ in ids):
            QMessageBox.warning(self, "Not approved", "This action includes an unapproved or relocated light. Nothing was changed.")
            return
        if self.worker:
            self.worker.submit("control", list(ids), action, value)
            self.status.setText(f"Sending {action} to {len(ids)} approved light(s)…")

    def preview_color(self, ids, value):
        current = {l.id: l for l in approved_visible(self.lights, self.config["approved"])}
        if not ids or any(id_ not in current for id_ in ids):
            self.status.setText("Live preview stopped: a bulb is no longer approved.")
            return
        if self.worker:
            self.worker.submit_preview(list(ids), value)

    def choose_color(self, ids, lights):
        if self.color_dialog is not None:
            self.color_dialog.close()  # Finish the previous target first.
        initial = lights[0]
        dialog = WheelDialog(self, initial.hue, initial.saturation,
                             lambda value, target=tuple(ids): self.preview_color(target, value),
                             lambda: self.refresh())
        self.color_dialog = dialog
        dialog.finished.connect(lambda result, d=dialog:
                                setattr(self, "color_dialog", None) if self.color_dialog is d else None)
        dialog.show()  # NOT exec(): the wheel remains open during live preview.
        dialog.raise_()

    def add_controls(self, layout, lights, is_group=False):
        ids = [l.id for l in lights]
        active = all(l.reachable for l in lights)
        # Only offer group actions supported by every approved bulb in that room.
        for action in ("power", "brightness", "temperature", "color"):
            supported = active and all(l.can(action) for l in lights)
            if action == "power":
                line = QHBoxLayout()
                on = QPushButton("Turn on")
                off = QPushButton("Turn off")
                on.setEnabled(supported)
                off.setEnabled(supported)
                on.clicked.connect(lambda checked=False, ids=ids: self.control(ids, "power", True))
                off.clicked.connect(lambda checked=False, ids=ids: self.control(ids, "power", False))
                line.addWidget(on)
                line.addWidget(off)
                line.addStretch()
                layout.addLayout(line)
            elif action in ("brightness", "temperature"):
                bounds = (1, 100) if action == "brightness" else (
                    max((l.temp_low or 0) for l in lights),
                    min((l.temp_high or 0) for l in lights)
                )
                if action == "temperature" and bounds[0] > bounds[1]:
                    supported = False
                line = QHBoxLayout()
                line.addWidget(QLabel("Brightness" if action == "brightness" else "Warm  ⇄  Cool"))
                label = QLabel()
                label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                room_hint = None
                if action == "temperature":
                    if is_group:
                        room_hint = wrapped_label("Adjusting sets all approved lights in this room to white.")
                    slider = TemperatureSlider(ids, label, room_hint)
                    slider.keyboardAdjusted.connect(lambda value, ids=tuple(ids), a=action:
                                                    self.queue_slider_wheel(ids, a, value))
                    slider.setRange(*((bounds[0], bounds[1]) if 0 < bounds[0] <= bounds[1] else (1, 1)))
                    slider.setEnabled(supported)
                    slider.update_from_lights(lights)
                else:
                    slider = ClickToJumpSlider(Qt.Orientation.Horizontal)
                    slider.setRange(*bounds)
                    slider.setValue(lights[0].brightness)
                    slider.setEnabled(supported)
                    label.setMinimumWidth(68)
                    label.setText(f"{slider.value()}%")
                    slider.valueChanged.connect(lambda v, lbl=label: lbl.setText(f"{v}%"))
                slider.sliderPressed.connect(lambda ids=tuple(ids), a=action: self.cancel_slider_wheel(ids, a))
                slider.sliderReleased.connect(lambda sl=slider, ids=tuple(ids), a=action:
                                              self.commit_slider(ids, a, sl.value()))
                slider.trackClicked.connect(lambda value, ids=tuple(ids), a=action:
                                            self.commit_slider(ids, a, value))
                slider.wheelAdjusted.connect(lambda value, ids=tuple(ids), a=action:
                                             self.queue_slider_wheel(ids, a, value))
                line.addWidget(slider, stretch=1)
                line.addWidget(label)
                layout.addLayout(line)
                if room_hint is not None:
                    layout.addWidget(room_hint)
            else:
                line = QHBoxLayout()
                button = QPushButton("Live colour wheel…")
                button.setEnabled(supported)
                button.clicked.connect(lambda checked=False, ids=ids, ls=lights: self.choose_color(ids, ls))
                if not supported:
                    button.setToolTip("RGB colour is available only when every target supports it and is online")
                line.addWidget(button)
                if is_group:
                    line.addWidget(QLabel("Only your approved lights"))
                line.addStretch()
                layout.addLayout(line)

    def clear_tab_pages(self):
        # Removing a tab leaves its page owned by Qt's internal stacked widget.
        while self.tabs.count():
            index = self.tabs.count() - 1
            page = self.tabs.widget(index)
            self.tabs.removeTab(index)
            page.deleteLater()

    def render_tabs(self):
        old_index = self.tabs.currentIndex()
        eligible = approved_visible(self.lights, self.config["approved"])
        self.clear_tab_pages()
        if not eligible:
            label = QLabel("No approved bulbs are currently available.\n\n"
                           "Choose devices to select the exact devices you own.")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setWordWrap(True)
            self.tabs.addTab(label, "My lights")
        rooms = defaultdict(list)
        for light in eligible:
            rooms[(light.room_id, light.room_name)].append(light)
        for (_, room_name), lights in sorted(rooms.items(), key=lambda pair: pair[0][1].casefold()):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            content = QWidget()
            column = QVBoxLayout(content)
            column.setSpacing(12)
            section_font = content.font()
            section_font.setBold(True)
            room_title = QLabel("Room controls")
            room_title.setObjectName("roomControlsHeading")
            room_title.setFont(section_font)
            column.addWidget(room_title)
            group = QGroupBox(f"{room_name} · {len(lights)} approved light(s)")
            group.setObjectName("approvedRoomControls")
            group.setToolTip("These controls affect only approved lights in this room.")
            group_layout = QVBoxLayout(group)
            self.add_controls(group_layout, lights, is_group=True)
            column.addWidget(group)
            divider = QFrame()
            divider.setObjectName("roomDeviceDivider")
            divider.setFrameShape(QFrame.Shape.HLine)
            divider.setFrameShadow(QFrame.Shadow.Sunken)
            column.addWidget(divider)
            devices_title = QLabel("Individual lights")
            devices_title.setObjectName("individualLightsHeading")
            devices_title.setFont(section_font)
            column.addWidget(devices_title)
            for light in lights:
                box = QGroupBox(light.name + ("  ·  offline" if not light.reachable else ""))
                box.setObjectName("individualLightControls")
                box.setToolTip("Controls only this approved light.")
                box_layout = QVBoxLayout(box)
                self.add_controls(box_layout, [light])
                column.addWidget(box)
            column.addStretch()
            scroll.setWidget(content)
            tab = self.tabs.addTab(scroll, room_name)
            self.tabs.setTabToolTip(tab, room_name)
        climate = approved_climate(self.sensors, self.config["approved_sensors"])
        if climate:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            content = QWidget()
            column = QVBoxLayout(content)
            note = QLabel("Live readings from your approved sensors. View-only: no sensor commands are sent.")
            note.setWordWrap(True)
            column.addWidget(note)
            # Matching relation_id groups multiple parts of one physical TIMMERFLOTTE.
            for sensor in climate:
                group = QGroupBox(f"{sensor.name}  ·  {sensor.room_name}")
                layout = QVBoxLayout(group)
                readings = []
                if sensor.temperature is not None:
                    readings.append(f"Temperature: {sensor.temperature:.1f} °C")
                if sensor.humidity is not None:
                    readings.append(f"Humidity: {sensor.humidity:g}% RH")
                if sensor.battery is not None:
                    readings.append(f"Battery: {sensor.battery}%")
                layout.addWidget(QLabel("   ·   ".join(readings) if readings else "No readings currently reported"))
                if not sensor.reachable:
                    layout.addWidget(QLabel("Sensor offline"))
                column.addWidget(group)
            column.addStretch()
            scroll.setWidget(content)
            self.tabs.addTab(scroll, "Climate")
        self.tabs.setCurrentIndex(min(max(0, old_index), self.tabs.count() - 1))

    def quit_application(self):
        self._really_quit = True
        self.close()

    def closeEvent(self, event):
        if not self._really_quit and self.tray.isVisible() and QSystemTrayIcon.isSystemTrayAvailable():
            self.hide()
            event.ignore()
            return
        self.refresh_timer.stop()
        self._tray_brightness_timer.stop()
        self._slider_wheel_timer.stop()
        self._slider_wheel_pending.clear()
        self.tray_wheel.stop()
        self.tray.hide()
        if self.worker:
            self.worker.stop()
            # The library uses 10-second HTTP timeouts; allow current calls to finish.
            self.worker.wait()
        event.accept()
        QApplication.instance().quit()


def main():
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(ASSETS / "lysnav-logo.svg")))
    if sys.argv[1:] == ["--check-runtime"]:
        # Exercise the frozen Qt platform and icon loading without opening a
        # window, installing the tray hook, or connecting to a hub.
        from dirigera.hub import auth

        for filename in ("lysnav-logo.svg", "tray-light.svg", "tray-dark.svg",
                         "tray-light-on.svg", "tray-dark-on.svg"):
            if QIcon(str(ASSETS / filename)).pixmap(32, 32).isNull():
                sys.exit(1)
        # Verify actual rendered notification icons, not just non-null SVGs.
        for mode, base in (("light", "#282828"), ("dark", "#ebdbb2")):
            for active in (False, True):
                name = f"tray-{mode}{'-on' if active else ''}.svg"
                icon = native_tray_icon(name)
                for size in (16, 32, 64):
                    image = icon.pixmap(size, size).toImage()
                    if image.pixelColor(size // 2, size * 25 // 64).name() != ("#fe8019" if active else base):
                        sys.exit(1)
        if b"ico" not in [bytes(fmt).lower() for fmt in QImageReader.supportedImageFormats()]:
            sys.exit(1)
        if not callable(auth.main):
            sys.exit(1)
        return

    app.setApplicationName("Lysnav")
    app.setApplicationDisplayName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    start_requested = "--tray" in sys.argv
    settings_dir = config_path().parent
    try:
        settings_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        QMessageBox.critical(None, APP_NAME, f"Could not open the settings directory: {error}")
        return

    # A Start menu launch should open the window of the existing tray process.
    server_name = "Lysnav-" + sha256(str(settings_dir).casefold().encode("utf-8")).hexdigest()[:24]
    instance = QLockFile(str(settings_dir / "instance.lock"))
    instance.setStaleLockTime(0)
    if not instance.tryLock(0):
        if instance.error() != QLockFile.LockError.LockFailedError:
            QMessageBox.critical(None, APP_NAME, "Could not lock the settings directory.")
        elif not start_requested:
            client = QLocalSocket()
            client.connectToServer(server_name)
            if not client.waitForConnected(1500):
                QMessageBox.information(None, APP_NAME, "Lysnav is already running. Open it from the system tray.")
        return

    QLocalServer.removeServer(server_name)
    server = QLocalServer(app)
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    listening = server.listen(server_name)
    window = MainWindow()

    def open_existing_window():
        while server.hasPendingConnections():
            client = server.nextPendingConnection()
            client.disconnectFromServer()
            client.deleteLater()
        window.show_and_raise()

    server.newConnection.connect(open_existing_window)
    start_in_tray = (
        start_requested
        and listening
        and bool(window.config["ip"] and window.config["token"])
        and QSystemTrayIcon.isSystemTrayAvailable()
    )
    if start_in_tray:
        window.winId()  # Create the hidden native window for system theme notifications.
    else:
        window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--token-helper":
        run_token_helper(sys.argv[2])
    else:
        main()
