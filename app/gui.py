"""Milestone 2: frameless, always-on-top desktop UI for Meeting Copilot.

The pipeline runs in a QThread; events are marshalled to the GUI thread with a
Qt signal.  Audio still never touches the disk.

Optional Arabic translation is opt-in: the first time it is enabled the user
is told that the English text (not audio) is sent to Google Translate.
"""

from __future__ import annotations

import argparse
import logging
import math
import queue
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (
    QEvent,
    QLockFile,
    QPoint,
    QRectF,
    QSettings,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPainter,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizeGrip,
    QSizePolicy,
    QSystemTrayIcon,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.config import Settings
from app.pipeline import Event, StreamPipeline
from app.translate import TranslationError, translate_ar

APP_NAME = "Meeting Copilot"
MINT = "#6EE7B7"
BLUE = "#8AB4FF"
AMBER = "#F5C26B"
RED = "#FF7A7A"
GREY = "#8B97A9"
TEXT = "#E9EEF7"
MUTED = "#8B97A9"

QS = QSettings("MeetingCopilot", "MeetingCopilot")

QSS = """
QWidget {
    color: #E9EEF7;
    font-family: "Segoe UI";
    font-size: 13px;
}
QFrame#card {
    background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 rgba(22, 27, 39, 230),
        stop: 1 rgba(9, 12, 19, 234));
    border: 1px solid rgba(255, 255, 255, 24);
    border-radius: 18px;
}
QLabel#appTitle {
    font-size: 14px;
    font-weight: 600;
    color: #F3F6FC;
}
QLabel#appSub {
    font-size: 9px;
    color: #7E8A9C;
}
QToolButton#hbtn {
    border: none;
    border-radius: 8px;
    background: transparent;
    color: #AAB5C6;
    font-size: 9px;
    font-weight: 600;
}
QToolButton#hbtn:hover {
    background: rgba(255, 255, 255, 16);
    color: #FFFFFF;
}
QToolButton#hbtn:checked {
    background: rgba(110, 231, 183, 24);
    color: #6EE7B7;
}
QToolButton#closeBtn {
    border: none;
    border-radius: 8px;
    background: transparent;
    color: #8B97A9;
    font-size: 13px;
}
QToolButton#closeBtn:hover {
    background: rgba(255, 90, 90, 40);
    color: #FFD3D3;
}
QToolButton#hideBtn {
    border: none;
    border-radius: 8px;
    background: transparent;
    color: #8B97A9;
    font-size: 15px;
}
QToolButton#hideBtn:hover {
    background: rgba(255, 255, 255, 16);
    color: #FFFFFF;
}
QLabel#time {
    color: #6F7B8D;
    font-size: 10px;
}
QLabel#latency {
    color: #6EE7B7;
    font-size: 9px;
}
QLabel#line {
    color: #E9EEF7;
    font-size: 14px;
}
QLabel#arabic {
    color: #A9B7CC;
    font-size: 13px;
}
QLabel#partial {
    color: #7E8A9C;
    font-size: 12px;
    font-style: italic;
}
QLabel#status {
    color: #AEB9C9;
    font-size: 10px;
}
QLabel#privacy {
    color: #6F7B8D;
    font-size: 9px;
}
QFrame#sep {
    background: rgba(255, 255, 255, 12);
    border: none;
}
QPushButton#ghost {
    background: rgba(255, 255, 255, 8);
    border: 1px solid rgba(255, 255, 255, 16);
    border-radius: 8px;
    padding: 4px 10px;
    color: #C7D0DE;
    font-size: 10px;
}
QPushButton#ghost:hover {
    background: rgba(255, 255, 255, 16);
    border-color: rgba(255, 255, 255, 28);
}
QPushButton#ghost:pressed {
    background: rgba(255, 255, 255, 24);
}
QPushButton#ghost:checked {
    background: rgba(110, 231, 183, 26);
    border-color: rgba(110, 231, 183, 90);
    color: #6EE7B7;
}
QFrame#error {
    background: rgba(255, 90, 90, 18);
    border: 1px solid rgba(255, 90, 90, 45);
    border-radius: 10px;
}
QLabel#errorText {
    color: #FFB4B4;
    font-size: 10px;
}
QScrollArea {
    background: transparent;
    border: none;
}
QScrollArea > QWidget > QWidget {
    background: transparent;
}
QScrollBar:vertical {
    background: transparent;
    width: 8px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: rgba(255, 255, 255, 36);
    border-radius: 4px;
    min-height: 24px;
}
QScrollBar::handle:vertical:hover {
    background: rgba(255, 255, 255, 60);
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: transparent;
}
"""


def make_app_icon(size: int = 64) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#151A24"))
    radius = size * 0.28
    painter.drawRoundedRect(0, 0, size, size, radius, radius)
    painter.setBrush(QColor(MINT))
    bar_w = max(2.0, size * 0.09)
    gap = bar_w * 0.9
    heights = (0.30, 0.56, 0.40)
    total = len(heights) * bar_w + (len(heights) - 1) * gap
    x = (size - total) / 2
    for height in heights:
        h = size * height
        painter.drawRoundedRect(
            QRectF(x, (size - h) / 2, bar_w, h), bar_w / 2, bar_w / 2
        )
        x += bar_w + gap
    painter.end()
    return pixmap


class WaveformWidget(QWidget):
    """Small animated waveform; it moves only while the app is live."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(96, 44)
        self._live = False
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(70)
        self._timer.timeout.connect(self._tick)

    def set_live(self, live: bool) -> None:
        if live == self._live:
            return
        self._live = live
        if live:
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def _tick(self) -> None:
        self._phase += 0.28
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        bars = 9
        bar_w = 4.0
        gap = 5.0
        total = bars * bar_w + (bars - 1) * gap
        x = (self.width() - total) / 2
        mid = self.height() / 2
        base = QColor(MINT) if self._live else QColor(GREY)
        for index in range(bars):
            if self._live:
                wave = 0.5 + 0.5 * math.sin(self._phase + index * 0.7)
                height = 5 + 24 * (wave**2)
                color = QColor(base)
                color.setAlpha(int(110 + 135 * wave))
                painter.setBrush(color)
            else:
                height = 6 + (5 if index in (3, 4, 5) else 0)
                painter.setBrush(base)
            painter.drawRoundedRect(
                QRectF(x + index * (bar_w + gap), mid - height / 2, bar_w, height),
                bar_w / 2,
                bar_w / 2,
            )


class StatusBadge(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("badge")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 3, 8, 3)
        layout.setSpacing(5)
        self.dot = QLabel("●")
        self.dot.setStyleSheet(f"color: {AMBER}; font-size: 7px;")
        self.label = QLabel("LOADING")
        self.label.setStyleSheet(
            f"color: {AMBER}; font-size: 9px; font-weight: 600;"
        )
        layout.addWidget(self.dot)
        layout.addWidget(self.label)
        self.set_state("LOADING", AMBER)

    def set_state(self, text: str, color: str) -> None:
        c = QColor(color)
        self.label.setText(text)
        self.label.setStyleSheet(
            f"color: {color}; font-size: 9px; font-weight: 600;"
        )
        self.dot.setStyleSheet(f"color: {color}; font-size: 7px;")
        self.setStyleSheet(
            "QFrame#badge {"
            f" background: rgba({c.red()}, {c.green()}, {c.blue()}, 22);"
            f" border: 1px solid rgba({c.red()}, {c.green()}, {c.blue()}, 60);"
            " border-radius: 9px; }"
        )


class TranscriptRow(QWidget):
    def __init__(
        self,
        card_id: int,
        when: datetime,
        text: str,
        latency: float,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.card_id = card_id
        self.english_text = text
        self._arabic_text = ""

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        left = QVBoxLayout()
        left.setContentsMargins(0, 2, 0, 0)
        left.setSpacing(3)
        time_label = QLabel(when.strftime("%H:%M"))
        time_label.setObjectName("time")
        time_label.setFixedWidth(50)
        time_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        left.addWidget(time_label)
        if latency > 0:
            latency_label = QLabel(f"{latency:.1f}s")
            latency_label.setObjectName("latency")
            latency_label.setFixedWidth(50)
            latency_label.setToolTip("Time from end of speech to text")
            left.addWidget(latency_label)
        left.addStretch(1)

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(5)
        self.text_label = QLabel(text)
        self.text_label.setObjectName("line")
        self.text_label.setWordWrap(True)
        self.text_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.text_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        right.addWidget(self.text_label)

        self.arabic_label = QLabel()
        self.arabic_label.setObjectName("arabic")
        self.arabic_label.setWordWrap(True)
        self.arabic_label.setAlignment(Qt.AlignRight)
        self.arabic_label.setLayoutDirection(Qt.RightToLeft)
        self.arabic_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.arabic_label.hide()
        right.addWidget(self.arabic_label)

        layout.addLayout(left)
        layout.addLayout(right, 1)

    def set_arabic(self, text: str, visible: bool = True) -> None:
        self._arabic_text = text
        self.arabic_label.setText(text)
        self.arabic_label.setVisible(bool(text) and visible)

    def set_arabic_pending(self) -> None:
        if self._arabic_text:
            return
        self.arabic_label.setText("…")
        self.arabic_label.show()

    def clear_arabic_pending(self) -> None:
        if self._arabic_text:
            return
        self.arabic_label.clear()
        self.arabic_label.hide()

    def show_arabic(self, visible: bool) -> None:
        self.arabic_label.setVisible(visible and bool(self._arabic_text))

    def has_arabic(self) -> bool:
        return bool(self._arabic_text)


class EmptyState(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)
        layout.addStretch(1)
        self.wave = WaveformWidget()
        layout.addWidget(self.wave, 0, Qt.AlignHCenter)
        title = QLabel("Listening for English audio")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 15px; font-weight: 600; color: #F3F6FC;")
        layout.addWidget(title)
        subtitle = QLabel(
            "Play a meeting, video, or voice call.\n"
            "Audio stays on this device; the microphone is off."
        )
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet("font-size: 11px; color: #8B97A9;")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)
        hint = QLabel("Drag to move · AR translates · ⋯ for options")
        hint.setAlignment(Qt.AlignCenter)
        hint.setStyleSheet("font-size: 9px; color: #5F6B7C;")
        layout.addWidget(hint)
        layout.addStretch(1)


class TranslatorThread(QThread):
    translated = Signal(int, str)
    failed = Signal(int, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._queue: queue.Queue[tuple[int, str] | None] = queue.Queue(maxsize=40)
        self._stopping = False

    def enqueue(self, card_id: int, text: str) -> bool:
        if self._stopping:
            return False
        try:
            self._queue.put_nowait((card_id, text))
            return True
        except queue.Full:
            return False

    def stop(self) -> None:
        self._stopping = True
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if not self.wait(3000):
            self.terminate()
            self.wait(500)

    def run(self) -> None:
        while not self._stopping:
            item = self._queue.get()
            if item is None:
                break
            card_id, text = item
            try:
                self.translated.emit(card_id, translate_ar(text, timeout=5.0))
            except TranslationError as exc:
                self.failed.emit(card_id, str(exc))
            except Exception as exc:  # noqa: BLE001 - never kill the worker
                self.failed.emit(card_id, str(exc))


class PipelineWorker(QThread):
    event = Signal(object)

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.pipeline: StreamPipeline | None = None

    def run(self) -> None:
        try:
            self.pipeline = StreamPipeline(self.settings, self.event.emit)
            self.pipeline.run()
        except Exception as exc:  # noqa: BLE001 - show any crash in the UI
            logging.exception("Pipeline worker crashed")
            self.event.emit(Event("error", text=f"Pipeline stopped: {exc}"))

    def request_stop(self) -> None:
        if self.pipeline is not None:
            self.pipeline.request_stop()


class MainWindow(QWidget):
    def __init__(self, settings: Settings, start: bool = False) -> None:
        super().__init__(None)
        self.settings = settings
        self._worker: PipelineWorker | None = None
        self._translator: TranslatorThread | None = None
        self._transcript_file = None
        self._transcript_path: Path | None = None
        self._rows: dict[int, TranscriptRow] = {}
        self._row_seq = 0
        self._last_text = ""
        self._drag_offset: QPoint | None = None
        self._closing = False
        self._arabic_failures = 0
        self._listening = False
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(self._restore_status)

        self._pinned = QS.value("pinned", True, type=bool)
        self._arabic = QS.value("arabic", False, type=bool)
        self._consent = QS.value("translate_consent", False, type=bool)
        if self._arabic and not self._consent:
            self._arabic = False

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(make_app_icon(64)))
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumSize(420, 480)
        self.resize(480, 680)
        flags = Qt.Window | Qt.FramelessWindowHint
        if self._pinned:
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)

        self._build_ui()
        self._restore_geometry()
        self._setup_tray()
        self._setup_shortcuts()
        self._update_privacy_text()
        if start:
            QTimer.singleShot(200, self.start_pipeline)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        self.setStyleSheet(QSS)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.setSpacing(0)

        self.card = QFrame()
        self.card.setObjectName("card")
        self.card.setContextMenuPolicy(Qt.CustomContextMenu)
        self.card.customContextMenuRequested.connect(self._show_context_menu)
        shadow = QGraphicsDropShadowEffect(self.card)
        shadow.setBlurRadius(44)
        shadow.setOffset(0, 12)
        shadow.setColor(QColor(0, 0, 0, 170))
        self.card.setGraphicsEffect(shadow)
        outer.addWidget(self.card)

        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)

        # --- header
        header = QFrame()
        header.setObjectName("header")
        header.setFixedHeight(66)
        self.header = header
        header.installEventFilter(self)
        header.setCursor(Qt.OpenHandCursor)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(18, 12, 12, 10)
        header_layout.setSpacing(10)

        icon = QLabel()
        icon.setPixmap(make_app_icon(34))
        icon.setFixedSize(34, 34)
        header_layout.addWidget(icon)

        titles = QVBoxLayout()
        titles.setContentsMargins(0, 0, 0, 0)
        titles.setSpacing(1)
        title = QLabel(APP_NAME)
        title.setObjectName("appTitle")
        subtitle = QLabel("SYSTEM AUDIO · LOCAL")
        subtitle.setObjectName("appSub")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        header_layout.addLayout(titles)
        header_layout.addStretch(1)

        self.badge = StatusBadge()
        header_layout.addWidget(self.badge, 0, Qt.AlignVCenter)

        self.menu_btn = QToolButton()
        self.menu_btn.setObjectName("hbtn")
        self.menu_btn.setText("⋯")
        self.menu_btn.setFixedSize(28, 26)
        self.menu_btn.setCursor(Qt.PointingHandCursor)
        self.menu_btn.setToolTip("Options")
        self.menu_btn.clicked.connect(self._show_header_menu)
        header_layout.addWidget(self.menu_btn)

        hide_btn = QToolButton()
        hide_btn.setObjectName("hideBtn")
        hide_btn.setText("–")
        hide_btn.setFixedSize(28, 26)
        hide_btn.setCursor(Qt.PointingHandCursor)
        hide_btn.setToolTip("Hide to tray (still listening)")
        hide_btn.clicked.connect(self._hide_to_tray)
        header_layout.addWidget(hide_btn)

        close_btn = QToolButton()
        close_btn.setObjectName("closeBtn")
        close_btn.setText("×")
        close_btn.setFixedSize(28, 26)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setToolTip("Quit Meeting Copilot")
        close_btn.clicked.connect(self.close)
        header_layout.addWidget(close_btn)

        card_layout.addWidget(header)
        self.options_menu = self._build_options_menu()
        self._refresh_options_menu()

        # --- content
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(18, 4, 14, 4)
        content_layout.setSpacing(8)

        self.error_frame = QFrame()
        self.error_frame.setObjectName("error")
        error_layout = QHBoxLayout(self.error_frame)
        error_layout.setContentsMargins(10, 8, 8, 8)
        error_layout.setSpacing(8)
        self.error_label = QLabel()
        self.error_label.setObjectName("errorText")
        self.error_label.setWordWrap(True)
        self.error_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        error_layout.addWidget(self.error_label, 1)
        retry_btn = QPushButton("Retry")
        retry_btn.setObjectName("ghost")
        retry_btn.setCursor(Qt.PointingHandCursor)
        retry_btn.clicked.connect(self._retry)
        error_layout.addWidget(retry_btn, 0, Qt.AlignTop)
        self.error_frame.hide()
        content_layout.addWidget(self.error_frame)

        self.empty = EmptyState()
        content_layout.addWidget(self.empty, 1)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.viewport().setAutoFillBackground(False)
        self.list_host = QWidget()
        self.list_host.setStyleSheet("background: transparent;")
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(0, 4, 4, 4)
        self.list_layout.setSpacing(16)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.list_host)
        self.scroll.hide()
        content_layout.addWidget(self.scroll, 1)

        self.partial_label = QLabel()
        self.partial_label.setObjectName("partial")
        self.partial_label.setWordWrap(True)
        self.partial_label.hide()
        content_layout.addWidget(self.partial_label)

        card_layout.addWidget(content, 1)

        # --- footer
        separator = QFrame()
        separator.setObjectName("sep")
        separator.setFixedHeight(1)
        card_layout.addWidget(separator)

        footer = QFrame()
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(18, 9, 12, 10)
        footer_layout.setSpacing(8)

        footer_text = QVBoxLayout()
        footer_text.setContentsMargins(0, 0, 0, 0)
        footer_text.setSpacing(2)
        self.status_label = QLabel("Starting…")
        self.status_label.setObjectName("status")
        self.status_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.privacy_label = QLabel("Mic off · audio never recorded")
        self.privacy_label.setObjectName("privacy")
        footer_text.addWidget(self.status_label)
        footer_text.addWidget(self.privacy_label)
        footer_layout.addLayout(footer_text, 1)

        self.ar_btn = QPushButton("AR")
        self.ar_btn.setObjectName("ghost")
        self.ar_btn.setCheckable(True)
        self.ar_btn.setChecked(self._arabic)
        self.ar_btn.setCursor(Qt.PointingHandCursor)
        self.ar_btn.setToolTip(
            "Show Arabic translation under each line\nعرض الترجمة العربية تحت كل سطر"
        )
        self.ar_btn.toggled.connect(self._toggle_arabic)
        footer_layout.addWidget(self.ar_btn)

        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setObjectName("ghost")
        self.copy_btn.setCursor(Qt.PointingHandCursor)
        self.copy_btn.setToolTip("Copy the latest sentence")
        self.copy_btn.clicked.connect(self._copy_last)
        footer_layout.addWidget(self.copy_btn)

        clear_btn = QPushButton("Clear")
        clear_btn.setObjectName("ghost")
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.setToolTip("Clear the transcript")
        clear_btn.clicked.connect(self._clear)
        footer_layout.addWidget(clear_btn)

        grip = QSizeGrip(footer)
        grip.setFixedSize(14, 14)
        footer_layout.addWidget(grip, 0, Qt.AlignBottom | Qt.AlignRight)

        card_layout.addWidget(footer)

    # ------------------------------------------------------------- pipeline
    def start_pipeline(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        self.error_frame.hide()
        self._set_state("LOADING", AMBER)
        self.status_label.setText("Loading local model…")
        if self.settings.save_transcript and self._transcript_file is None:
            self._open_transcript()
        self._worker = PipelineWorker(self.settings, self)
        self._worker.event.connect(self._on_event)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()

    def _stop_worker(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        if worker.isRunning():
            worker.request_stop()
            if not worker.wait(6000):
                logging.warning("Pipeline thread did not stop; terminating")
                worker.terminate()
                worker.wait(1000)

    def _on_event(self, event: Event) -> None:
        if self._closing:
            return
        try:
            if event.kind == "status":
                self._on_status(event.text)
            elif event.kind == "final":
                self._add_final(event)
            elif event.kind == "partial":
                self._on_partial(event)
            elif event.kind == "error":
                self._on_error(event.text)
        except Exception:  # noqa: BLE001 - GUI must not die on one event
            logging.exception("Failed to handle pipeline event")

    def _on_status(self, text: str) -> None:
        if text.startswith("Loading"):
            self._set_state("LOADING", AMBER)
            self.status_label.setText("Loading local model…")
        elif text.startswith("Model ready:"):
            model = text.split(":", 1)[1].split("(loaded")[0].strip()
            self._model_text = model
            self.status_label.setText("Ready · local model")
            self.status_label.setToolTip(f"Model ready: {model}")
        elif text.startswith("Capturing loopback:"):
            self.status_label.setToolTip(text)
        elif text.startswith("Using local model:"):
            self.status_label.setToolTip(text)
        elif text.startswith("Listening for speech"):
            self._listening = True
            self._set_state("LIVE", MINT)
            self.status_label.setText("Listening · local")
            self.status_label.setToolTip(text)
        else:
            self.status_label.setText(text[:80])
            self.status_label.setToolTip(text)

    def _restore_status(self) -> None:
        if self._listening:
            self.status_label.setText("Listening · local")
        elif self._last_text:
            self.status_label.setText("Paused")
        else:
            self.status_label.setText("Starting…")

    def _add_final(self, event: Event) -> None:
        self._row_seq += 1
        card_id = self._row_seq
        row = TranscriptRow(
            card_id,
            event.wall or datetime.now(),
            event.text,
            event.latency_s,
        )
        self._rows[card_id] = row
        self.list_layout.insertWidget(self.list_layout.count() - 1, row)
        while len(self._rows) > 40:
            oldest_id = next(iter(self._rows))
            oldest = self._rows.pop(oldest_id)
            self.list_layout.removeWidget(oldest)
            oldest.deleteLater()

        self._last_text = event.text
        if self._transcript_file is not None:
            stamp = (event.wall or datetime.now()).strftime("%H:%M:%S")
            try:
                self._transcript_file.write(f"[{stamp}] {event.text}\n")
                self._transcript_file.flush()
            except OSError:
                logging.exception("Could not write transcript")
                self._transcript_file = None
                self._update_privacy_text()
        self.partial_label.hide()
        if self._arabic:
            self._ensure_translator()
            if self._translator is not None:
                if self._translator.enqueue(card_id, event.text):
                    row.set_arabic_pending()

        self.empty.hide()
        self.scroll.show()
        QTimer.singleShot(0, self._scroll_to_bottom)

    def _on_partial(self, event: Event) -> None:
        if event.text:
            self.partial_label.setText(event.text)
            self.partial_label.show()
        else:
            self.partial_label.hide()

    def _on_error(self, text: str) -> None:
        self._listening = False
        self._set_state("ERROR", RED)
        self.error_label.setText(text)
        self.error_frame.show()
        self.status_label.setText("Stopped — see the error above")
        self.status_label.setToolTip(text)
        logging.error("Pipeline error: %s", text)

    def _on_worker_finished(self) -> None:
        if self._closing:
            return
        if self.badge.label.text() != "ERROR":
            self._listening = False
            self._set_state("PAUSED", GREY)
            self.status_label.setText("Stopped")
        worker = self.sender()
        if isinstance(worker, QThread):
            worker.deleteLater()

    def _retry(self) -> None:
        self.error_frame.hide()
        self._stop_worker()
        QTimer.singleShot(150, self.start_pipeline)

    def _scroll_to_bottom(self) -> None:
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ------------------------------------------------------------ translation
    def _ensure_translator(self) -> None:
        if self._translator is not None:
            return
        self._translator = TranslatorThread(self)
        self._translator.translated.connect(self._on_translated)
        self._translator.failed.connect(self._on_translation_failed)
        self._translator.start()

    def _toggle_arabic(self, checked: bool) -> None:
        if checked and not self._consent:
            answer = QMessageBox.question(
                self,
                "Arabic translation",
                "Enable Arabic translation?\n\n"
                "The English text of each sentence will be sent to Google "
                "Translate (translate.googleapis.com) to get an Arabic line.\n\n"
                "Audio never leaves your device. You can turn this off at any time.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                self._sync_arabic_controls(False)
                return
            self._consent = True
            QS.setValue("translate_consent", True)

        self._arabic = checked
        QS.setValue("arabic", checked)
        self._arabic_failures = 0
        self._update_privacy_text()
        self._sync_arabic_controls(checked)
        if checked:
            self._ensure_translator()
            recent = list(self._rows.values())[-8:]
            if self._translator is not None:
                for row in recent:
                    if not row.has_arabic():
                        if self._translator.enqueue(row.card_id, row.english_text):
                            row.set_arabic_pending()
        for row in self._rows.values():
            row.show_arabic(checked)

    def _sync_arabic_controls(self, checked: bool) -> None:
        for control in (
            getattr(self, "ar_btn", None),
            getattr(self, "ar_action", None),
            getattr(self, "ar_menu_action", None),
        ):
            if control is None or control.isChecked() == checked:
                continue
            control.blockSignals(True)
            control.setChecked(checked)
            control.blockSignals(False)

    def _on_translated(self, card_id: int, text: str) -> None:
        row = self._rows.get(card_id)
        if row is None:
            return
        row.set_arabic(text, self._arabic)
        logging.info("Arabic translation ready (%s, %d chars)", card_id, len(text))

    def _on_translation_failed(self, card_id: int, message: str) -> None:
        self._arabic_failures += 1
        row = self._rows.get(card_id)
        if row is not None:
            row.clear_arabic_pending()
        logging.warning("Arabic translation failed (%s): %s", card_id, message)
        if self._arabic_failures == 1:
            self._flash_status("Arabic translation unavailable")
            self.status_label.setToolTip(f"Translation failed: {message}")

    def _update_privacy_text(self) -> None:
        logging_on = self._transcript_file is not None
        if self._arabic:
            text = "AR: Google text · logging on" if logging_on else "AR: Google text · mic off"
        else:
            text = "Mic off · logging locally" if logging_on else "Mic off · audio local"
        self.privacy_label.setText(text)
        if logging_on and self._transcript_path is not None:
            self.privacy_label.setToolTip(f"Transcript: {self._transcript_path}")
        else:
            self.privacy_label.setToolTip("")

    def _open_transcript(self) -> None:
        log_dir = Path(__file__).resolve().parents[1] / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"transcript-{datetime.now():%Y%m%d-%H%M%S}.txt"
        try:
            self._transcript_file = path.open("w", encoding="utf-8")
        except OSError:
            logging.exception("Could not open transcript file")
            self._flash_status("Transcript logging unavailable")
            return
        self._transcript_path = path
        logging.info("Transcript logging ON: %s", path)
        self._update_privacy_text()

    # -------------------------------------------------------------- actions
    def _copy_last(self) -> None:
        if not self._last_text:
            return
        QApplication.clipboard().setText(self._last_text)
        self.copy_btn.setText("Copied")
        QTimer.singleShot(1200, lambda: self.copy_btn.setText("Copy"))

    def _clear(self) -> None:
        for card_id, row in list(self._rows.items()):
            self._rows.pop(card_id, None)
            self.list_layout.removeWidget(row)
            row.deleteLater()
        self._last_text = ""
        self.partial_label.hide()
        self.scroll.hide()
        self.empty.show()

    def _flash_status(self, text: str) -> None:
        self.status_label.setText(text)
        self._status_timer.start(2500)

    def _set_state(self, text: str, color: str) -> None:
        self.badge.set_state(text, color)
        self.empty.wave.set_live(text == "LIVE")

    def _apply_pin(self, pinned: bool) -> None:
        changed = pinned != self._pinned
        self._pinned = pinned
        if changed:
            was_visible = self.isVisible()
            geometry = self.geometry()
            self.setWindowFlag(Qt.WindowStaysOnTopHint, pinned)
            self.setGeometry(geometry)
            if was_visible:
                self.show()
                self.raise_()
        QS.setValue("pinned", pinned)
        for control in (
            getattr(self, "top_btn", None),
            getattr(self, "pin_action", None),
            getattr(self, "pin_menu_action", None),
        ):
            if control is None or control.isChecked() == pinned:
                continue
            control.blockSignals(True)
            control.setChecked(pinned)
            control.blockSignals(False)

    def _set_opacity(self, value: float) -> None:
        self.setWindowOpacity(value)
        QS.setValue("opacity", value)

    def _dock_right(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        max_height = max(480, area.height() - 40)
        if self.height() > max_height:
            self.resize(self.width(), max_height)
        width = self.width()
        height = self.height()
        x = max(area.left() + 20, area.right() - width - 20)
        y = area.top() + max(20, (area.height() - height) // 2)
        self.move(x, y)

    def _build_options_menu(self) -> QMenu:
        menu = QMenu(self)
        dock = menu.addAction("Dock to right edge")
        dock.triggered.connect(self._dock_right)
        reset = menu.addAction("Reset size")
        reset.triggered.connect(lambda: self.resize(480, 680))
        menu.addSeparator()

        opacity_menu = menu.addMenu("Opacity")
        self._opacity_actions: dict[int, QAction] = {}
        for percent in (100, 95, 90, 80, 70):
            action = opacity_menu.addAction(f"{percent}%")
            action.setCheckable(True)
            action.triggered.connect(
                lambda checked=False, p=percent: self._set_opacity(p / 100)
            )
            self._opacity_actions[percent] = action
        menu.addSeparator()

        self.pin_menu_action = menu.addAction("Always on top")
        self.pin_menu_action.setCheckable(True)
        self.pin_menu_action.triggered.connect(self._apply_pin)

        self.ar_menu_action = menu.addAction("Arabic translation")
        self.ar_menu_action.setCheckable(True)
        self.ar_menu_action.triggered.connect(self._toggle_arabic)

        menu.addSeparator()
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(self.close)
        menu.aboutToShow.connect(self._refresh_options_menu)
        return menu

    def _refresh_options_menu(self) -> None:
        self.pin_menu_action.setChecked(self._pinned)
        self.ar_menu_action.setChecked(self._arabic)
        current = round(self.windowOpacity() * 100)
        for percent, action in self._opacity_actions.items():
            action.setChecked(abs(current - percent) <= 1)

    def _show_header_menu(self) -> None:
        self._refresh_options_menu()
        self.options_menu.popup(
            self.menu_btn.mapToGlobal(QPoint(0, self.menu_btn.height() + 4))
        )

    def _show_context_menu(self, position) -> None:
        self._refresh_options_menu()
        self.options_menu.exec(self.card.mapToGlobal(position))

    def _setup_shortcuts(self) -> None:
        hide = QShortcut(QKeySequence("Ctrl+H"), self)
        hide.activated.connect(self._toggle_visible)
        copy = QShortcut(QKeySequence("Ctrl+Shift+C"), self)
        copy.activated.connect(self._copy_last)
        quit_shortcut = QShortcut(QKeySequence("Ctrl+Q"), self)
        quit_shortcut.activated.connect(self.close)

    def _setup_tray(self) -> None:
        self.tray = QSystemTrayIcon(QIcon(make_app_icon(64)), self)
        self.tray.setToolTip("Meeting Copilot · listening")
        menu = QMenu()
        show_action = menu.addAction("Show / hide")
        show_action.triggered.connect(self._toggle_visible)
        self.pin_action = menu.addAction("Always on top")
        self.pin_action.setCheckable(True)
        self.pin_action.setChecked(self._pinned)
        self.pin_action.triggered.connect(self._apply_pin)
        self.ar_action = menu.addAction("Arabic translation")
        self.ar_action.setCheckable(True)
        self.ar_action.setChecked(self._arabic)
        self.ar_action.triggered.connect(self._toggle_arabic)
        menu.addSeparator()
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(self.close)
        self.tray.setContextMenu(menu)
        self.tray_menu = menu
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # A single click should reveal the window, never hide it.  Hiding is
        # explicit (Ctrl+H or the tray "Show / hide" item).
        if reason in (
            QSystemTrayIcon.DoubleClick,
            QSystemTrayIcon.Trigger,
        ):
            self._show_window()

    def _show_window(self) -> None:
        if not self.isVisible():
            self.show()
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()
        self.tray.setToolTip("Meeting Copilot · listening")

    def _hide_to_tray(self) -> None:
        self.hide()
        self.tray.setToolTip("Meeting Copilot · hidden (still listening)")

    def _toggle_visible(self) -> None:
        if self.isVisible():
            self._hide_to_tray()
        else:
            self._show_window()

    # -------------------------------------------------------------- window
    def _restore_geometry(self) -> None:
        saved = QS.value("geometry")
        if saved is not None and self.restoreGeometry(saved):
            center = self.frameGeometry().center()
            screens = QGuiApplication.screens()
            if any(screen.availableGeometry().contains(center) for screen in screens):
                self._restore_opacity()
                return
        self._dock_right()
        self._restore_opacity()

    def _restore_opacity(self) -> None:
        value = QS.value("opacity", 1.0, type=float)
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = 1.0
        self.setWindowOpacity(min(1.0, max(0.55, value)))

    def _save_geometry(self) -> None:
        QS.setValue("geometry", self.saveGeometry())
        QS.setValue("opacity", self.windowOpacity())

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt naming
        header = getattr(self, "header", None)
        if header is not None and obj is header:
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._drag_offset = (
                    event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                )
            elif event.type() == QEvent.MouseMove and self._drag_offset is not None:
                if event.buttons() & Qt.LeftButton:
                    self.move(event.globalPosition().toPoint() - self._drag_offset)
                    return True
            elif event.type() == QEvent.MouseButtonRelease:
                self._drag_offset = None
        return super().eventFilter(obj, event)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._closing = True
        self._save_geometry()
        self.hide()
        self._stop_worker()
        if self._translator is not None:
            self._translator.stop()
            self._translator = None
        if self._transcript_file is not None:
            try:
                self._transcript_file.close()
            except OSError:
                pass
            logging.info("Transcript saved to %s", self._transcript_path)
            self._transcript_file = None
        self.tray.hide()
        event.accept()
        QApplication.quit()


def _configure_logging() -> None:
    log_dir = Path(__file__).resolve().parents[1] / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=log_dir / "gui.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )


def _install_excepthook() -> None:
    def hook(exc_type, exc, tb) -> None:
        logging.exception("Unhandled GUI error", exc_info=(exc_type, exc, tb))
        try:
            QMessageBox.critical(None, APP_NAME, f"Unexpected error:\n{exc}")
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = hook


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.gui",
        description="Meeting Copilot desktop UI - local live transcription.",
    )
    parser.add_argument("--audio-device", default=None, help="part of the loopback device name")
    parser.add_argument("--model", default="auto", help="Whisper model (default: auto)")
    parser.add_argument(
        "--compute-device",
        choices=("auto", "cuda", "cpu"),
        default="auto",
        help="where to run Whisper (default: auto)",
    )
    parser.add_argument("--compute-type", default=None, help="override compute type")
    parser.add_argument("--beam-size", type=int, default=1)
    parser.add_argument("--end-silence-ms", type=int, default=400)
    parser.add_argument("--max-utterance", type=float, default=12.0)
    parser.add_argument("--partials", action="store_true", help="show live partial text")
    parser.add_argument(
        "--save-transcript",
        action="store_true",
        help="write finalized text to logs/ (off by default)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    _configure_logging()
    settings = Settings(
        audio_device=args.audio_device,
        model=args.model,
        compute_device=args.compute_device,
        compute_type=args.compute_type,
        beam_size=args.beam_size,
        end_silence_ms=args.end_silence_ms,
        max_utterance_s=args.max_utterance,
        partials=args.partials,
        save_transcript=args.save_transcript,
    )

    app = QApplication([sys.argv[0]])
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName("MeetingCopilot")
    app.setWindowIcon(QIcon(make_app_icon(64)))
    app.setFont(QFont("Segoe UI", 10))
    app.setQuitOnLastWindowClosed(True)
    _install_excepthook()

    # One live instance is enough: two windows would both capture the same
    # loopback and compete for the GPU.
    lock_path = Path(tempfile.gettempdir()) / "meeting-copilot.lock"
    lock = QLockFile(str(lock_path))
    lock.setStaleLockTime(30_000)
    if not lock.tryLock(100):
        QMessageBox.information(
            None,
            APP_NAME,
            "Meeting Copilot is already running.\n"
            "Use its tray icon if the window is hidden.",
        )
        return 0

    window = MainWindow(settings)
    window._instance_lock = lock
    window.show()
    QTimer.singleShot(200, window.start_pipeline)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
