"""
MarineGlory Video Maker

PySide6 GUI와 ffmpeg.exe 명령어를 이용해 회사 홍보영상용 MP4를 만드는
단일 파일 초기 버전입니다. 나중에 모듈을 분리하기 쉽도록 데이터 모델,
ffmpeg 실행 로직, GUI 클래스를 구분해서 작성했습니다.

실행 전 준비:
    pip install PySide6

ffmpeg 준비:
    1) ffmpeg.exe를 PATH에 등록하거나
    2) 프로그램에서 [ffmpeg 선택] 버튼으로 ffmpeg.exe 경로를 지정하세요.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


APP_NAME = "MarineGlory Video Maker"
OUTPUT_WIDTH = 1920
OUTPUT_HEIGHT = 1080


# 초기 템플릿: 사용자가 촬영 영상을 추가하면 이 순서대로 장면명/자막을
# 자동으로 채워 주기 위한 기본 홍보영상 구성입니다.
DEFAULT_SCENES: list[tuple[str, str]] = [
    ("항만 전경", "안전한 항만 물류의 시작"),
    ("선박 접안", "마린글로리 소금 하역 운영"),
    ("TBM 회의", "작업 전 위험요인 공유"),
    ("안전 준비", "기본을 지키는 안전문화"),
    ("본선 하역", "체계적인 본선 하역"),
    ("호퍼 투입", "안정적인 하역 공정"),
    ("컨베이어 이송", "설비와 연계된 이송 시스템"),
    ("설비 점검", "예방점검을 통한 안정 운영"),
    ("무전 교신", "현장 중심의 신속한 대응"),
    ("작업 완료", "안전과 신뢰로 완성하는 하역 작업"),
]


@dataclass
class Scene:
    """프로젝트 JSON에 저장되는 장면 정보입니다."""

    video_path: str = ""
    scene_name: str = ""
    start_time: str = "00:00:00"
    end_time: str = ""
    subtitle: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Scene":
        """이전 버전 JSON을 읽어도 기본값을 유지하도록 안전하게 복원합니다."""

        return cls(
            video_path=str(data.get("video_path", "")),
            scene_name=str(data.get("scene_name", "")),
            start_time=str(data.get("start_time", "00:00:00")),
            end_time=str(data.get("end_time", "")),
            subtitle=str(data.get("subtitle", "")),
        )


@dataclass
class Project:
    """프로젝트 전체 상태입니다. JSON 저장/불러오기 기준 구조입니다."""

    ffmpeg_path: str = ""
    logo_path: str = ""
    music_path: str = ""
    scenes: list[Scene] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Project":
        return cls(
            ffmpeg_path=str(data.get("ffmpeg_path", "")),
            logo_path=str(data.get("logo_path", "")),
            music_path=str(data.get("music_path", "")),
            scenes=[Scene.from_dict(item) for item in data.get("scenes", [])],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ffmpeg_path": self.ffmpeg_path,
            "logo_path": self.logo_path,
            "music_path": self.music_path,
            "scenes": [asdict(scene) for scene in self.scenes or []],
        }


def application_dir() -> Path:
    """main.py 실행 폴더 또는 PyInstaller exe가 있는 폴더를 반환합니다."""

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def find_default_ffmpeg() -> str:
    """exe/main.py 폴더, 현재 작업 폴더, PATH 순서로 ffmpeg.exe를 찾습니다."""

    for base_dir in [application_dir(), Path.cwd()]:
        local_ffmpeg = base_dir / "ffmpeg.exe"
        if local_ffmpeg.exists():
            return str(local_ffmpeg)

    path_ffmpeg = shutil.which("ffmpeg")
    if path_ffmpeg:
        return path_ffmpeg

    return ""


def find_korean_font() -> str:
    """한글 자막 깨짐을 줄이기 위해 Windows 기본 한글 폰트를 우선 사용합니다."""

    candidates = [
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "malgun.ttf",
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "malgunbd.ttf",
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "gulim.ttc",
    ]
    for font_path in candidates:
        if font_path.exists():
            return str(font_path)
    return ""


def ffmpeg_filter_path(path: str) -> str:
    """
    ffmpeg 필터 내부에서 쓰는 파일 경로를 안전하게 이스케이프합니다.

    Windows 경로의 역슬래시는 슬래시로 바꾸고, 드라이브 콜론과 따옴표는
    필터 문법에 맞게 처리합니다.
    """

    escaped = Path(path).as_posix()
    escaped = escaped.replace("\\", "/")
    escaped = escaped.replace(":", r"\:")
    escaped = escaped.replace("'", r"\'")
    return escaped


def parse_time_to_seconds(value: str) -> float:
    """
    사용자가 입력한 시간을 초 단위 float로 변환합니다.

    지원 형식:
        12
        12.5
        01:23
        00:01:23.5
    """

    value = value.strip()
    if not value:
        return 0.0

    if ":" not in value:
        return float(value)

    parts = value.split(":")
    if len(parts) > 3:
        raise ValueError(f"시간 형식이 올바르지 않습니다: {value}")

    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    return seconds


def format_seconds_for_ffmpeg(seconds: float) -> str:
    """ffmpeg 인자로 넘기기 좋은 초 단위 문자열을 만듭니다."""

    return f"{seconds:.3f}".rstrip("0").rstrip(".")


def format_seconds_for_display(seconds: float) -> str:
    """표와 안내창에 표시하기 좋은 HH:MM:SS.mmm 형식으로 변환합니다."""

    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    remain = seconds - (hours * 3600) - (minutes * 60)
    if abs(remain - round(remain)) < 0.001:
        return f"{hours:02d}:{minutes:02d}:{int(round(remain)):02d}"
    return f"{hours:02d}:{minutes:02d}:{remain:06.3f}"


def format_seconds_for_ass(seconds: float) -> str:
    """ASS 자막 Dialogue 시간 형식(H:MM:SS.cc)으로 변환합니다."""

    centiseconds = int(round(max(seconds, 0.0) * 100))
    hours = centiseconds // 360000
    centiseconds %= 360000
    minutes = centiseconds // 6000
    centiseconds %= 6000
    secs = centiseconds // 100
    centis = centiseconds % 100
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def escape_ass_text(text: str) -> str:
    """ASS 자막 본문에서 특수 문자를 안전하게 처리합니다."""

    return (
        text.replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\r\n", r"\N")
        .replace("\n", r"\N")
    )


def write_ass_subtitle(path: Path, subtitle: str, duration_seconds: float) -> None:
    """
    한글 자막을 안정적으로 입히기 위한 ASS 자막 파일을 생성합니다.

    drawtext는 Windows 경로, 폰트, 따옴표 조합에서 오류가 나기 쉬워서
    libass 기반 subtitles 필터가 읽을 수 있는 ASS 파일을 사용합니다.
    """

    safe_text = escape_ass_text(subtitle.strip())
    end_time = format_seconds_for_ass(duration_seconds)
    ass_text = f"""[Script Info]
