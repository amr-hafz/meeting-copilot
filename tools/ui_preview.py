"""Render the desktop UI with fake transcript lines to a PNG.

    .\\.venv\\Scripts\\python.exe tools\\ui_preview.py

The app is not started (no audio capture), and the image is only for design
review.  Safe to delete.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from app.config import Settings
from app.gui import MainWindow
from app.pipeline import Event


def main() -> int:
    app = QApplication([sys.argv[0]])
    app.setFont(QFont("Segoe UI", 10))

    window = MainWindow(Settings(), start=False)
    window.resize(480, 680)
    window.show()

    window._on_event(
        Event(
            "status",
            text="Model ready: faster-whisper-small.en · cuda/float16 (loaded in 4.1s)",
        )
    )
    window._on_event(Event("status", text="Listening for speech..."))

    now = datetime.now()
    lines = [
        (
            "Hi everyone, thanks for joining on such short notice.",
            "أهلاً بالجميع، شكراً لانضمامكم في وقت قصير.",
            1.6,
        ),
        (
            "The client asked us to move the delivery date to the 15th of next month.",
            "طلب العميل منا تأجيل موعد التسليم إلى الخامس عشر من الشهر المقبل.",
            1.7,
        ),
        (
            "Can you prepare a short summary of the risks before Thursday?",
            "هل يمكنك إعداد ملخص قصير للمخاطر قبل يوم الخميس؟",
            1.6,
        ),
        (
            "Please include the budget estimate in the same document.",
            "يرجى تضمين تقدير الميزانية في نفس المستند.",
            1.5,
        ),
    ]
    for index, (text, arabic, latency) in enumerate(lines):
        window._on_event(
            Event(
                "final",
                text=text,
                latency_s=latency,
                inference_s=latency - 0.4,
                audio_s=3.6,
                wall=now + timedelta(seconds=index * 6),
            )
        )
        row = list(window._rows.values())[-1]
        row.set_arabic(arabic)

    window._consent = True
    window._toggle_arabic(True)

    def snapshot() -> None:
        pixmap = window.grab()
        canvas = QPixmap(760, 860)
        painter = QPainter(canvas)
        gradient = QLinearGradient(0, 0, 760, 860)
        gradient.setColorAt(0.0, QColor("#202a3a"))
        gradient.setColorAt(1.0, QColor("#0a0e15"))
        painter.fillRect(canvas.rect(), gradient)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(35, 44, 60, 210))
        painter.drawRoundedRect(30, 30, 700, 800, 14, 14)
        painter.drawPixmap(150, 100, pixmap)
        painter.end()
        out = PROJECT_ROOT / "ui_preview.png"
        canvas.save(str(out))
        print(out)
        app.quit()

    QTimer.singleShot(900, snapshot)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
