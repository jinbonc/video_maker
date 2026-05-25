"""
MarineGlory scene editor UI.

This lightweight PySide6 desktop tool edits marineglory_project.json while
leaving the existing main.py video generation flow intact.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, Qt, QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSplitter,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


APP_NAME = "MarineGlory Scene Editor"
PROJECT_FILE = "marineglory_project.json"
TRANSITION_MODES = ["없음", "크로스페이드", "페이드 투 블랙"]


def app_dir() -> Path:
    return Path(__file__).resolve().parent


def default_project_path() -> Path:
    return app_dir() / PROJECT_FILE


def format_timestamp(milliseconds: int) -> str:
    milliseconds = max(0, milliseconds)
    total_seconds, millis = divmod(milliseconds, 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def parse_time(value: str) -> float | None:
    value = value.strip()
    if not value:
        return None
    try:
        if ":" not in value:
            return float(value)
        seconds = 0.0
        for part in value.split(":"):
            seconds = seconds * 60 + float(part)
        return seconds
    except ValueError:
        return None


def format_duration(start_time: str, end_time: str) -> str:
    start_seconds = parse_time(start_time)
    end_seconds = parse_time(end_time)
    if start_seconds is None or end_seconds is None or end_seconds <= start_seconds:
        return "-"
    duration = end_seconds - start_seconds
    minutes = int(duration // 60)
    seconds = duration - minutes * 60
    if minutes:
        return f"{minutes}분 {seconds:.1f}초"
    return f"{seconds:.1f}초"


class SceneEditorWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1320, 780)

        self.project_path: Path | None = None
        self.project_data: dict[str, Any] = {"scenes": []}
        self.current_scene_index = -1
        self.updating_ui = False
        self.slider_is_pressed = False
        self.loaded_preview_path: Path | None = None

        self.media_player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.media_player.setAudioOutput(self.audio_output)

        self._build_ui()
        self._connect_signals()

        if default_project_path().exists():
            self.load_project(default_project_path())

    def _build_ui(self) -> None:
        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        self.setCentralWidget(central)

        toolbar = QHBoxLayout()
        self.open_button = QPushButton("프로젝트 열기")
        self.save_button = QPushButton("저장")
        self.generate_button = QPushButton("전체 영상 생성")
        self.path_label = QLabel("프로젝트가 열리지 않았습니다.")
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.path_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        toolbar.addWidget(self.open_button)
        toolbar.addWidget(self.save_button)
        toolbar.addWidget(self.generate_button)
        toolbar.addWidget(self.path_label)
        root.addLayout(toolbar)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("씬 목록"))
        self.scene_list = QListWidget()
        self.scene_list.setMinimumWidth(230)
        left_layout.addWidget(self.scene_list, 1)
        splitter.addWidget(left_panel)

        center_panel = QWidget()
        center_layout = QVBoxLayout(center_panel)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.addWidget(QLabel("영상 미리보기"))
        self.video_widget = QVideoWidget()
        self.video_widget.setMinimumSize(QSize(520, 300))
        self.video_widget.setStyleSheet("background: #15181c; border: 1px solid #343a40;")
        self.media_player.setVideoOutput(self.video_widget)
        center_layout.addWidget(self.video_widget, 1)

        preview_seek_layout = QHBoxLayout()
        self.position_slider = QSlider(Qt.Horizontal)
        self.position_slider.setRange(0, 0)
        self.time_label = QLabel("00:00:00.000 / 00:00:00.000")
        self.time_label.setMinimumWidth(220)
        self.time_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        preview_seek_layout.addWidget(self.position_slider, 1)
        preview_seek_layout.addWidget(self.time_label)
        center_layout.addLayout(preview_seek_layout)

        preview_controls = QHBoxLayout()
        self.play_button = QPushButton("재생")
        self.stop_button = QPushButton("정지")
        self.back_1s_button = QPushButton("1초 뒤로")
        self.forward_1s_button = QPushButton("1초 앞으로")
        self.set_start_button = QPushButton("현재 위치를 자막 시작 시간으로")
        self.set_end_button = QPushButton("현재 위치를 자막 종료 시간으로")
        self.preview_path_label = QLabel("선택한 씬의 클립 경로가 표시됩니다.")
        self.preview_path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.preview_path_label.setWordWrap(True)
        preview_controls.addWidget(self.play_button)
        preview_controls.addWidget(self.stop_button)
        preview_controls.addWidget(self.back_1s_button)
        preview_controls.addWidget(self.forward_1s_button)
        preview_controls.addWidget(self.set_start_button)
        preview_controls.addWidget(self.set_end_button)
        center_layout.addLayout(preview_controls)

        preview_path_layout = QHBoxLayout()
        preview_path_layout.addWidget(self.preview_path_label, 1)
        center_layout.addLayout(preview_path_layout)
        splitter.addWidget(center_panel)

        right_panel = QWidget()
        right_panel.setMinimumWidth(360)
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QLabel("씬 속성"))

        form_container = QFrame()
        form_container.setFrameShape(QFrame.StyledPanel)
        form = QFormLayout(form_container)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.title_edit = QLineEdit()
        self.clip_path_edit = QLineEdit()
        self.clip_browse_button = QToolButton()
        self.clip_browse_button.setIcon(self.style().standardIcon(QStyle.SP_DirOpenIcon))
        clip_row = QHBoxLayout()
        clip_row.addWidget(self.clip_path_edit, 1)
        clip_row.addWidget(self.clip_browse_button)
        self.subtitle_edit = QPlainTextEdit()
        self.subtitle_edit.setMinimumHeight(90)
        self.start_time_edit = QLineEdit()
        self.end_time_edit = QLineEdit()
        self.duration_label = QLabel("-")
        self.transition_combo = QComboBox()
        self.transition_combo.addItems(TRANSITION_MODES)
        self.transition_duration_spin = QDoubleSpinBox()
        self.transition_duration_spin.setRange(0.0, 5.0)
        self.transition_duration_spin.setSingleStep(0.1)
        self.transition_duration_spin.setDecimals(1)
        self.transition_duration_spin.setSuffix(" 초")

        form.addRow("씬 제목", self.title_edit)
        form.addRow("클립 파일 경로", clip_row)
        form.addRow("자막 내용", self.subtitle_edit)
        form.addRow("자막 시작 시간", self.start_time_edit)
        form.addRow("자막 종료 시간", self.end_time_edit)
        form.addRow("씬 길이", self.duration_label)
        form.addRow("전환 효과", self.transition_combo)
        form.addRow("전환 시간", self.transition_duration_spin)
        right_layout.addWidget(form_container)
        right_layout.addStretch(1)
        splitter.addWidget(right_panel)
        splitter.setSizes([260, 650, 390])

        timeline_header = QHBoxLayout()
        timeline_header.addWidget(QLabel("타임라인"))
        timeline_header.addStretch(1)
        self.move_left_button = QPushButton("앞으로")
        self.move_right_button = QPushButton("뒤로")
        timeline_header.addWidget(self.move_left_button)
        timeline_header.addWidget(self.move_right_button)
        root.addLayout(timeline_header)

        self.timeline_scroll = QScrollArea()
        self.timeline_scroll.setWidgetResizable(True)
        self.timeline_content = QWidget()
        self.timeline_layout = QHBoxLayout(self.timeline_content)
        self.timeline_layout.setContentsMargins(8, 8, 8, 8)
        self.timeline_layout.setSpacing(8)
        self.timeline_scroll.setWidget(self.timeline_content)
        self.timeline_scroll.setFixedHeight(96)
        root.addWidget(self.timeline_scroll)

    def _connect_signals(self) -> None:
        self.open_button.clicked.connect(self.choose_project)
        self.save_button.clicked.connect(self.save_project)
        self.generate_button.clicked.connect(self.run_main_py)
        self.scene_list.currentRowChanged.connect(self.select_scene)
        self.play_button.clicked.connect(self.play_preview)
        self.stop_button.clicked.connect(self.media_player.stop)
        self.back_1s_button.clicked.connect(lambda: self.seek_relative(-1000))
        self.forward_1s_button.clicked.connect(lambda: self.seek_relative(1000))
        self.set_start_button.clicked.connect(lambda: self.set_time_from_position(self.start_time_edit))
        self.set_end_button.clicked.connect(lambda: self.set_time_from_position(self.end_time_edit))
        self.clip_browse_button.clicked.connect(self.choose_clip)
        self.move_left_button.clicked.connect(lambda: self.move_scene(-1))
        self.move_right_button.clicked.connect(lambda: self.move_scene(1))
        self.position_slider.sliderPressed.connect(self._slider_pressed)
        self.position_slider.sliderReleased.connect(self._slider_released)
        self.position_slider.sliderMoved.connect(self.media_player.setPosition)
        self.media_player.positionChanged.connect(self._media_position_changed)
        self.media_player.durationChanged.connect(self._media_duration_changed)

        self.title_edit.textChanged.connect(self.apply_editor_to_scene)
        self.clip_path_edit.textChanged.connect(self.apply_editor_to_scene)
        self.subtitle_edit.textChanged.connect(self.apply_editor_to_scene)
        self.start_time_edit.textChanged.connect(self.apply_editor_to_scene)
        self.end_time_edit.textChanged.connect(self.apply_editor_to_scene)
        self.transition_combo.currentTextChanged.connect(self.apply_editor_to_scene)
        self.transition_duration_spin.valueChanged.connect(self.apply_editor_to_scene)

    def choose_project(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "프로젝트 JSON 열기",
            str(app_dir()),
            "JSON 파일 (*.json);;모든 파일 (*.*)",
        )
        if file_path:
            self.load_project(Path(file_path))

    def load_project(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            QMessageBox.critical(self, "프로젝트 열기 실패", str(exc))
            return

        if not isinstance(data, dict) or not isinstance(data.get("scenes", []), list):
            QMessageBox.warning(self, "프로젝트 형식 확인", "scenes 목록이 있는 JSON 파일을 선택해 주세요.")
            return

        self.media_player.stop()
        self.project_path = path
        self.project_data = data
        self.path_label.setText(str(path))
        self.refresh_scene_list()
        self.refresh_timeline()
        self.scene_list.setCurrentRow(0 if self.scenes else -1)

    @property
    def scenes(self) -> list[dict[str, Any]]:
        scenes = self.project_data.setdefault("scenes", [])
        return scenes if isinstance(scenes, list) else []

    def refresh_scene_list(self) -> None:
        self.updating_ui = True
        self.scene_list.clear()
        for index, scene in enumerate(self.scenes, start=1):
            title = str(scene.get("scene_name") or Path(str(scene.get("video_path", ""))).stem or "이름 없는 씬")
            item = QListWidgetItem(f"{index:02d}. {title}")
            self.scene_list.addItem(item)
        self.updating_ui = False

    def refresh_timeline(self) -> None:
        while self.timeline_layout.count():
            item = self.timeline_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        for index, scene in enumerate(self.scenes):
            title = str(scene.get("scene_name") or f"{index + 1}번 씬")
            duration = format_duration(str(scene.get("start_time", "")), str(scene.get("end_time", "")))
            button = QPushButton(f"{index + 1}\n{title}\n{duration}")
            button.setMinimumWidth(132)
            button.setMaximumWidth(180)
            button.setCheckable(True)
            button.setChecked(index == self.current_scene_index)
            button.clicked.connect(lambda checked=False, row=index: self.scene_list.setCurrentRow(row))
            self.timeline_layout.addWidget(button)
        self.timeline_layout.addStretch(1)

    def select_scene(self, row: int) -> None:
        self.apply_editor_to_scene()
        self.current_scene_index = row
        self.updating_ui = True
        if row < 0 or row >= len(self.scenes):
            self._clear_editor()
        else:
            scene = self.scenes[row]
            self.title_edit.setText(str(scene.get("scene_name", "")))
            self.clip_path_edit.setText(str(scene.get("video_path", "")))
            self.subtitle_edit.setPlainText(str(scene.get("subtitle", "")))
            self.start_time_edit.setText(str(scene.get("start_time", "00:00:00")))
            self.end_time_edit.setText(str(scene.get("end_time", "")))
            transition = str(self.project_data.get("transition_mode", "크로스페이드"))
            self.transition_combo.setCurrentText(transition if transition in TRANSITION_MODES else "크로스페이드")
            self.transition_duration_spin.setValue(float(self.project_data.get("transition_duration", 0.7)))
            self._update_preview_source(show_missing_message=True)
            self._update_duration_label()
        self.updating_ui = False
        self.refresh_timeline()

    def _clear_editor(self) -> None:
        self.title_edit.clear()
        self.clip_path_edit.clear()
        self.subtitle_edit.clear()
        self.start_time_edit.clear()
        self.end_time_edit.clear()
        self.duration_label.setText("-")
        self.preview_path_label.setText("선택한 씬의 클립 경로가 표시됩니다.")
        self.loaded_preview_path = None
        self.media_player.stop()
        self.media_player.setSource(QUrl())
        self._media_duration_changed(0)
        self._media_position_changed(0)

    def apply_editor_to_scene(self) -> None:
        if self.updating_ui:
            return
        row = self.current_scene_index
        if row < 0 or row >= len(self.scenes):
            return

        scene = self.scenes[row]
        scene["scene_name"] = self.title_edit.text().strip()
        scene["video_path"] = self.clip_path_edit.text().strip()
        scene["subtitle"] = self.subtitle_edit.toPlainText()
        scene["start_time"] = self.start_time_edit.text().strip()
        scene["end_time"] = self.end_time_edit.text().strip()
        self.project_data["transition_mode"] = self.transition_combo.currentText()
        self.project_data["transition_duration"] = self.transition_duration_spin.value()
        self._update_duration_label()
        self._update_preview_source()
        self.refresh_scene_list_preserving_row(row)
        self.refresh_timeline()

    def refresh_scene_list_preserving_row(self, row: int) -> None:
        self.refresh_scene_list()
        if 0 <= row < self.scene_list.count():
            was_blocked = self.scene_list.blockSignals(True)
            self.scene_list.setCurrentRow(row)
            self.scene_list.blockSignals(was_blocked)

    def _update_duration_label(self) -> None:
        self.duration_label.setText(format_duration(self.start_time_edit.text(), self.end_time_edit.text()))

    def resolve_clip_path(self, clip_path: str) -> Path:
        path = Path(clip_path.strip())
        if path.is_absolute():
            return path
        base_dir = self.project_path.parent if self.project_path else app_dir()
        return base_dir / path

    def _update_preview_source(self, show_missing_message: bool = False) -> None:
        clip_path = self.clip_path_edit.text().strip()
        self.preview_path_label.setText(clip_path or "클립 파일 경로가 비어 있습니다.")
        if not clip_path:
            self.loaded_preview_path = None
            self.media_player.stop()
            self.media_player.setSource(QUrl())
            self._media_duration_changed(0)
            self._media_position_changed(0)
            return

        path = self.resolve_clip_path(clip_path)
        self.preview_path_label.setText(f"{clip_path}\n실제 경로: {path}")
        if path.exists():
            if self.loaded_preview_path != path:
                self.loaded_preview_path = path
                self.media_player.stop()
                self.media_player.setSource(QUrl.fromLocalFile(str(path)))
            return

        self.loaded_preview_path = None
        self.media_player.stop()
        self.media_player.setSource(QUrl())
        self._media_duration_changed(0)
        self._media_position_changed(0)
        if show_missing_message:
            QMessageBox.warning(self, "클립 파일 없음", f"클립 파일을 찾을 수 없습니다.\n{path}")

    def choose_clip(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "클립 파일 선택",
            str(app_dir() / "clips"),
            "영상 파일 (*.mp4 *.mov *.m4v);;모든 파일 (*.*)",
        )
        if file_path:
            self.clip_path_edit.setText(file_path)

    def play_preview(self) -> None:
        clip_path = self.clip_path_edit.text().strip()
        if not clip_path:
            QMessageBox.information(self, "미리보기", "클립 파일 경로를 먼저 입력해 주세요.")
            return
        path = self.resolve_clip_path(clip_path)
        if not path.exists():
            QMessageBox.warning(self, "미리보기", f"파일을 찾을 수 없습니다.\n{path}")
            return
        if self.media_player.source().isEmpty():
            self.media_player.setSource(QUrl.fromLocalFile(str(path)))
            self.loaded_preview_path = path
        self.media_player.play()

    def seek_relative(self, milliseconds: int) -> None:
        duration = max(0, self.media_player.duration())
        next_position = self.media_player.position() + milliseconds
        if duration:
            next_position = min(next_position, duration)
        self.media_player.setPosition(max(0, next_position))

    def set_time_from_position(self, target: QLineEdit) -> None:
        target.setText(format_timestamp(self.media_player.position()))

    def _slider_pressed(self) -> None:
        self.slider_is_pressed = True

    def _slider_released(self) -> None:
        self.slider_is_pressed = False
        self.media_player.setPosition(self.position_slider.value())

    def _media_position_changed(self, position: int) -> None:
        if not self.slider_is_pressed:
            was_blocked = self.position_slider.blockSignals(True)
            self.position_slider.setValue(position)
            self.position_slider.blockSignals(was_blocked)
        self._update_time_label(position, self.media_player.duration())

    def _media_duration_changed(self, duration: int) -> None:
        was_blocked = self.position_slider.blockSignals(True)
        self.position_slider.setRange(0, max(0, duration))
        self.position_slider.blockSignals(was_blocked)
        self._update_time_label(self.media_player.position(), duration)

    def _update_time_label(self, position: int, duration: int) -> None:
        self.time_label.setText(f"{format_timestamp(position)} / {format_timestamp(duration)}")

    def move_scene(self, delta: int) -> None:
        row = self.current_scene_index
        new_row = row + delta
        if row < 0 or new_row < 0 or new_row >= len(self.scenes):
            return
        self.apply_editor_to_scene()
        self.scenes[row], self.scenes[new_row] = self.scenes[new_row], self.scenes[row]
        self.refresh_scene_list()
        self.scene_list.setCurrentRow(new_row)
        self.refresh_timeline()

    def save_project(self) -> None:
        self.apply_editor_to_scene()
        path = self.project_path
        if path is None:
            file_path, _ = QFileDialog.getSaveFileName(
                self,
                "프로젝트 JSON 저장",
                str(default_project_path()),
                "JSON 파일 (*.json)",
            )
            if not file_path:
                return
            path = Path(file_path)
            if path.suffix.lower() != ".json":
                path = path.with_suffix(".json")
            self.project_path = path

        if self._write_project(path):
            self.path_label.setText(str(path))
            QMessageBox.information(self, "저장 완료", f"프로젝트를 저장했습니다.\n{path}")

    def _write_project(self, path: Path) -> bool:
        try:
            path.write_text(
                json.dumps(self.project_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            QMessageBox.critical(self, "저장 실패", str(exc))
            return False
        return True

    def run_main_py(self) -> None:
        self.apply_editor_to_scene()
        if self.project_path is not None and not self._write_project(self.project_path):
            return
        main_path = app_dir() / "main.py"
        if not main_path.exists():
            QMessageBox.critical(self, "실행 실패", f"main.py를 찾을 수 없습니다.\n{main_path}")
            return
        try:
            subprocess.Popen([sys.executable, str(main_path)], cwd=str(app_dir()))
        except Exception as exc:
            QMessageBox.critical(self, "실행 실패", str(exc))


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    window = SceneEditorWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