ScriptType: v4.00+
WrapStyle: 2
ScaledBorderAndShadow: yes
PlayResX: {OUTPUT_WIDTH}
PlayResY: {OUTPUT_HEIGHT}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: MarineGlory,Malgun Gothic,54,&H00FFFFFF,&H000000FF,&HBF000000,&H66000000,0,0,0,0,100,100,0,0,3,3,0,2,80,80,86,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,{end_time},MarineGlory,,0,0,0,,{safe_text}
"""
    path.write_text(ass_text, encoding="utf-8-sig")


def read_video_duration_seconds(ffmpeg_path: str, video_path: str) -> float | None:
    """
    ffmpeg.exe로 영상 메타데이터를 읽어 전체 길이를 초 단위로 반환합니다.

    ffprobe가 없는 배포 환경을 고려해 ffmpeg -i 출력의 Duration 라인을
    파싱합니다. 읽지 못하면 None을 반환하고 export 검증에서 안내합니다.
    """

    if not ffmpeg_path or not Path(ffmpeg_path).exists() or not Path(video_path).exists():
        return None

    process = subprocess.run(
        [ffmpeg_path, "-hide_banner", "-i", video_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", process.stdout)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def run_process(command: list[str], log: Signal) -> None:
    """ffmpeg 프로세스를 실행하고 stdout/stderr를 GUI 로그로 전달합니다."""

    log.emit(" ".join(f'"{part}"' if " " in part else part for part in command))
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )

    assert process.stdout is not None
    for line in process.stdout:
        line = line.rstrip()
        if line:
            log.emit(line)

    return_code = process.wait()
    if return_code != 0:
        raise RuntimeError(f"ffmpeg 실행 실패: 종료 코드 {return_code}")


class ExportWorker(QObject):
    """시간이 오래 걸리는 영상 생성을 GUI와 분리해서 실행하는 작업자입니다."""

    log = Signal(str)
    progress = Signal(int, str)
    finished = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        ffmpeg_path: str,
        scenes: list[Scene],
        logo_path: str,
        music_path: str,
        output_path: str,
    ) -> None:
        super().__init__()
        self.ffmpeg_path = ffmpeg_path
        self.scenes = scenes
        self.logo_path = logo_path
        self.music_path = music_path
        self.output_path = output_path

    def run(self) -> None:
        """전체 export 절차: 장면별 렌더링 -> concat -> 음악 믹스."""

        try:
            self._validate()
            with tempfile.TemporaryDirectory(prefix="marineglory_") as temp_dir:
                temp_path = Path(temp_dir)
                rendered_clips: list[Path] = []

                for index, scene in enumerate(self.scenes, start=1):
                    percent = int(((index - 1) / len(self.scenes)) * 80)
                    label = f"[{index}/{len(self.scenes)}] 임시 클립 생성: {scene.scene_name}"
                    self.progress.emit(percent, label)
                    self.log.emit(label)
                    rendered_clips.append(self._render_scene(scene, index, temp_path))

                concat_output = temp_path / "concat.mp4"
                self.progress.emit(85, "장면 순서대로 병합 중...")
                self._concat_clips(rendered_clips, concat_output, temp_path)

                if self.music_path:
                    self.progress.emit(92, "배경음악 삽입 중...")
                    self._mix_background_music(concat_output)
                else:
                    self.progress.emit(92, "최종 파일 복사 중...")
                    self._copy_without_music(concat_output)

            self.progress.emit(100, "완료")
            self.finished.emit(self.output_path)
        except Exception as exc:
            self.failed.emit(str(exc))

    def _validate(self) -> None:
        """export 전에 필수 입력과 파일 존재 여부를 점검합니다."""

        if not self.ffmpeg_path or not Path(self.ffmpeg_path).exists():
            raise FileNotFoundError("ffmpeg.exe 경로가 없습니다. [ffmpeg 선택]으로 지정해 주세요.")
        if not self.scenes:
            raise ValueError("렌더링할 장면이 없습니다.")
        for scene in self.scenes:
            if not scene.video_path or not Path(scene.video_path).exists():
                raise FileNotFoundError(f"영상 파일을 찾을 수 없습니다: {scene.video_path}")
            if not scene.end_time.strip():
                raise ValueError(f"종료 시간이 비어 있습니다: {scene.scene_name or scene.video_path}")
            start_seconds = parse_time_to_seconds(scene.start_time.strip() or "0")
            end_seconds = parse_time_to_seconds(scene.end_time.strip())
            if start_seconds < 0 or end_seconds <= start_seconds:
                raise ValueError(
                    "시간 입력을 확인해 주세요. "
                    f"종료 시간은 시작 시간보다 커야 합니다: {scene.scene_name or scene.video_path}"
                )
        if self.logo_path and not Path(self.logo_path).exists():
            raise FileNotFoundError(f"로고 파일을 찾을 수 없습니다: {self.logo_path}")
        if self.music_path and not Path(self.music_path).exists():
            raise FileNotFoundError(f"배경음악 파일을 찾을 수 없습니다: {self.music_path}")

    def _render_scene(self, scene: Scene, index: int, temp_path: Path) -> Path:
        """하나의 원본 영상을 잘라 1920x1080, 자막, 로고가 포함된 MP4로 만듭니다."""

        output_clip = temp_path / f"scene_{index:03d}.mp4"

        # scale/crop은 비율이 다른 영상도 화면을 꽉 채우는 중앙 크롭 방식입니다.
        # 패딩 방식이 필요하면 force_original_aspect_ratio=decrease와 pad 필터로
        # 바꾸면 됩니다. 초기 버전은 홍보영상에 흔한 풀프레임 구성을 기본값으로 둡니다.
        video_filters = [
            (
                f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:"
                "force_original_aspect_ratio=increase"
            ),
            f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}",
            "setsar=1",
        ]

        start_seconds = parse_time_to_seconds(scene.start_time.strip() or "0")
        end_seconds = parse_time_to_seconds(scene.end_time.strip())
        duration_seconds = end_seconds - start_seconds
        if scene.subtitle.strip():
            subtitle_file = temp_path / f"subtitle_{index:03d}.ass"
            write_ass_subtitle(subtitle_file, scene.subtitle, duration_seconds)
            video_filters.append(f"subtitles='{ffmpeg_filter_path(str(subtitle_file))}'")

        self.log.emit(
            "임시 클립 생성: "
            f"{format_seconds_for_ffmpeg(start_seconds)}초 ~ "
            f"{format_seconds_for_ffmpeg(end_seconds)}초 "
            f"({format_seconds_for_ffmpeg(duration_seconds)}초)"
        )

        command = [
            self.ffmpeg_path,
            "-y",
            "-ss",
            format_seconds_for_ffmpeg(start_seconds),
            "-i",
            scene.video_path,
            "-t",
            format_seconds_for_ffmpeg(duration_seconds),
        ]

        if self.logo_path:
            command.extend(["-i", self.logo_path])
            # 로고는 가로 220px 기준으로 줄이고 우측 상단에 배치합니다.
            filter_complex = (
                f"[0:v]{','.join(video_filters)}[base];"
                "[1:v]scale=220:-1[logo];"
                "[base][logo]overlay=W-w-36:36[v]"
            )
            command.extend(["-filter_complex", filter_complex, "-map", "[v]"])
        else:
            command.extend(["-vf", ",".join(video_filters)])

        # 장면별 중간 파일은 concat 호환성을 위해 같은 코덱/해상도/프레임레이트로 맞춥니다.
        command.extend(
            [
                "-an",
                "-r",
                "30",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                str(output_clip),
            ]
        )
        run_process(command, self.log)
        return output_clip

    def _concat_clips(self, clips: list[Path], output_path: Path, temp_path: Path) -> None:
        """장면 순서대로 중간 MP4 파일을 하나로 이어 붙입니다."""

        concat_file = temp_path / "concat_list.txt"
        lines = [f"file '{clip.as_posix()}'" for clip in clips]
        concat_file.write_text("\n".join(lines), encoding="utf-8")

        self.log.emit("장면 병합 중...")
        command = [
            self.ffmpeg_path,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            str(output_path),
        ]
        run_process(command, self.log)

    def _mix_background_music(self, video_path: Path) -> None:
        """배경음악을 낮은 볼륨으로 반복 삽입하고 최종 MP4를 만듭니다."""

        self.log.emit("배경음악 삽입 중...")
        command = [
            self.ffmpeg_path,
            "-y",
            "-i",
            str(video_path),
            "-stream_loop",
            "-1",
            "-i",
            self.music_path,
            "-filter_complex",
            "[1:a]volume=0.18[aud]",
            "-map",
            "0:v",
            "-map",
            "[aud]",
            "-shortest",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            self.output_path,
        ]
        run_process(command, self.log)

    def _copy_without_music(self, video_path: Path) -> None:
        """배경음악이 없을 때는 병합 결과를 최종 파일로 복사합니다."""

        self.log.emit("배경음악 없음: 최종 파일 복사 중...")
        shutil.copy2(video_path, self.output_path)


class MainWindow(QMainWindow):
    """MarineGlory Video Maker 메인 GUI입니다."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 820)

        self.ffmpeg_edit = QLineEdit(find_default_ffmpeg())
        self.logo_edit = QLineEdit()
        self.music_edit = QLineEdit()
        self.output_edit = QLineEdit(str(Path.cwd() / "marineglory_promo.mp4"))
        self.table = QTableWidget(0, 5)
        self.log_edit = QPlainTextEdit()
        self.progress_bar = QProgressBar()
        self.export_button = QPushButton("최종 MP4 생성")

        self.worker_thread: QThread | None = None
        self.worker: ExportWorker | None = None

        self._build_ui()
        self._load_default_template()
        self._warn_if_ffmpeg_missing()

    def _build_ui(self) -> None:
        """버튼, 입력창, 표, 로그 영역을 배치합니다."""

        root = QWidget()
        main_layout = QVBoxLayout(root)
        self.setCentralWidget(root)

        path_box = QGroupBox("파일 설정")
        path_layout = QGridLayout(path_box)
        main_layout.addWidget(path_box)

        ffmpeg_button = QPushButton("ffmpeg 선택")
        logo_button = QPushButton("로고 PNG 선택")
        music_button = QPushButton("배경음악 MP3 선택")
        output_button = QPushButton("출력 MP4 선택")

        path_layout.addWidget(QLabel("ffmpeg.exe"), 0, 0)
        path_layout.addWidget(self.ffmpeg_edit, 0, 1)
        path_layout.addWidget(ffmpeg_button, 0, 2)
        path_layout.addWidget(QLabel("회사 로고 PNG"), 1, 0)
        path_layout.addWidget(self.logo_edit, 1, 1)
        path_layout.addWidget(logo_button, 1, 2)
        path_layout.addWidget(QLabel("배경음악 MP3"), 2, 0)
        path_layout.addWidget(self.music_edit, 2, 1)
        path_layout.addWidget(music_button, 2, 2)
        path_layout.addWidget(QLabel("최종 출력 MP4"), 3, 0)
        path_layout.addWidget(self.output_edit, 3, 1)
        path_layout.addWidget(output_button, 3, 2)

        scene_buttons = QHBoxLayout()
        main_layout.addLayout(scene_buttons)

        add_video_button = QPushButton("MP4 영상 추가")
        remove_button = QPushButton("선택 장면 삭제")
        up_button = QPushButton("위로 이동")
        down_button = QPushButton("아래로 이동")
        save_button = QPushButton("프로젝트 저장")
        load_button = QPushButton("프로젝트 불러오기")
        template_button = QPushButton("초기 템플릿 다시 만들기")

        for button in [
            add_video_button,
            remove_button,
            up_button,
            down_button,
            save_button,
            load_button,
            template_button,
            self.export_button,
        ]:
            scene_buttons.addWidget(button)

        self._setup_table()
        main_layout.addWidget(self.table, stretch=1)

        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("대기 중")
        main_layout.addWidget(self.progress_bar)

        log_label = QLabel("처리 로그")
        main_layout.addWidget(log_label)
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumBlockCount(3000)
        main_layout.addWidget(self.log_edit, stretch=1)

        ffmpeg_button.clicked.connect(self.choose_ffmpeg)
        logo_button.clicked.connect(self.choose_logo)
        music_button.clicked.connect(self.choose_music)
        output_button.clicked.connect(self.choose_output)
        add_video_button.clicked.connect(self.add_videos)
        remove_button.clicked.connect(self.remove_selected_scene)
        up_button.clicked.connect(lambda: self.move_selected_scene(-1))
        down_button.clicked.connect(lambda: self.move_selected_scene(1))
        save_button.clicked.connect(self.save_project)
        load_button.clicked.connect(self.load_project)
        template_button.clicked.connect(self.reset_template)
        self.export_button.clicked.connect(self.export_video)

    def _setup_table(self) -> None:
        """장면 목록 표의 컬럼과 기본 편집 동작을 설정합니다."""

        headers = ["영상 파일", "장면명", "시작 시간", "종료 시간", "자막"]
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.verticalHeader().setVisible(True)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)

    def _warn_if_ffmpeg_missing(self) -> None:
        """ffmpeg.exe가 없으면 사용자가 바로 알 수 있도록 안내합니다."""

        if not self.ffmpeg_edit.text().strip():
            QMessageBox.information(
                self,
                "ffmpeg.exe 필요",
                "ffmpeg.exe 경로를 찾지 못했습니다.\n"
                "ffmpeg를 설치한 뒤 PATH에 등록하거나 [ffmpeg 선택] 버튼으로 지정해 주세요.",
            )

    def _load_default_template(self) -> None:
        """프로그램 시작 시 기본 10개 장면 템플릿을 표에 넣습니다."""

        self.table.setRowCount(0)
        for scene_name, subtitle in DEFAULT_SCENES:
            self._append_scene(Scene(scene_name=scene_name, subtitle=subtitle))

    def _append_scene(self, scene: Scene) -> None:
        """표 마지막에 장면 한 줄을 추가합니다."""

        row = self.table.rowCount()
        self.table.insertRow(row)
        values = [
            scene.video_path,
            scene.scene_name,
            scene.start_time,
            scene.end_time,
            scene.subtitle,
        ]
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column in (2, 3):
                item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, column, item)

    def _scene_from_row(self, row: int) -> Scene:
        """표 한 줄을 Scene 데이터로 변환합니다."""

        def text(column: int) -> str:
            item = self.table.item(row, column)
            return item.text().strip() if item else ""

        return Scene(
            video_path=text(0),
            scene_name=text(1),
            start_time=text(2) or "00:00:00",
            end_time=text(3),
            subtitle=text(4),
        )

    def _all_scenes(self) -> list[Scene]:
        """현재 표의 모든 장면을 순서대로 가져옵니다."""

        return [self._scene_from_row(row) for row in range(self.table.rowCount())]

    def _set_scenes(self, scenes: list[Scene]) -> None:
        """JSON 불러오기나 행 이동 후 표 전체를 다시 그립니다."""

        self.table.setRowCount(0)
        for scene in scenes:
            self._append_scene(scene)

    def log(self, message: str) -> None:
        """GUI 로그 창에 메시지를 누적합니다."""

        self.log_edit.appendPlainText(message)
        self.log_edit.verticalScrollBar().setValue(self.log_edit.verticalScrollBar().maximum())

    def choose_ffmpeg(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "ffmpeg.exe 선택",
            str(Path.cwd()),
            "ffmpeg 실행 파일 (ffmpeg.exe);;모든 파일 (*.*)",
        )
        if file_path:
            self.ffmpeg_edit.setText(file_path)

    def choose_logo(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "회사 로고 PNG 선택",
            str(Path.cwd()),
            "PNG 이미지 (*.png);;모든 파일 (*.*)",
        )
        if file_path:
            self.logo_edit.setText(file_path)

    def choose_music(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "배경음악 MP3 선택",
            str(Path.cwd()),
            "MP3 오디오 (*.mp3);;모든 파일 (*.*)",
        )
        if file_path:
            self.music_edit.setText(file_path)

    def choose_output(self) -> None:
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "최종 MP4 저장 위치 선택",
            self.output_edit.text().strip() or str(Path.cwd() / "marineglory_promo.mp4"),
            "MP4 영상 (*.mp4)",
        )
        if file_path:
            if not file_path.lower().endswith(".mp4"):
                file_path += ".mp4"
            self.output_edit.setText(file_path)

    def add_videos(self) -> None:
        """여러 MP4 파일을 추가합니다. 빈 템플릿 행이 있으면 먼저 채웁니다."""

        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "MP4 영상 여러 개 추가",
            str(Path.cwd()),
            "MP4 영상 (*.mp4);;모든 파일 (*.*)",
        )
        if not file_paths:
            return

        for file_path in file_paths:
            duration_text = self._duration_text_for_video(file_path)
            target_row = self._first_empty_video_row()
            if target_row is None:
                template_index = self.table.rowCount()
                scene_name, subtitle = (
                    DEFAULT_SCENES[template_index]
                    if template_index < len(DEFAULT_SCENES)
                    else (Path(file_path).stem, "")
                )
                self._append_scene(
                    Scene(
                        video_path=file_path,
                        scene_name=scene_name,
                        end_time=duration_text,
                        subtitle=subtitle,
                    )
                )
            else:
                self.table.item(target_row, 0).setText(file_path)
                if duration_text and not self.table.item(target_row, 3).text().strip():
                    self.table.item(target_row, 3).setText(duration_text)

    def _duration_text_for_video(self, file_path: str) -> str:
        """영상 길이를 읽어 표에 넣을 종료 시간 문자열로 변환합니다."""

        duration = read_video_duration_seconds(self.ffmpeg_edit.text().strip(), file_path)
        if duration is None:
            self.log(f"영상 길이 자동 입력 실패: {file_path}")
            return ""
        duration_text = format_seconds_for_display(duration)
        self.log(f"영상 길이 확인: {Path(file_path).name} = {duration_text}")
        return duration_text

    def _fill_missing_end_times(self, scenes: list[Scene]) -> tuple[list[Scene], list[str]]:
        """종료 시간이 비어 있는 장면은 원본 영상 전체 길이로 자동 보완합니다."""

        warnings: list[str] = []
        changed = False
        for scene in scenes:
            if scene.video_path.strip() and not scene.end_time.strip():
                duration = read_video_duration_seconds(
                    self.ffmpeg_edit.text().strip(),
                    scene.video_path,
                )
                if duration is None:
                    warnings.append(f"종료 시간 자동 입력 실패: {scene.scene_name or scene.video_path}")
                    continue
                scene.end_time = format_seconds_for_display(duration)
                changed = True
        if changed:
            self._set_scenes(scenes)
        return scenes, warnings

    def _first_empty_video_row(self) -> int | None:
        """영상 파일 칸이 비어 있는 첫 템플릿 행을 찾습니다."""

        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is None or not item.text().strip():
                return row
        return None

    def remove_selected_scene(self) -> None:
        selected = self.table.currentRow()
        if selected >= 0:
            self.table.removeRow(selected)

    def move_selected_scene(self, offset: int) -> None:
        """선택한 장면의 순서를 위/아래로 이동합니다."""

        current = self.table.currentRow()
        target = current + offset
        if current < 0 or target < 0 or target >= self.table.rowCount():
            return

        scenes = self._all_scenes()
        scenes[current], scenes[target] = scenes[target], scenes[current]
        self._set_scenes(scenes)
        self.table.selectRow(target)

    def reset_template(self) -> None:
        """기본 10개 장면으로 표를 초기화합니다."""

        answer = QMessageBox.question(
            self,
            "초기 템플릿",
            "현재 장면 목록을 초기 템플릿으로 다시 만들까요?",
        )
        if answer == QMessageBox.Yes:
            self._load_default_template()

    def save_project(self) -> None:
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "프로젝트 JSON 저장",
            str(Path.cwd() / "marineglory_project.json"),
            "JSON 파일 (*.json)",
        )
        if not file_path:
            return
        if not file_path.lower().endswith(".json"):
            file_path += ".json"

        project = Project(
            ffmpeg_path=self.ffmpeg_edit.text().strip(),
            logo_path=self.logo_edit.text().strip(),
            music_path=self.music_edit.text().strip(),
            scenes=self._all_scenes(),
        )
        Path(file_path).write_text(
            json.dumps(project.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.log(f"프로젝트 저장 완료: {file_path}")

    def load_project(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "프로젝트 JSON 불러오기",
            str(Path.cwd()),
            "JSON 파일 (*.json);;모든 파일 (*.*)",
        )
        if not file_path:
            return

        data = json.loads(Path(file_path).read_text(encoding="utf-8"))
        project = Project.from_dict(data)
        self.ffmpeg_edit.setText(project.ffmpeg_path)
        self.logo_edit.setText(project.logo_path)
        self.music_edit.setText(project.music_path)
        self._set_scenes(project.scenes or [])
        self.log(f"프로젝트 불러오기 완료: {file_path}")

    def _validate_before_export(
        self,
        scenes: list[Scene],
        output_path: str,
    ) -> tuple[list[str], list[str], list[str]]:
        """최종 생성 전 사용자에게 보여 줄 검증 결과를 만듭니다."""

        errors: list[str] = []
        warnings: list[str] = []
        summary: list[str] = []

        ffmpeg_path = self.ffmpeg_edit.text().strip()
        if not ffmpeg_path or not Path(ffmpeg_path).exists():
            errors.append("ffmpeg.exe 경로가 없습니다. ffmpeg 선택 버튼으로 지정해 주세요.")

        if not output_path:
            errors.append("최종 출력 MP4 파일명을 선택해 주세요.")
        elif Path(output_path).suffix.lower() != ".mp4":
            warnings.append("출력 파일 확장자가 .mp4가 아니어서 .mp4로 저장하는 것을 권장합니다.")

        logo_path = self.logo_edit.text().strip()
        if logo_path and not Path(logo_path).exists():
            errors.append(f"로고 PNG 파일을 찾을 수 없습니다: {logo_path}")

        music_path = self.music_edit.text().strip()
        if music_path and not Path(music_path).exists():
            errors.append(f"배경음악 MP3 파일을 찾을 수 없습니다: {music_path}")

        total_duration = 0.0
        for index, scene in enumerate(scenes, start=1):
            label = scene.scene_name or Path(scene.video_path).name or f"{index}번 장면"
            if not scene.video_path or not Path(scene.video_path).exists():
                errors.append(f"{index}번 장면 영상 파일을 찾을 수 없습니다: {scene.video_path}")
                continue
            if not scene.end_time.strip():
                errors.append(f"{index}번 장면 종료 시간이 비어 있습니다: {label}")
                continue
            try:
                start_seconds = parse_time_to_seconds(scene.start_time.strip() or "0")
                end_seconds = parse_time_to_seconds(scene.end_time.strip())
            except ValueError:
                errors.append(f"{index}번 장면 시간 형식이 올바르지 않습니다: {label}")
                continue
            if start_seconds < 0 or end_seconds <= start_seconds:
                errors.append(f"{index}번 장면 종료 시간은 시작 시간보다 커야 합니다: {label}")
                continue
            total_duration += end_seconds - start_seconds

        summary.append(f"장면 수: {len(scenes)}개")
        summary.append(f"예상 영상 길이: {format_seconds_for_display(total_duration)}")
        summary.append(f"로고: {'사용' if logo_path else '없음'}")
        summary.append(f"배경음악: {'사용' if music_path else '없음'}")
        summary.append(f"출력 파일: {output_path or '(미선택)'}")
        return errors, warnings, summary

    def _show_validation_dialog(
        self,
        errors: list[str],
        warnings: list[str],
        summary: list[str],
    ) -> bool:
        """검증 결과를 보기 쉽게 안내하고 진행 여부를 반환합니다."""

        if errors:
            message = "최종 MP4를 생성하기 전에 아래 항목을 수정해 주세요.\n\n"
            message += "\n".join(f"- {item}" for item in errors)
            if warnings:
                message += "\n\n참고:\n" + "\n".join(f"- {item}" for item in warnings)
            QMessageBox.warning(self, "입력값 확인 필요", message)
            return False

        message = "입력값 검증이 완료되었습니다.\n\n"
        message += "\n".join(f"- {item}" for item in summary)
        if warnings:
            message += "\n\n참고:\n" + "\n".join(f"- {item}" for item in warnings)
        message += "\n\n이 설정으로 최종 MP4를 생성할까요?"
        return (
            QMessageBox.question(self, "최종 생성 확인", message)
            == QMessageBox.Yes
        )

    def export_video(self) -> None:
        """입력값을 검증하고 별도 스레드에서 최종 MP4 생성을 시작합니다."""

        scenes = [scene for scene in self._all_scenes() if scene.video_path.strip()]
        scenes, duration_warnings = self._fill_missing_end_times(scenes)
        if not scenes:
            QMessageBox.warning(self, "장면 없음", "MP4 영상을 하나 이상 추가해 주세요.")
            return

        output_path = self.output_edit.text().strip()
        if not output_path:
            self.choose_output()
            output_path = self.output_edit.text().strip()
        if not output_path:
            return
        if Path(output_path).suffix.lower() != ".mp4":
            output_path += ".mp4"
            self.output_edit.setText(output_path)

        errors, warnings, summary = self._validate_before_export(scenes, output_path)
        warnings.extend(duration_warnings)
        if not self._show_validation_dialog(errors, warnings, summary):
            return

        output_parent = Path(output_path).parent
        output_parent.mkdir(parents=True, exist_ok=True)

        self.export_button.setEnabled(False)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("0% - 시작 준비")
        self.log("===== 최종 MP4 생성 시작 =====")

        self.worker_thread = QThread(self)
        self.worker = ExportWorker(
            ffmpeg_path=self.ffmpeg_edit.text().strip(),
            scenes=scenes,
            logo_path=self.logo_edit.text().strip(),
            music_path=self.music_edit.text().strip(),
            output_path=output_path,
        )
        self.worker.moveToThread(self.worker_thread)

        self.worker_thread.started.connect(self.worker.run)
        self.worker.log.connect(self.log)
        self.worker.progress.connect(self.update_progress)
        self.worker.finished.connect(self._export_finished)
        self.worker.failed.connect(self._export_failed)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker.failed.connect(self.worker_thread.quit)
        self.worker_thread.finished.connect(self.worker_thread.deleteLater)
        self.worker_thread.start()

    def update_progress(self, percent: int, message: str) -> None:
        """export 진행률을 장면 단위로 보기 쉽게 표시합니다."""

        percent = max(0, min(100, percent))
        self.progress_bar.setValue(percent)
        self.progress_bar.setFormat(f"{percent}% - {message}")
        self.log(f"진행률 {percent}%: {message}")

    def _export_finished(self, output_path: str) -> None:
        self.export_button.setEnabled(True)
        self.progress_bar.setValue(100)
        self.progress_bar.setFormat("100% - 완료")
        self.log(f"완료: {output_path}")
        QMessageBox.information(self, "완료", f"최종 MP4 생성이 완료되었습니다.\n{output_path}")
        self.worker = None
        self.worker_thread = None

    def _export_failed(self, error_message: str) -> None:
        self.export_button.setEnabled(True)
        self.progress_bar.setFormat("오류 발생")
        self.log(f"오류: {error_message}")
        QMessageBox.critical(self, "오류", error_message)
        self.worker = None
        self.worker_thread = None


def main() -> int:
    """Qt 애플리케이션 진입점입니다."""

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
