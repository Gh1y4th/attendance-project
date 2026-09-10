import sys
import time
import os
from datetime import datetime

import cv2  # used directly for IP cameras (RTSP/HTTP streams)

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton,
    QVBoxLayout, QHBoxLayout, QGridLayout, QFrame, QMessageBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QFileDialog, QComboBox, QDialog, QDialogButtonBox, QLineEdit
)

from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.units import cm

# Import the existing AI engine without changing it.
import attendance_camera as engine


def open_capture(source):
    """Open a camera capture from a local index (int) or an IP camera URL (str).

    Local cameras go through the engine's own helper first (it may set
    resolution / backend options). IP cameras (RTSP/HTTP) are opened
    directly with OpenCV because the engine helper only knows local indices.
    """
    if isinstance(source, str):
        # IP camera / NVR stream: rtsp://..., http://..., https://...
        cap = cv2.VideoCapture(source)
    else:
        try:
            cap = engine.open_camera(source)
        except TypeError:
            # Older engine versions take no argument at all.
            cap = cv2.VideoCapture(source)

    if cap is None or not cap.isOpened():
        raise RuntimeError(
            f"Could not open camera source: {source}\n"
            "Check that the camera is connected / the URL is reachable "
            "on this Wi-Fi network."
        )

    return cap


class IpCameraDialog(QDialog):
    """Dialog to enter an IP camera / NVR stream URL."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add IP Camera")
        self.setFixedSize(460, 220)
        self.setStyleSheet("""
            QDialog {
                background: #f7f4ec;
            }
            QLabel {
                color: #29334a;
                font-size: 13px;
                font-family: "Segoe UI";
            }
            QLineEdit {
                background: #ffffff;
                color: #29334a;
                border: 1px solid #cbd2dc;
                border-radius: 8px;
                padding: 8px;
                font-size: 13px;
                font-family: "Segoe UI";
            }
            QLineEdit:focus {
                border: 1px solid #d1b37a;
            }
            QPushButton {
                background: #d1b37a;
                color: #24324f;
                border-radius: 8px;
                padding: 8px 16px;
                font-weight: 700;
                font-family: "Segoe UI";
            }
            QPushButton:hover {
                background: #ddc392;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        title = QLabel("Camera stream URL (RTSP / HTTP):")
        layout.addWidget(title)

        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText(
            "rtsp://admin:password@192.168.1.64:554/Streaming/Channels/101"
        )
        layout.addWidget(self.url_edit)

        hint = QLabel(
            "Examples:\n"
            "  Hikvision : rtsp://admin:pass@IP:554/Streaming/Channels/101\n"
            "  Dahua     : rtsp://admin:pass@IP:554/cam/realmonitor?channel=1&subtype=0\n"
            "  Generic   : http://IP:8080/video"
        )
        hint.setStyleSheet("color: #7d8797; font-size: 11px;")
        layout.addWidget(hint)

        layout.addStretch()

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.button(QDialogButtonBox.Ok).setText("Connect")
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

        for btn in btn_box.buttons():
            if btn_box.buttonRole(btn) == QDialogButtonBox.RejectRole:
                btn.setStyleSheet("""
                    QPushButton {
                        background: #24324f;
                        color: #f7f4ec;
                        border-radius: 8px;
                        padding: 8px 16px;
                        font-weight: 700;
                        font-family: "Segoe UI";
                    }
                    QPushButton:hover {
                        background: #324362;
                    }
                """)

    def url(self):
        return self.url_edit.text().strip()


class CameraWorker(QThread):
    attendance_added = Signal(str, str, bytes, int, int)
    stats_ready = Signal(dict)
    error = Signal(str)

    def __init__(self, camera_source=0):
        super().__init__()
        self.camera_source = camera_source
        self.running = True

    def stop(self):
        self.running = False

    def run(self):
        try:
            engine.SECURITY_LOGS_DIR.mkdir(parents=True, exist_ok=True)

            detector_app = engine.init_detector()
            recognition_model = engine.init_recognition_model()
            known_faces = engine.load_known_faces(detector_app, recognition_model)

            if not known_faces:
                self.error.emit("No valid known faces were found in known_faces/.")
                return

            tracker = engine.FaceTracker()
            liveness_detector = engine.LivenessDetector()
            cap = open_capture(self.camera_source)

            fps = 0.0
            previous_time = time.perf_counter()
            frame_counter = 0
            recognized_people = {}
            flagged_track_ids = set()

            try:
                while self.running:
                    ret, frame = cap.read()
                    if not ret:
                        self.error.emit("Failed to read frame from camera.")
                        break

                    frame_counter += 1

                    faces = detector_app.get(frame)
                    face_to_track = tracker.match(faces, frame.shape)

                    if len(faces) == 0:
                        tracker.clear()
                    elif frame_counter % engine.RECOGNITION_INTERVAL == 0:
                        engine.run_recognition(
                            faces, face_to_track, tracker, frame,
                            recognition_model, known_faces,
                            liveness_detector, frame_counter
                        )

                        for track_id in face_to_track.values():
                            track = tracker.get(track_id)
                            if track is None:
                                continue

                            name = track["name"]

                            if name in ("SPOOF", "UNKNOWN"):
                                flagged_track_ids.add(track_id)

                            if (
                                track["is_live"]
                                and name not in ("UNKNOWN", "SPOOF", "VERIFYING")
                                and name not in recognized_people
                            ):
                                recognized_people[name] = {
                                    "first_seen": time.strftime(
                                        "%Y-%m-%dT%H:%M:%S",
                                        time.localtime()
                                    )
                                }

                                # Crop the face out of the current frame so
                                # the UI can show a thumbnail next to the name.
                                bbox = track["bbox"]
                                fx1 = float(bbox[0])
                                fy1 = float(bbox[1])
                                fx2 = float(bbox[2])
                                fy2 = float(bbox[3])

                                box_w = fx2 - fx1
                                box_h = fy2 - fy1
                                cx = (fx1 + fx2) / 2.0
                                cy = (fy1 + fy2) / 2.0

                                # Pad outward from the raw detector bbox so the
                                # full face (forehead to chin) and a bit of
                                # background fit in the crop, instead of just
                                # the middle of the face.
                                half_side = max(box_w, box_h) * 1.3 / 2.0

                                x1 = max(0, int(cx - half_side))
                                y1 = max(0, int(cy - half_side))
                                x2 = min(frame.shape[1] - 1, int(cx + half_side))
                                y2 = min(frame.shape[0] - 1, int(cy + half_side))

                                photo_bytes = b""
                                photo_w = 0
                                photo_h = 0

                                if x2 > x1 and y2 > y1:
                                    face_crop = frame[y1:y2, x1:x2, ::-1]
                                    photo_h, photo_w = face_crop.shape[:2]
                                    photo_bytes = face_crop.tobytes()

                                # Push the new attendee straight to the log table.
                                self.attendance_added.emit(
                                    name,
                                    time.strftime("%H:%M:%S", time.localtime()),
                                    photo_bytes,
                                    photo_w,
                                    photo_h,
                                )

                                # Send the recognized name directly to the
                                # backend. The camera attendance API accepts
                                # { name, confidence_score }; requiring a
                                # student_id here caused successful local
                                # recognition to be silently skipped whenever
                                # the name was not in the students collection.
                                engine.send_attendance_to_backend(
                                    name,
                                    track.get("similarity")
                                )

                    current_time = time.perf_counter()
                    elapsed = current_time - previous_time
                    previous_time = current_time

                    if elapsed > 0:
                        instant_fps = 1.0 / elapsed
                        fps = (
                            instant_fps
                            if fps == 0.0
                            else fps * 0.9 + instant_fps * 0.1
                        )

                    # Total faces flagged as spoofed or unrecognized so far
                    # this session — stays even after the face leaves frame.
                    fake_or_unknown = len(flagged_track_ids)

                    stats = {
                        "fps": fps,
                        "faces": len(faces),
                        "known": len(known_faces),
                        "tracks": len(tracker.tracks),
                        "recognized": len(recognized_people),
                        "fake_or_unknown": fake_or_unknown,
                        "time": datetime.now().strftime("%H:%M:%S"),
                    }

                    self.stats_ready.emit(stats)

            finally:
                cap.release()

        except Exception as exc:
            self.error.emit(str(exc))


class ReportTypeDialog(QDialog):
    """Dialog to choose report format (PDF or Excel)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Generate Report")
        self.setFixedSize(320, 160)
        self.setStyleSheet("""
            QDialog {
                background: #f7f4ec;
            }
            QLabel {
                color: #29334a;
                font-size: 13px;
                font-family: "Segoe UI";
            }
            QComboBox {
                background: #ffffff;
                color: #29334a;
                border: 1px solid #cbd2dc;
                border-radius: 8px;
                padding: 8px;
                font-size: 13px;
                font-family: "Segoe UI";
            }
            QComboBox::drop-down {
                border: none;
            }
            QComboBox QAbstractItemView {
                background: #ffffff;
                color: #29334a;
                selection-background-color: #24324f;
            }
            QPushButton {
                background: #d1b37a;
                color: #24324f;
                border-radius: 8px;
                padding: 8px 16px;
                font-weight: 700;
                font-family: "Segoe UI";
            }
            QPushButton:hover {
                background: #ddc392;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        label = QLabel("Select report format:")
        layout.addWidget(label)

        self.format_combo = QComboBox()
        self.format_combo.addItems(["PDF Report (.pdf)", "Excel Report (.xlsx)"])
        layout.addWidget(self.format_combo)

        layout.addStretch()

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

        # Style the dialog buttons
        for btn in btn_box.buttons():
            if btn_box.buttonRole(btn) == QDialogButtonBox.RejectRole:
                btn.setStyleSheet("""
                    QPushButton {
                        background: #24324f;
                        color: #f7f4ec;
                        border-radius: 8px;
                        padding: 8px 16px;
                        font-weight: 700;
                        font-family: "Segoe UI";
                    }
                    QPushButton:hover {
                        background: #324362;
                    }
                """)

    def selected_format(self):
        return "pdf" if self.format_combo.currentIndex() == 0 else "excel"


class StatCard(QFrame):
    def __init__(self, title, value="0"):
        super().__init__()
        self.setObjectName("StatCard")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)

        self.title = QLabel(title)
        self.title.setObjectName("CardTitle")

        self.value = QLabel(value)
        self.value.setObjectName("CardValue")

        layout.addWidget(self.title)
        layout.addWidget(self.value)

    def set_value(self, value):
        self.value.setText(str(value))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.worker = None
        self.ip_cameras = []  # saved IP camera URLs added by the user
        self.setWindowTitle("AI Attendance System")
        self.setMinimumSize(1180, 760)

        self.build_ui()
        self.apply_style()
        self.refresh_camera_list()

    def build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        root = QHBoxLayout(central)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(18)

        # ================= SIDEBAR =================
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(245)

        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 22, 18, 18)
        side.setSpacing(12)

        # ================= LOGO =================
        logo = QLabel()
        logo.setObjectName("Logo")
        logo.setAlignment(Qt.AlignCenter)

        # Explicitly make the QLabel transparent so the sidebar color
        # is visible through transparent pixels of the PNG.
        logo.setAttribute(Qt.WA_TranslucentBackground, True)

        logo_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "vision_logo.png"
        )

        logo_image = QImage(logo_path)

        if not logo_image.isNull():
            # Force an RGBA image format while preserving the original
            # alpha channel. Transparent pixels remain transparent.
            if not logo_image.hasAlphaChannel():
                logo_image = logo_image.convertToFormat(QImage.Format_RGBA8888)
            else:
                logo_image = logo_image.convertToFormat(QImage.Format_RGBA8888)

            logo_pixmap = QPixmap.fromImage(
                logo_image,
                Qt.AutoColor
            )

            if not logo_pixmap.isNull():
                logo_pixmap = logo_pixmap.scaled(
                    160,
                    160,
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation
                )
                logo.setPixmap(logo_pixmap)
            else:
                logo.setText("VISION")
        else:
            # Fallback if vision_logo.png isn't next to this script.
            logo.setText("VISION")

        side.addWidget(logo)
        side.addSpacing(20)

        # ================= CAMERA SELECTOR =================
        cam_label = QLabel("CAMERA SOURCE")
        cam_label.setObjectName("SectionLabel")
        side.addWidget(cam_label)

        cam_row = QHBoxLayout()
        cam_row.setSpacing(8)

        self.camera_combo = QComboBox()
        self.camera_combo.setObjectName("CameraCombo")
        cam_row.addWidget(self.camera_combo, 1)

        self.scan_btn = QPushButton("⟳")
        self.scan_btn.setObjectName("ScanButton")
        self.scan_btn.setFixedSize(38, 38)
        self.scan_btn.setToolTip("Rescan local cameras")
        self.scan_btn.clicked.connect(self.refresh_camera_list)
        cam_row.addWidget(self.scan_btn)

        side.addLayout(cam_row)

        self.camera_hint = QLabel("USB cameras + IP cameras on the same Wi-Fi")
        self.camera_hint.setObjectName("CameraHint")
        self.camera_hint.setWordWrap(True)
        side.addWidget(self.camera_hint)

        side.addSpacing(10)

        self.status = QLabel("●  SYSTEM READY")
        self.status.setObjectName("Status")
        side.addWidget(self.status)

        side.addSpacing(20)
        side.addStretch()

        self.start_btn = QPushButton("START CAMERA")
        self.start_btn.setObjectName("PrimaryButton")
        self.start_btn.clicked.connect(self.start_camera)
        side.addWidget(self.start_btn)

        self.stop_btn = QPushButton("STOP CAMERA")
        self.stop_btn.setObjectName("DangerButton")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_camera)
        side.addWidget(self.stop_btn)

        # ====== NEW: REPORT BUTTON ======
        self.report_btn = QPushButton("GENERATE REPORT")
        self.report_btn.setObjectName("ReportButton")
        self.report_btn.clicked.connect(self.generate_report)
        side.addWidget(self.report_btn)

        root.addWidget(sidebar)

        # ================= MAIN =================
        main = QVBoxLayout()
        main.setSpacing(14)

        header = QHBoxLayout()
        header.addStretch()

        self.clock = QLabel("--:--:--")
        self.clock.setObjectName("Clock")
        header.addWidget(self.clock)

        main.addLayout(header)

        # ================= ATTENDANCE LOG =================
        attendance_frame = QFrame()
        attendance_frame.setObjectName("AttendanceFrame")

        attendance_layout = QVBoxLayout(attendance_frame)
        attendance_layout.setContentsMargins(20, 18, 20, 20)
        attendance_layout.setSpacing(12)

        attendance_head = QHBoxLayout()
        head_box = QVBoxLayout()
        head_box.setSpacing(2)

        attendance_title = QLabel("ATTENDANCE LOG")
        attendance_title.setObjectName("AttendanceHeader")

        self.attendance_subtitle = QLabel(
            "Start the camera to begin recording attendance"
        )
        self.attendance_subtitle.setObjectName("AttendanceSubtitle")

        head_box.addWidget(attendance_title)
        head_box.addWidget(self.attendance_subtitle)

        attendance_head.addLayout(head_box)
        attendance_head.addStretch()

        attendance_layout.addLayout(attendance_head)

        self.attendance_table = QTableWidget(0, 3)
        self.attendance_table.setObjectName("AttendanceTable")
        self.attendance_table.setHorizontalHeaderLabels(["PHOTO", "NAME", "TIME"])
        self.attendance_table.horizontalHeaderItem(1).setTextAlignment(
            Qt.AlignVCenter | Qt.AlignLeft
        )
        self.attendance_table.verticalHeader().setVisible(False)
        self.attendance_table.verticalHeader().setDefaultSectionSize(86)
        self.attendance_table.setShowGrid(False)
        self.attendance_table.setAlternatingRowColors(True)
        self.attendance_table.setFocusPolicy(Qt.NoFocus)
        self.attendance_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.attendance_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.attendance_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Fixed
        )
        self.attendance_table.setColumnWidth(0, 90)
        self.attendance_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch
        )
        self.attendance_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeToContents
        )
        self.attendance_table.setMinimumHeight(420)

        attendance_layout.addWidget(self.attendance_table)

        main.addWidget(attendance_frame, 1)

        # ================= CARDS =================
        cards = QGridLayout()
        cards.setSpacing(12)

        self.fake_card = StatCard("FAKE / UNKNOWN")
        self.faces_card = StatCard("DETECTED FACES")
        self.known_card = StatCard("KNOWN PEOPLE")
        self.rec_card = StatCard("ATTENDANCE")

        cards.addWidget(self.fake_card, 0, 0)
        cards.addWidget(self.faces_card, 0, 1)
        cards.addWidget(self.known_card, 0, 2)
        cards.addWidget(self.rec_card, 0, 3)

        main.addLayout(cards)

        root.addLayout(main, 1)

    # ================= CAMERA SOURCE MANAGEMENT =================

    def scan_local_cameras(self, max_index=5):
        """Probe local camera indices 0..max_index-1 and return working ones."""
        found = []
        for i in range(max_index):
            cap = cv2.VideoCapture(i)
            if cap is not None and cap.isOpened():
                found.append(i)
                cap.release()
        if not found:
            found = [0]  # keep a default so the combo is never empty
        return found

    def refresh_camera_list(self):
        """Rebuild the camera combo: local cameras, saved IP cameras, add option."""
        self.camera_combo.blockSignals(True)
        self.camera_combo.clear()

        for idx in self.scan_local_cameras():
            self.camera_combo.addItem(f"📷 Camera {idx}", idx)

        if self.ip_cameras:
            self.camera_combo.insertSeparator(self.camera_combo.count())
            for url in self.ip_cameras:
                short = url if len(url) <= 32 else url[:29] + "..."
                self.camera_combo.addItem(f"🌐 {short}", url)

        self.camera_combo.insertSeparator(self.camera_combo.count())
        self.camera_combo.addItem("＋ Add IP Camera / NVR…", "ADD_IP")

        self.camera_combo.blockSignals(False)

    def prompt_ip_camera(self):
        """Ask the user for an IP camera URL; add it to the list if valid."""
        dialog = IpCameraDialog(self)

        if dialog.exec() != QDialog.Accepted:
            return None

        url = dialog.url()

        if not url:
            return None

        # Quick connectivity check so a typo fails fast with a clear message.
        probe = cv2.VideoCapture(url)
        ok = probe is not None and probe.isOpened() and probe.read()[0]
        if probe is not None:
            probe.release()

        if not ok:
            QMessageBox.warning(
                self,
                "Connection Failed",
                "Could not open this stream.\n"
                "Check the URL, username/password, and that the camera "
                "is on the same network."
            )
            return None

        if url not in self.ip_cameras:
            self.ip_cameras.append(url)
            self.refresh_camera_list()

        # Select the newly added camera.
        for i in range(self.camera_combo.count()):
            if self.camera_combo.itemData(i) == url:
                self.camera_combo.setCurrentIndex(i)
                break

        return url

    def current_camera_source(self):
        """Return the selected camera source (int index or str URL).

        Returns None if the user cancelled adding an IP camera.
        """
        data = self.camera_combo.currentData()

        if data == "ADD_IP":
            return self.prompt_ip_camera()

        return data

    def set_camera_controls_enabled(self, enabled):
        self.camera_combo.setEnabled(enabled)
        self.scan_btn.setEnabled(enabled)

    def apply_style(self):
        self.setStyleSheet("""
        QMainWindow {
            background: #f7f4ec;
        }

        QWidget {
            color: #29334a;
            font-family: "Segoe UI";
        }

        #Sidebar {
            background: #1f2d48;
            border: 1px solid #1f2d48;
            border-radius: 18px;
        }

        #Logo {
            background: transparent;
            border: none;
            padding: 4px 0;
        }

        #SectionLabel {
            color: #f1d9a7;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 1px;
        }

        #CameraCombo {
            background: #ffffff;
            color: #29334a;
            border: 1px solid #cbd2dc;
            border-radius: 10px;
            padding: 9px;
            font-size: 12px;
        }

        #CameraCombo::drop-down {
            border: none;
            width: 26px;
        }

        #CameraCombo QAbstractItemView {
            background: #ffffff;
            color: #29334a;
            selection-background-color: #24324f;
            selection-color: #f7f4ec;
        }

        #CameraCombo:disabled {
            background: #5d6677;
            color: #aeb5bf;
        }

        #ScanButton {
            background: #324362;
            color: #f7f4ec;
            border-radius: 10px;
            font-size: 16px;
            font-weight: 700;
        }

        #ScanButton:hover {
            background: #435474;
        }

        #ScanButton:disabled {
            background: #27334c;
            color: #8993a3;
        }

        #CameraHint {
            color: #8993a3;
            font-size: 11px;
        }

        #Status {
            background: #324362;
            border: 1px solid #53627a;
            border-radius: 9px;
            padding: 10px;
            color: #f1d9a7;
            font-weight: 700;
        }

        QPushButton {
            border-radius: 10px;
            padding: 12px;
            font-weight: 700;
        }

        #PrimaryButton {
            background: #d1b37a;
            color: #24324f;
        }

        #PrimaryButton:hover {
            background: #ddc392;
        }

        #PrimaryButton:disabled {
            background: #5d6677;
            color: #aeb5bf;
        }

        #DangerButton {
            background: #324362;
            color: #f7f4ec;
        }

        #DangerButton:hover {
            background: #435474;
        }

        #DangerButton:disabled {
            background: #27334c;
            color: #8993a3;
        }

        #ReportButton {
            background: #d1b37a;
            color: #24324f;
        }

        #ReportButton:hover {
            background: #ddc392;
        }

        #ReportButton:disabled {
            background: #5d6677;
            color: #aeb5bf;
        }

        #Clock {
            font-size: 20px;
            font-weight: 700;
            color: #24324f;
            padding: 8px;
        }

        #AttendanceFrame {
            background: #ffffff;
            border: 1px solid #e3dfd6;
            border-radius: 16px;
        }

        #AttendanceHeader {
            font-size: 15px;
            font-weight: 800;
            color: #24324f;
            letter-spacing: 1px;
        }

        #AttendanceSubtitle {
            color: #7d8797;
            font-size: 12px;
        }

        QTableWidget {
            background: transparent;
            border: none;
            color: #29334a;
            font-size: 13px;
            alternate-background-color: #f5f3ee;
        }

        QTableWidget::item {
            padding: 10px 10px;
            border-bottom: 1px solid #e8e4db;
        }

        QHeaderView::section {
            background: #24324f;
            color: #f7f4ec;
            padding: 10px 10px;
            border: none;
            font-weight: 700;
            font-size: 11px;
            letter-spacing: 1px;
        }

        #FaceThumb {
            background: #eef0f3;
            border: 1px solid #cbd2dc;
            border-radius: 6px;
        }

        QScrollBar:vertical {
            background: #f1efe9;
            width: 10px;
            margin: 0;
            border-radius: 5px;
        }

        QScrollBar::handle:vertical {
            background: #aeb7c5;
            border-radius: 5px;
            min-height: 24px;
        }

        QScrollBar::handle:vertical:hover {
            background: #d1b37a;
        }

        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
            height: 0;
        }

        #StatCard {
            background: #ffffff;
            border: 1px solid #e3dfd6;
            border-radius: 13px;
        }

        #CardTitle {
            color: #7d8797;
            font-size: 11px;
            font-weight: 700;
        }

        #CardValue {
            color: #24324f;
            font-size: 24px;
            font-weight: 800;
        }
        """)

    def start_camera(self):
        if self.worker and self.worker.isRunning():
            return

        source = self.current_camera_source()
        if source is None:
            return  # user cancelled adding an IP camera

        self.attendance_table.setRowCount(0)
        self.attendance_subtitle.setText("Initializing AI models...")

        self.status.setText("●  CAMERA RUNNING")
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.set_camera_controls_enabled(False)

        self.worker = CameraWorker(source)
        self.worker.attendance_added.connect(self.add_attendance_row)
        self.worker.stats_ready.connect(self.update_stats)
        self.worker.error.connect(self.worker_error)
        self.worker.start()

    def stop_camera(self):
        if self.worker:
            self.worker.stop()
            self.worker.wait()
            self.worker = None

        self.status.setText("●  SYSTEM READY")
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.set_camera_controls_enabled(True)

        recorded = self.attendance_table.rowCount()
        if recorded == 0:
            self.attendance_subtitle.setText(
                "Start the camera to begin recording attendance"
            )
        else:
            self.attendance_subtitle.setText(
                f"Session ended — {recorded} attendee(s) recorded"
            )

    def add_attendance_row(self, name, time_str, image_bytes, width, height):
        self.attendance_table.insertRow(0)

        photo_holder = QWidget()
        photo_layout = QHBoxLayout(photo_holder)
        photo_layout.setContentsMargins(0, 0, 0, 0)
        photo_layout.setAlignment(Qt.AlignCenter)

        photo_label = QLabel()
        photo_label.setObjectName("FaceThumb")
        photo_label.setFixedSize(70, 70)
        photo_label.setAlignment(Qt.AlignCenter)

        if image_bytes and width > 0 and height > 0:
            qimage = QImage(
                image_bytes,
                width,
                height,
                width * 3,
                QImage.Format_RGB888
            )
            pixmap = QPixmap.fromImage(qimage)

            side = min(pixmap.width(), pixmap.height())
            square = pixmap.copy(
                (pixmap.width() - side) // 2,
                (pixmap.height() - side) // 2,
                side,
                side,
            )
            photo_label.setPixmap(
                square.scaled(
                    70,
                    70,
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation
                )
            )

        photo_layout.addWidget(photo_label)

        name_item = QTableWidgetItem(name)
        name_item.setTextAlignment(Qt.AlignVCenter | Qt.AlignLeft)

        time_item = QTableWidgetItem(time_str)
        time_item.setTextAlignment(Qt.AlignVCenter | Qt.AlignCenter)

        self.attendance_table.setCellWidget(0, 0, photo_holder)
        self.attendance_table.setItem(0, 1, name_item)
        self.attendance_table.setItem(0, 2, time_item)

        self.attendance_subtitle.setText(
            f"{self.attendance_table.rowCount()} attendee(s) recorded"
        )

    def update_stats(self, stats):
        self.fake_card.set_value(stats["fake_or_unknown"])
        self.faces_card.set_value(stats["faces"])
        self.known_card.set_value(stats["known"])
        self.rec_card.set_value(stats["recognized"])
        self.clock.setText(stats["time"])

        if self.attendance_table.rowCount() == 0:
            self.attendance_subtitle.setText("Listening for faces...")

    def worker_error(self, message):
        self.status.setText("●  SYSTEM ERROR")
        QMessageBox.critical(self, "AI System Error", message)
        self.stop_camera()

    # ================= REPORT GENERATION =================

    def get_attendance_data(self):
        """Extract attendance data from the table as list of dicts."""
        data = []

        for row in range(self.attendance_table.rowCount()):
            name_item = self.attendance_table.item(row, 1)
            time_item = self.attendance_table.item(row, 2)

            if name_item and time_item:
                data.append({
                    "no": row + 1,
                    "name": name_item.text(),
                    "time": time_item.text(),
                })

        # Reverse to show chronological order (newest at bottom in report)
        data.reverse()

        for i, entry in enumerate(data, 1):
            entry["no"] = i

        return data

    def generate_report(self):
        """Open dialog to choose format, then generate the report."""
        data = self.get_attendance_data()

        if not data:
            QMessageBox.information(
                self,
                "No Data",
                "No attendance records to export.\n"
                "Start the camera and record some attendance first."
            )
            return

        dialog = ReportTypeDialog(self)

        if dialog.exec() != QDialog.Accepted:
            return

        fmt = dialog.selected_format()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if fmt == "pdf":
            default_name = f"Attendance_Report_{timestamp}.pdf"

            file_path, _ = QFileDialog.getSaveFileName(
                self,
                "Save PDF Report",
                default_name,
                "PDF Files (*.pdf)"
            )

            if file_path:
                self.export_to_pdf(file_path, data)

        else:
            default_name = f"Attendance_Report_{timestamp}.xlsx"

            file_path, _ = QFileDialog.getSaveFileName(
                self,
                "Save Excel Report",
                default_name,
                "Excel Files (*.xlsx)"
            )

            if file_path:
                self.export_to_excel(file_path, data)

    def export_to_excel(self, file_path, data):
        """Generate a styled Excel report using openpyxl."""
        try:
            wb = Workbook()
            ws = wb.active
            ws.title = "Attendance"

            # Title
            ws.merge_cells("A1:C1")
            title_cell = ws["A1"]
            title_cell.value = "AI Attendance System - Attendance Report"
            title_cell.font = Font(size=16, bold=True, color="FFFFFF")
            title_cell.fill = PatternFill(
                start_color="24324F",
                end_color="24324F",
                fill_type="solid"
            )
            title_cell.alignment = Alignment(
                horizontal="center",
                vertical="center"
            )
            ws.row_dimensions[1].height = 35

            # Date
            ws.merge_cells("A2:C2")
            date_cell = ws["A2"]
            date_cell.value = (
                f"Generated on: "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            date_cell.font = Font(
                size=11,
                italic=True,
                color="29334A"
            )
            date_cell.fill = PatternFill(
                start_color="F7F4EC",
                end_color="F7F4EC",
                fill_type="solid"
            )
            date_cell.alignment = Alignment(
                horizontal="center",
                vertical="center"
            )
            ws.row_dimensions[2].height = 25

            # Headers
            headers = ["#", "Name", "Time"]

            header_fill = PatternFill(
                start_color="24324F",
                end_color="24324F",
                fill_type="solid"
            )

            header_font = Font(
                bold=True,
                color="F7F4EC",
                size=12
            )

            thin_border = Border(
                left=Side(style="thin", color="CBD2DC"),
                right=Side(style="thin", color="CBD2DC"),
                top=Side(style="thin", color="CBD2DC"),
                bottom=Side(style="thin", color="CBD2DC"),
            )

            for col, header in enumerate(headers, 1):
                cell = ws.cell(
                    row=4,
                    column=col,
                    value=header
                )
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(
                    horizontal="center",
                    vertical="center"
                )
                cell.border = thin_border

            # Data rows
            alt_fill = PatternFill(
                start_color="F5F3EE",
                end_color="F5F3EE",
                fill_type="solid"
            )

            normal_fill = PatternFill(
                start_color="FFFFFF",
                end_color="FFFFFF",
                fill_type="solid"
            )

            data_font = Font(
                color="29334A",
                size=11
            )

            for i, entry in enumerate(data):
                row_num = i + 5
                row_fill = alt_fill if i % 2 == 0 else normal_fill

                for col, key in enumerate(
                    ["no", "name", "time"],
                    1
                ):
                    cell = ws.cell(
                        row=row_num,
                        column=col,
                        value=entry[key]
                    )
                    cell.fill = row_fill
                    cell.font = data_font
                    cell.border = thin_border
                    cell.alignment = Alignment(
                        horizontal="center" if col != 2 else "left",
                        vertical="center"
                    )

            # Column widths
            ws.column_dimensions["A"].width = 8
            ws.column_dimensions["B"].width = 35
            ws.column_dimensions["C"].width = 18

            # Auto row height
            for i in range(len(data)):
                ws.row_dimensions[i + 5].height = 22

            wb.save(file_path)

            QMessageBox.information(
                self,
                "Report Saved",
                f"Excel report saved successfully:\n{file_path}"
            )

        except Exception as e:
            QMessageBox.critical(
                self,
                "Export Error",
                f"Failed to save Excel report:\n{str(e)}"
            )

    def export_to_pdf(self, file_path, data):
        """Generate a styled PDF report using reportlab."""
        try:
            doc = SimpleDocTemplate(
                file_path,
                pagesize=A4,
                rightMargin=2 * cm,
                leftMargin=2 * cm,
                topMargin=2 * cm,
                bottomMargin=2 * cm,
            )

            elements = []
            styles = getSampleStyleSheet()

            # Custom style for RTL/Arabic support if needed
            title_style = ParagraphStyle(
                "CustomTitle",
                parent=styles["Heading1"],
                fontSize=18,
                textColor=colors.HexColor("#24324F"),
                spaceAfter=8,
                alignment=1,
                fontName="Helvetica-Bold",
            )

            subtitle_style = ParagraphStyle(
                "CustomSubtitle",
                parent=styles["Normal"],
                fontSize=10,
                textColor=colors.HexColor("#7D8797"),
                spaceAfter=20,
                alignment=1,
            )

            elements.append(
                Paragraph(
                    "AI Attendance System",
                    title_style
                )
            )

            elements.append(
                Paragraph(
                    "Attendance Report — Generated on "
                    f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    subtitle_style
                )
            )

            elements.append(Spacer(1, 12))

            # Table data
            table_data = [["#", "Name", "Time"]]

            for entry in data:
                table_data.append([
                    str(entry["no"]),
                    entry["name"],
                    entry["time"]
                ])

            # Create table
            col_widths = [
                1.5 * cm,
                10 * cm,
                3 * cm
            ]

            table = Table(
                table_data,
                colWidths=col_widths,
                repeatRows=1
            )

            # Styling
            header_color = colors.HexColor("#24324F")
            alt_color = colors.HexColor("#F5F3EE")
            normal_color = colors.HexColor("#FFFFFF")
            gold_color = colors.HexColor("#F7F4EC")

            style_commands = [
                (
                    "BACKGROUND",
                    (0, 0),
                    (-1, 0),
                    header_color
                ),
                (
                    "TEXTCOLOR",
                    (0, 0),
                    (-1, 0),
                    gold_color
                ),
                (
                    "ALIGN",
                    (0, 0),
                    (-1, -1),
                    "CENTER"
                ),
                (
                    "ALIGN",
                    (1, 1),
                    (1, -1),
                    "LEFT"
                ),
                (
                    "FONTNAME",
                    (0, 0),
                    (-1, 0),
                    "Helvetica-Bold"
                ),
                (
                    "FONTSIZE",
                    (0, 0),
                    (-1, 0),
                    12
                ),
                (
                    "BOTTOMPADDING",
                    (0, 0),
                    (-1, 0),
                    12
                ),
                (
                    "TOPPADDING",
                    (0, 0),
                    (-1, 0),
                    12
                ),
                (
                    "GRID",
                    (0, 0),
                    (-1, -1),
                    0.5,
                    colors.HexColor("#CBD2DC")
                ),
                (
                    "FONTSIZE",
                    (0, 1),
                    (-1, -1),
                    11
                ),
                (
                    "TEXTCOLOR",
                    (0, 1),
                    (-1, -1),
                    colors.HexColor("#29334A")
                ),
                (
                    "BOTTOMPADDING",
                    (0, 1),
                    (-1, -1),
                    10
                ),
                (
                    "TOPPADDING",
                    (0, 1),
                    (-1, -1),
                    10
                ),
            ]

            # Alternate row colors
            for i in range(1, len(table_data)):
                bg = alt_color if i % 2 == 0 else normal_color
                style_commands.append(
                    ("BACKGROUND", (0, i), (-1, i), bg)
                )

            table.setStyle(TableStyle(style_commands))
            elements.append(table)

            # Footer
            elements.append(Spacer(1, 20))

            footer_style = ParagraphStyle(
                "Footer",
                parent=styles["Normal"],
                fontSize=9,
                textColor=colors.HexColor("#7D8797"),
                alignment=1,
            )

            elements.append(
                Paragraph(
                    f"Total Attendees: {len(data)}  |  "
                    "Report generated by AI Attendance System",
                    footer_style
                )
            )

            doc.build(elements)

            QMessageBox.information(
                self,
                "Report Saved",
                f"PDF report saved successfully:\n{file_path}"
            )

        except Exception as e:
            QMessageBox.critical(
                self,
                "Export Error",
                f"Failed to save PDF report:\n{str(e)}"
            )

    def closeEvent(self, event):
        self.stop_camera()
        event.accept()


def main():
    app = QApplication(sys.argv)

    app.setApplicationName("AI Attendance System")
    app.setFont(QFont("Segoe UI", 10))

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
