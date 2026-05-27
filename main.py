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
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QPlainTextEdit,
    QSlider,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


APP_NAME = "MarineGlory Video Maker"
OUTPUT_WIDTH = 1920
OUTPUT_HEIGHT = 1080
INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v"}
TRANSITION_NONE = "없음"
TRANSITION_CROSSFADE = "크로스페이드"
TRANSITION_FADE_BLACK = "페이드 투 블랙"
TRANSITION_MODES = [TRANSITION_NONE, TRANSITION_CROSSFADE, TRANSITION_FADE_BLACK]
SUBTITLE_MASK_NONE = "없음"
SUBTITLE_MASK_CROP = "하단 크롭"
SUBTITLE_MASK_BLUR = "하단 블러"
SUBTITLE_MASK_BAR = "하단 바 덮기"
SUBTITLE_MASK_MODES = [
    SUBTITLE_MASK_NONE,
    SUBTITLE_MASK_CROP,
    SUBTITLE_MASK_BLUR,
    SUBTITLE_MASK_BAR,
]
SCENE_MIN_DURATION_SECONDS = 1.0
SCENE_MAX_DURATION_SECONDS = 60.0
SCENE_DEFAULT_DURATION_SECONDS = 5.0
SCENE_DEFAULT_TRANSITION = TRANSITION_NONE
DURATION_MODE_FREEZE = "freeze"
DURATION_MODE_SLOW = "slow"
DURATION_MODE_LOOP = "loop"
DURATION_MODE_LABELS = {
    DURATION_MODE_FREEZE: "정지 연장",
    DURATION_MODE_SLOW: "슬로우 모션",
    DURATION_MODE_LOOP: "반복 재생",
}
DURATION_MODE_VALUES = {label: value for value, label in DURATION_MODE_LABELS.items()}
SCENE_DEFAULT_DURATION_MODE = DURATION_MODE_SLOW


def clamp_scene_duration(value: Any) -> float:
    try:
        duration = float(value)
    except (TypeError, ValueError):
        duration = SCENE_DEFAULT_DURATION_SECONDS
    return max(SCENE_MIN_DURATION_SECONDS, min(SCENE_MAX_DURATION_SECONDS, duration))


def normalize_scene_transition(value: Any) -> str:
    transition = str(value or "").strip()
    if transition == "cut":
        return TRANSITION_NONE
    return transition or SCENE_DEFAULT_TRANSITION


def normalize_duration_mode(value: Any) -> str:
    mode = str(value or "").strip()
    if mode in DURATION_MODE_LABELS:
        return mode
    if mode in DURATION_MODE_VALUES:
        return DURATION_MODE_VALUES[mode]
    return SCENE_DEFAULT_DURATION_MODE


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
    duration: float = SCENE_DEFAULT_DURATION_SECONDS
    transition: str = SCENE_DEFAULT_TRANSITION
    duration_mode: str = SCENE_DEFAULT_DURATION_MODE
    subtitle: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Scene":
        """이전 버전 JSON을 읽어도 기본값을 유지하도록 안전하게 복원합니다."""

        duration_value = data.get("duration")
        if duration_value is None:
            try:
                start_seconds = parse_time_to_seconds(str(data.get("start_time", "00:00:00")).strip() or "0")
                end_seconds = parse_time_to_seconds(str(data.get("end_time", "")).strip())
                duration_value = end_seconds - start_seconds
            except Exception:
                duration_value = SCENE_DEFAULT_DURATION_SECONDS

        return cls(
            video_path=str(data.get("clip", data.get("video_path", ""))),
            scene_name=str(data.get("title", data.get("scene_name", ""))),
            start_time=str(data.get("start_time", "00:00:00")),
            end_time=str(data.get("end_time", "")),
            duration=clamp_scene_duration(duration_value),
            transition=normalize_scene_transition(data.get("transition", SCENE_DEFAULT_TRANSITION)),
            duration_mode=normalize_duration_mode(data.get("duration_mode", SCENE_DEFAULT_DURATION_MODE)),
            subtitle=str(data.get("subtitle", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["title"] = self.scene_name
        data["clip"] = self.video_path
        data["duration"] = clamp_scene_duration(self.duration)
        data["transition"] = normalize_scene_transition(self.transition)
        data["duration_mode"] = normalize_duration_mode(self.duration_mode)
        return data


@dataclass
class Project:
    """프로젝트 전체 상태입니다. JSON 저장/불러오기 기준 구조입니다."""

    ffmpeg_path: str = ""
    logo_path: str = ""
    music_path: str = ""
    output_path: str = ""
    scenes: list[Scene] | None = None
    transition_mode: str = TRANSITION_CROSSFADE
    transition_duration: float = 0.7
    fade_in_enabled: bool = True
    fade_out_enabled: bool = True
    edge_fade_duration: float = 1.0
    subtitle_enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Project":
        transition_mode = str(data.get("transition_mode", TRANSITION_CROSSFADE))
        if transition_mode not in TRANSITION_MODES:
            transition_mode = TRANSITION_CROSSFADE
        return cls(
            ffmpeg_path=str(data.get("ffmpeg_path", "")),
            logo_path=str(data.get("logo_path", "")),
            music_path=str(data.get("music_path", "")),
            output_path=str(data.get("output_path", "")),
            scenes=[Scene.from_dict(item) for item in data.get("scenes", [])],
            transition_mode=transition_mode,
            transition_duration=float(data.get("transition_duration", 0.7)),
            fade_in_enabled=bool(data.get("fade_in_enabled", True)),
            fade_out_enabled=bool(data.get("fade_out_enabled", True)),
            edge_fade_duration=float(data.get("edge_fade_duration", 1.0)),
            subtitle_enabled=bool(data.get("subtitle_enabled", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ffmpeg_path": self.ffmpeg_path,
            "logo_path": self.logo_path,
            "music_path": self.music_path,
            "output_path": self.output_path,
            "scenes": [scene.to_dict() for scene in self.scenes or []],
            "transition_mode": self.transition_mode,
            "transition_duration": self.transition_duration,
            "fade_in_enabled": self.fade_in_enabled,
            "fade_out_enabled": self.fade_out_enabled,
            "edge_fade_duration": self.edge_fade_duration,
            "subtitle_enabled": self.subtitle_enabled,
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


def format_milliseconds_for_display(milliseconds: int) -> str:
    """QtMultimedia의 ms 단위 재생 위치를 HH:MM:SS.mmm 문자열로 바꿉니다."""

    return format_seconds_for_display(max(milliseconds, 0) / 1000)


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


def sanitize_filename_part(value: str, fallback: str = "scene") -> str:
    """Windows 파일명으로 사용할 수 없는 문자를 _로 바꿔 안전한 이름을 만듭니다."""

    safe_value = INVALID_FILENAME_CHARS.sub("_", value.strip())
    safe_value = safe_value.strip(" .")
    return safe_value or fallback


def is_supported_video_file(path: str) -> bool:
    """입력 영상은 mp4/mov/m4v를 지원하고, 출력은 mp4로 통일합니다."""

    return Path(path).suffix.lower() in ALLOWED_VIDEO_EXTENSIONS


def apply_subtitle_mask_filter(
    subtitle_mask_mode: str,
    subtitle_height: int,
    subtitle_y: int | None,
) -> tuple[str, str] | None:
    """
    기존 영상에 박혀 있는 하단 자막을 가리기 위한 ffmpeg 필터를 만듭니다.

    반환값의 첫 번째 값은 ffmpeg 옵션 종류입니다.
    - "vf": 단일 비디오 필터라서 -vf 로 전달합니다.
    - "filter_complex": split/overlay가 필요해서 -filter_complex 와 [v] map을 사용합니다.

    subtitle_y가 비어 있으면 영상 높이에서 subtitle_height를 뺀 하단 영역을 자동으로 잡습니다.
    시작 Y를 사용자가 직접 넣으면 그 위치부터 subtitle_height만큼을 처리합니다.
    """

    if subtitle_mask_mode == SUBTITLE_MASK_NONE:
        return None

    y_expr = str(subtitle_y) if subtitle_y is not None else f"ih-{subtitle_height}"
    overlay_y_expr = str(subtitle_y) if subtitle_y is not None else f"H-{subtitle_height}"

    if subtitle_mask_mode == SUBTITLE_MASK_CROP:
        # 하단 크롭은 자막이 있는 아래 영역을 잘라낸 뒤 최종 클립 크기를 1920x1080으로 맞춥니다.
        crop_height_expr = str(subtitle_y) if subtitle_y is not None else f"ih-{subtitle_height}"
        return "vf", f"crop=iw:{crop_height_expr}:0:0,scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}"

    if subtitle_mask_mode == SUBTITLE_MASK_BLUR:
        # 원본 프레임을 유지하고 자막 영역만 잘라 blur 처리한 뒤 같은 위치에 다시 올립니다.
        filter_text = (
            f"[0:v]split=2[base][blur];"
            f"[blur]crop=iw:{subtitle_height}:0:{y_expr},boxblur=12[blurred];"
            f"[base][blurred]overlay=0:{overlay_y_expr}[v]"
        )
        return "filter_complex", filter_text

    if subtitle_mask_mode == SUBTITLE_MASK_BAR:
        # 반투명 검정 바를 덮어 원본 자막이 홍보영상 자막과 겹치지 않도록 가립니다.
        return "vf", f"drawbox=x=0:y={y_expr}:w=iw:h={subtitle_height}:color=black@0.75:t=fill"

    raise ValueError(f"지원하지 않는 기존 자막 처리 방식입니다: {subtitle_mask_mode}")


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


def find_ffprobe_for_ffmpeg(ffmpeg_path: str) -> str:
    """ffmpeg.exe와 같은 폴더에 있는 ffprobe.exe를 찾습니다."""

    if not ffmpeg_path:
        return ""
    ffmpeg_file = Path(ffmpeg_path)
    candidates = [
        ffmpeg_file.with_name("ffprobe.exe"),
        ffmpeg_file.with_name("ffprobe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    path_ffprobe = shutil.which("ffprobe")
    return path_ffprobe or ""


def _short_process_output(output: str, limit: int = 1200) -> str:
    """ffmpeg/ffprobe 출력에서 로그로 보기 좋은 마지막 부분만 반환합니다."""

    output = output.strip()
    if not output:
        return "(출력 없음)"
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    text = "\n".join(lines[-12:])
    if len(text) > limit:
        text = "..." + text[-limit:]
    return text


def read_video_duration_seconds(ffmpeg_path: str, video_path: str, log: Any | None = None) -> float | None:
    """
    ffmpeg.exe로 영상 메타데이터를 읽어 전체 길이를 초 단위로 반환합니다.

    ffprobe가 없는 배포 환경을 고려해 ffmpeg -i 출력의 Duration 라인을
    파싱합니다. 읽지 못하면 None을 반환하고 export 검증에서 안내합니다.
    """

    def emit(message: str) -> None:
        if log is not None:
            log(message)

    if not ffmpeg_path or not Path(ffmpeg_path).exists():
        emit(f"영상 길이 읽기 실패: ffmpeg.exe 경로가 없습니다: {ffmpeg_path}")
        return None
    if not video_path or not Path(video_path).exists():
        emit(f"영상 길이 읽기 실패: 영상 파일이 없습니다: {video_path}")
        return None

    ffprobe_path = find_ffprobe_for_ffmpeg(ffmpeg_path)
    if ffprobe_path:
        try:
            process = subprocess.run(
                [
                    ffprobe_path,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    video_path,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                timeout=10,
            )
            output = process.stdout.strip()
            duration = float(output) if output else 0.0
            if process.returncode == 0 and duration > 0:
                emit(f"ffprobe 영상 길이 읽기 성공: {duration:.3f}초")
                return duration
            emit(
                "ffprobe 영상 길이 읽기 실패: "
                f"returncode={process.returncode}, output={_short_process_output(process.stdout)}"
            )
        except subprocess.TimeoutExpired:
            emit(f"ffprobe 실행 시간 초과: {video_path}")
        except Exception as exc:
            emit(f"ffprobe 실행 실패: {exc}")
    else:
        emit("ffprobe.exe를 찾지 못해 ffmpeg -i 출력 파싱으로 길이를 읽습니다.")

    try:
        process = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-i", video_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        emit(f"ffmpeg 영상 길이 읽기 시간 초과: {video_path}")
        return None
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", process.stdout)
    if not match:
        emit(
            "ffmpeg Duration 파싱 실패: "
            f"returncode={process.returncode}, output={_short_process_output(process.stdout)}"
        )
        return None
    hours, minutes, seconds = match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    emit(f"ffmpeg 영상 길이 읽기 성공: {duration:.3f}초")
    return duration


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
        transition_mode: str = TRANSITION_CROSSFADE,
        transition_duration: float = 0.7,
        fade_in_enabled: bool = True,
        fade_out_enabled: bool = True,
        edge_fade_duration: float = 1.0,
        subtitle_enabled: bool = True,
    ) -> None:
        super().__init__()
        self.ffmpeg_path = ffmpeg_path
        self.scenes = scenes
        self.logo_path = logo_path
        self.music_path = music_path
        self.output_path = output_path
        self.transition_mode = transition_mode
        self.transition_duration = transition_duration
        self.fade_in_enabled = fade_in_enabled
        self.fade_out_enabled = fade_out_enabled
        self.edge_fade_duration = edge_fade_duration
        self.subtitle_enabled = subtitle_enabled

    def run(self) -> None:
        """전체 export 절차: 장면별 렌더링 -> concat -> 음악 믹스."""

        try:
            self._validate()
            with tempfile.TemporaryDirectory(prefix="marineglory_") as temp_dir:
                temp_path = Path(temp_dir)
                rendered_clips: list[Path] = []
                clip_durations: list[float] = []

                for index, scene in enumerate(self.scenes, start=1):
                    percent = int(((index - 1) / len(self.scenes)) * 80)
                    label = f"[{index}/{len(self.scenes)}] 임시 클립 생성: {scene.scene_name}"
                    self.progress.emit(percent, label)
                    self.log.emit(label)
                    duration_seconds = self._scene_duration_seconds(scene)
                    clip_durations.append(duration_seconds)
                    rendered_clips.append(
                        self._render_scene(scene, index, temp_path, duration_seconds)
                    )

                concat_output = temp_path / "concat.mp4"
                self.progress.emit(85, "장면 전환 효과 적용 및 병합 중...")
                self._merge_rendered_clips(rendered_clips, clip_durations, concat_output, temp_path)

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
        if self.transition_mode not in TRANSITION_MODES:
            raise ValueError("장면 전환 방식이 올바르지 않습니다.")
        if not 0.2 <= self.transition_duration <= 2.0:
            raise ValueError("전환 시간은 0.2~2.0초 범위로 입력해 주세요.")
        if not 0.2 <= self.edge_fade_duration <= 3.0:
            raise ValueError("시작/종료 페이드 시간은 0.2~3.0초 범위로 입력해 주세요.")

        scene_durations: list[float] = []
        for scene in self.scenes:
            if not scene.video_path or not Path(scene.video_path).exists():
                raise FileNotFoundError(f"영상 파일을 찾을 수 없습니다: {scene.video_path}")
            render_duration = self._scene_duration_seconds(scene)
            if not SCENE_MIN_DURATION_SECONDS <= render_duration <= SCENE_MAX_DURATION_SECONDS:
                raise ValueError(
                    f"장면 재생시간은 {SCENE_MIN_DURATION_SECONDS:.0f}~{SCENE_MAX_DURATION_SECONDS:.0f}초 범위로 입력해 주세요: "
                    f"{scene.scene_name or scene.video_path}"
                )
            if not scene.end_time.strip():
                raise ValueError(f"종료 시간이 비어 있습니다: {scene.scene_name or scene.video_path}")
            start_seconds = parse_time_to_seconds(scene.start_time.strip() or "0")
            end_seconds = parse_time_to_seconds(scene.end_time.strip())
            if start_seconds < 0 or end_seconds <= start_seconds:
                raise ValueError(
                    "시간 입력을 확인해 주세요. "
                    f"종료 시간은 시작 시간보다 커야 합니다: {scene.scene_name or scene.video_path}"
                )
            scene_durations.append(render_duration)
        if self.logo_path and not Path(self.logo_path).exists():
            raise FileNotFoundError(f"로고 파일을 찾을 수 없습니다: {self.logo_path}")
        if self.music_path and not Path(self.music_path).exists():
            raise FileNotFoundError(f"배경음악 파일을 찾을 수 없습니다: {self.music_path}")
        if len(scene_durations) > 1 and self.transition_mode in (TRANSITION_CROSSFADE, TRANSITION_FADE_BLACK):
            shortest = min(scene_durations)
            if self.transition_duration >= shortest / 2:
                raise ValueError(
                    "전환 시간이 너무 깁니다. "
                    f"가장 짧은 장면({shortest:.1f}초)의 절반보다 짧게 설정해 주세요."
                )
        if self.fade_in_enabled and self.edge_fade_duration >= scene_durations[0]:
            raise ValueError("첫 장면 페이드 시간이 첫 장면 길이보다 짧아야 합니다.")
        if self.fade_out_enabled and self.edge_fade_duration >= scene_durations[-1]:
            raise ValueError("마지막 장면 페이드 시간이 마지막 장면 길이보다 짧아야 합니다.")
        if len(scene_durations) > 1 and self.transition_mode == TRANSITION_FADE_BLACK:
            if self.fade_in_enabled and self.edge_fade_duration + self.transition_duration >= scene_durations[0]:
                raise ValueError("첫 장면 길이가 시작 페이드와 전환 페이드를 함께 적용하기에 너무 짧습니다.")
            if self.fade_out_enabled and self.edge_fade_duration + self.transition_duration >= scene_durations[-1]:
                raise ValueError("마지막 장면 길이가 종료 페이드와 전환 페이드를 함께 적용하기에 너무 짧습니다.")

    def _scene_duration_seconds(self, scene: Scene) -> float:
        """장면의 시작/종료 시간으로 렌더링될 클립 길이를 계산합니다."""

        return clamp_scene_duration(scene.duration)

    def _render_scene(
        self,
        scene: Scene,
        index: int,
        temp_path: Path,
        duration_seconds: float,
    ) -> Path:
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
        source_duration_seconds = max(end_seconds - start_seconds, 0.001)
        duration_mode = normalize_duration_mode(scene.duration_mode)
        if duration_mode == DURATION_MODE_FREEZE:
            video_filters.extend(
                [
                    f"tpad=stop_mode=clone:stop_duration={duration_seconds:.3f}",
                    f"trim=duration={duration_seconds:.3f}",
                    "setpts=PTS-STARTPTS",
                ]
            )
        elif duration_mode == DURATION_MODE_SLOW:
            pts_factor = duration_seconds / source_duration_seconds
            video_filters.extend(
                [
                    f"setpts={pts_factor:.6f}*PTS",
                    f"trim=duration={duration_seconds:.3f}",
                    "setpts=PTS-STARTPTS",
                ]
            )
        else:
            video_filters.extend(
                [
                    f"trim=duration={duration_seconds:.3f}",
                    "setpts=PTS-STARTPTS",
                ]
            )
        if self.subtitle_enabled and scene.subtitle.strip():
            subtitle_file = temp_path / f"subtitle_{index:03d}.ass"
            write_ass_subtitle(subtitle_file, scene.subtitle, duration_seconds)
            video_filters.append(f"subtitles='{ffmpeg_filter_path(str(subtitle_file))}'")

        total_scenes = len(self.scenes)
        if self.transition_mode == TRANSITION_FADE_BLACK and total_scenes > 1:
            if index > 1:
                video_filters.append(f"fade=t=in:st=0:d={self.transition_duration:.3f}")
            if index < total_scenes:
                fade_start = max(duration_seconds - self.transition_duration, 0)
                video_filters.append(f"fade=t=out:st={fade_start:.3f}:d={self.transition_duration:.3f}")
        if self.fade_in_enabled and index == 1:
            video_filters.append(f"fade=t=in:st=0:d={self.edge_fade_duration:.3f}")
        if self.fade_out_enabled and index == total_scenes:
            fade_start = max(duration_seconds - self.edge_fade_duration, 0)
            video_filters.append(f"fade=t=out:st={fade_start:.3f}:d={self.edge_fade_duration:.3f}")

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
        ]
        if duration_mode == DURATION_MODE_LOOP and duration_seconds > source_duration_seconds:
            command.extend(["-stream_loop", "-1"])
            input_duration_seconds = duration_seconds
        else:
            input_duration_seconds = source_duration_seconds
        command.extend(
            [
                "-t",
                format_seconds_for_ffmpeg(input_duration_seconds),
                "-i",
                scene.video_path,
            ]
        )

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

    def _merge_rendered_clips(
        self,
        clips: list[Path],
        clip_durations: list[float],
        output_path: Path,
        temp_path: Path,
    ) -> None:
        """선택한 장면 전환 방식에 맞춰 렌더링된 임시 클립들을 하나로 합칩니다."""

        if len(clips) == 1:
            if self.transition_mode != TRANSITION_NONE:
                self.log.emit("장면이 1개뿐이어서 클립 사이 전환 효과는 적용하지 않습니다.")
            self._concat_clips(clips, output_path, temp_path)
            return

        if self.transition_mode == TRANSITION_CROSSFADE:
            self._xfade_clips(clips, clip_durations, output_path)
            return

        if self.transition_mode == TRANSITION_FADE_BLACK:
            self.log.emit("페이드 투 블랙 적용: 각 장면의 시작/끝 페이드를 넣은 뒤 병합합니다.")
            self._concat_clips(clips, output_path, temp_path)
            return

        self._concat_clips(clips, output_path, temp_path)

    def _xfade_clips(self, clips: list[Path], clip_durations: list[float], output_path: Path) -> None:
        """ffmpeg xfade 필터로 임시 클립 사이를 크로스페이드합니다."""

        transition_duration = self.transition_duration
        command = [self.ffmpeg_path, "-y"]
        for clip in clips:
            command.extend(["-i", str(clip)])

        filter_parts: list[str] = []
        previous_label = "0:v"
        current_timeline_duration = clip_durations[0]
        for input_index in range(1, len(clips)):
            output_label = f"v{input_index}"
            # xfade의 offset은 "현재까지 만들어진 타임라인에서 다음 클립과 겹치기 시작할 시점"입니다.
            # 첫 전환은 첫 클립 끝에서 transition_duration만큼 앞당긴 지점이고,
            # 이후에는 이전 xfade 때문에 전체 길이가 transition_duration씩 줄어든 상태를 누적합니다.
            offset = current_timeline_duration - transition_duration
            filter_parts.append(
                f"[{previous_label}][{input_index}:v]"
                f"xfade=transition=fade:duration={transition_duration:.3f}:offset={offset:.3f}"
                f"[{output_label}]"
            )
            current_timeline_duration += clip_durations[input_index] - transition_duration
            previous_label = output_label

        filter_complex = ";".join(filter_parts)
        command.extend(
            [
                "-filter_complex",
                filter_complex,
                "-map",
                f"[{previous_label}]",
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
                str(output_path),
            ]
        )
        self.log.emit(f"크로스페이드 적용: {transition_duration:.1f}초")
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


def _preview_scene_slice(scene: Scene, use_tail: bool, seconds: float = 2.0) -> tuple[Scene, float]:
    """전환 확인용으로 장면의 마지막/처음 일부 구간을 잘라낸 Scene을 만듭니다."""

    start_seconds = parse_time_to_seconds(scene.start_time.strip() or "0")
    end_seconds = parse_time_to_seconds(scene.end_time.strip())
    if end_seconds <= start_seconds:
        raise ValueError(f"장면 시간이 올바르지 않습니다: {scene.scene_name or scene.video_path}")

    if use_tail:
        slice_start = max(start_seconds, end_seconds - seconds)
        slice_end = end_seconds
    else:
        slice_start = start_seconds
        slice_end = min(end_seconds, start_seconds + seconds)

    sliced_scene = Scene(
        video_path=scene.video_path,
        scene_name=scene.scene_name,
        start_time=format_seconds_for_display(slice_start),
        end_time=format_seconds_for_display(slice_end),
        duration=clamp_scene_duration(slice_end - slice_start),
        transition=scene.transition,
        duration_mode=scene.duration_mode,
        subtitle=scene.subtitle,
    )
    return sliced_scene, slice_end - slice_start


def render_transition_preview(
    project_path: str | Path,
    scene_index: int,
    output_path: str | Path,
    log_callback: Any | None = None,
) -> Path:
    """
    선택 장면과 다음 장면의 짧은 구간만 렌더링해 전환 효과를 확인하는 MP4를 만듭니다.

    전체 export와 같은 ExportWorker 렌더/병합 helper를 재사용하되, 배경음악 믹스는 생략합니다.
    """

    project_file = Path(project_path)
    data = json.loads(project_file.read_text(encoding="utf-8"))
    project = Project.from_dict(data)
    scenes = project.scenes or []
    if scene_index < 0 or scene_index >= len(scenes) - 1:
        raise ValueError("다음 씬이 없어 전환 미리보기를 만들 수 없습니다.")

    ffmpeg_path = project.ffmpeg_path or find_default_ffmpeg()
    if not ffmpeg_path or not Path(ffmpeg_path).exists():
        raise FileNotFoundError("ffmpeg.exe 경로가 없습니다. main.py에서 ffmpeg 경로를 지정해 저장해 주세요.")

    project_dir = project_file.resolve().parent
    selected_scene = scenes[scene_index]
    next_scene = scenes[scene_index + 1]
    selected_scene.video_path = str((project_dir / selected_scene.video_path).resolve()) if selected_scene.video_path and not Path(selected_scene.video_path).is_absolute() else selected_scene.video_path
    next_scene.video_path = str((project_dir / next_scene.video_path).resolve()) if next_scene.video_path and not Path(next_scene.video_path).is_absolute() else next_scene.video_path
    if project.logo_path and not Path(project.logo_path).is_absolute():
        project.logo_path = str((project_dir / project.logo_path).resolve())

    for scene in (selected_scene, next_scene):
        if not scene.video_path or not Path(scene.video_path).exists():
            raise FileNotFoundError(f"전환 미리보기용 클립 파일을 찾을 수 없습니다: {scene.video_path}")
        if not scene.end_time.strip():
            raise ValueError(f"전환 미리보기를 만들려면 종료 시간이 필요합니다: {scene.scene_name or scene.video_path}")

    preview_scenes_and_durations = [
        _preview_scene_slice(selected_scene, use_tail=True),
        _preview_scene_slice(next_scene, use_tail=False),
    ]
    preview_scenes = [item[0] for item in preview_scenes_and_durations]
    preview_durations = [item[1] for item in preview_scenes_and_durations]
    transition_duration = project.transition_duration
    if project.transition_mode in (TRANSITION_CROSSFADE, TRANSITION_FADE_BLACK):
        shortest = min(preview_durations)
        if transition_duration <= 0 or transition_duration >= shortest:
            raise ValueError(
                "전환 시간이 미리보기 구간보다 깁니다. 전환 시간을 2초보다 짧게 조정해 주세요."
            )

    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="marineglory_transition_preview_") as temp_dir:
        temp_path = Path(temp_dir)
        worker = ExportWorker(
            ffmpeg_path=ffmpeg_path,
            scenes=preview_scenes,
            logo_path=project.logo_path,
            music_path="",
            output_path=str(output_file),
            transition_mode=project.transition_mode,
            transition_duration=transition_duration,
            fade_in_enabled=False,
            fade_out_enabled=False,
            edge_fade_duration=project.edge_fade_duration,
            subtitle_enabled=project.subtitle_enabled,
        )
        if log_callback is not None:
            worker.log.connect(log_callback)
        clips = [
            worker._render_scene(scene, index, temp_path, duration)
            for index, (scene, duration) in enumerate(zip(preview_scenes, preview_durations), start=1)
        ]
        worker._merge_rendered_clips(clips, preview_durations, output_file, temp_path)

    return output_file


class ClipExportWorker(QObject):
    """원본 영상의 지정 구간만 잘라 개별 MP4 클립으로 저장하는 작업자입니다."""

    log = Signal(str)
    progress = Signal(int, str)
    finished = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        ffmpeg_path: str,
        scenes: list[Scene],
        output_dir: str,
        start_index: int = 1,
        subtitle_mask_mode: str = SUBTITLE_MASK_NONE,
        subtitle_mask_height: int = 120,
        subtitle_mask_y: int | None = None,
    ) -> None:
        super().__init__()
        self.ffmpeg_path = ffmpeg_path
        self.scenes = scenes
        self.output_dir = output_dir
        self.start_index = start_index
        self.subtitle_mask_mode = subtitle_mask_mode
        self.subtitle_mask_height = subtitle_mask_height
        self.subtitle_mask_y = subtitle_mask_y

    def run(self) -> None:
        """검증 후 선택된 장면들을 순서대로 개별 MP4 파일로 저장합니다."""

        try:
            self._validate()
            output_path = Path(self.output_dir)
            output_path.mkdir(parents=True, exist_ok=True)

            total_count = len(self.scenes)
            for offset, scene in enumerate(self.scenes):
                clip_number = self.start_index + offset
                label = scene.scene_name or Path(scene.video_path).stem or "scene"
                safe_name = sanitize_filename_part(label)
                clip_path = output_path / f"{clip_number:02d}_{safe_name}.mp4"

                start_seconds = parse_time_to_seconds(scene.start_time.strip() or "0")
                end_seconds = parse_time_to_seconds(scene.end_time.strip())
                duration_seconds = end_seconds - start_seconds

                message = f"[{offset + 1}/{total_count}] 클립 저장 중: {clip_path}"
                self.progress.emit(int((offset / total_count) * 100), message)
                self.log.emit(message)

                if self.subtitle_mask_mode == SUBTITLE_MASK_NONE:
                    self._cut_clip_fast_copy(
                        scene.video_path,
                        clip_path,
                        start_seconds,
                        duration_seconds,
                    )
                else:
                    self._cut_clip_with_subtitle_mask(
                        scene.video_path,
                        clip_path,
                        start_seconds,
                        duration_seconds,
                    )
                self.log.emit(f"저장 완료: {clip_path}")

            self.progress.emit(100, "클립 저장 완료")
            self.finished.emit(str(output_path))
        except Exception as exc:
            self.failed.emit(str(exc))

    def _validate(self) -> None:
        """ffmpeg, 입력 영상, 시간값, 출력 폴더 조건을 미리 확인합니다."""

        if not self.ffmpeg_path or not Path(self.ffmpeg_path).exists():
            raise FileNotFoundError("ffmpeg.exe 경로가 없습니다. [ffmpeg 선택]으로 지정해 주세요.")
        if not self.scenes:
            raise ValueError("저장할 장면이 없습니다.")
        if self.subtitle_mask_mode not in SUBTITLE_MASK_MODES:
            raise ValueError("기존 자막 처리 방식이 올바르지 않습니다.")
        if not 30 <= self.subtitle_mask_height <= 400:
            raise ValueError("자막 영역 높이는 30~400px 범위로 입력해 주세요.")
        if self.subtitle_mask_y is not None and self.subtitle_mask_y < 0:
            raise ValueError("자막 영역 시작 Y 위치는 0 이상의 숫자로 입력해 주세요.")

        for index, scene in enumerate(self.scenes, start=1):
            label = scene.scene_name or Path(scene.video_path).name or f"{index}번 장면"
            if not scene.video_path or not Path(scene.video_path).exists():
                raise FileNotFoundError(f"{index}번 장면 영상 파일을 찾을 수 없습니다: {scene.video_path}")
            if not scene.end_time.strip():
                raise ValueError(f"{index}번 장면 종료 시간이 비어 있습니다: {label}")
            try:
                start_seconds = parse_time_to_seconds(scene.start_time.strip() or "0")
                end_seconds = parse_time_to_seconds(scene.end_time.strip())
            except ValueError as exc:
                raise ValueError(f"{index}번 장면 시간 형식이 올바르지 않습니다: {label}") from exc
            if start_seconds < 0 or end_seconds <= start_seconds:
                raise ValueError(f"{index}번 장면 종료 시간은 시작 시간보다 커야 합니다: {label}")

    def _cut_clip_fast_copy(
        self,
        source_path: str,
        output_path: Path,
        start_seconds: float,
        duration_seconds: float,
    ) -> None:
        """
        빠른 컷 저장 방식입니다.

        -c copy는 재인코딩 없이 스트림을 복사하므로 빠르고 화질 손실이 거의 없습니다.
        다만 원본 영상의 키프레임 위치에 맞춰 잘릴 수 있어 시작/끝 지점이 조금 어긋날 수 있습니다.
        정확한 컷이 필요하면 아래 _cut_clip_precise_reencode() 방식으로 전환하면 됩니다.
        """

        command = [
            self.ffmpeg_path,
            "-y",
            "-ss",
            format_seconds_for_ffmpeg(start_seconds),
            "-i",
            source_path,
            "-t",
            format_seconds_for_ffmpeg(duration_seconds),
            "-c",
            "copy",
            str(output_path),
        ]
        run_process(command, self.log)

    def _cut_clip_precise_reencode(
        self,
        source_path: str,
        output_path: Path,
        start_seconds: float,
        duration_seconds: float,
    ) -> None:
        """정확한 컷이 필요할 때 사용할 재인코딩 방식입니다. 현재 UI 기본값은 빠른 컷입니다."""

        command = [
            self.ffmpeg_path,
            "-y",
            "-ss",
            format_seconds_for_ffmpeg(start_seconds),
            "-i",
            source_path,
            "-t",
            format_seconds_for_ffmpeg(duration_seconds),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(output_path),
        ]
        run_process(command, self.log)

    def _cut_clip_with_subtitle_mask(
        self,
        source_path: str,
        output_path: Path,
        start_seconds: float,
        duration_seconds: float,
    ) -> None:
        """
        기존 자막 가리기 필터를 적용해 클립을 저장합니다.

        필터가 들어가면 비디오 스트림 복사(-c copy)를 사용할 수 없으므로 H.264로 재인코딩합니다.
        오디오는 원본이 없을 수도 있어 0:a? optional map을 사용하고, 있으면 AAC로 맞춥니다.
        """

        filter_kind, filter_text = apply_subtitle_mask_filter(
            self.subtitle_mask_mode,
            self.subtitle_mask_height,
            self.subtitle_mask_y,
        ) or ("", "")

        command = [
            self.ffmpeg_path,
            "-y",
            "-ss",
            format_seconds_for_ffmpeg(start_seconds),
            "-i",
            source_path,
            "-t",
            format_seconds_for_ffmpeg(duration_seconds),
        ]

        if filter_kind == "filter_complex":
            command.extend(["-filter_complex", filter_text, "-map", "[v]", "-map", "0:a?"])
        else:
            command.extend(["-vf", filter_text, "-map", "0:v:0", "-map", "0:a?"])

        command.extend(
            [
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                str(output_path),
            ]
        )
        self.log.emit(f"기존 자막 처리 적용: {self.subtitle_mask_mode}")
        run_process(command, self.log)


class MainWindow(QMainWindow):
    """MarineGlory Video Maker 메인 GUI입니다."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 980)

        self.project_path: Path | None = None
        self.ffmpeg_edit = QLineEdit(find_default_ffmpeg())
        self.logo_edit = QLineEdit()
        self.music_edit = QLineEdit()
        self.output_edit = QLineEdit(str(Path.cwd() / "marineglory_promo.mp4"))
        self.clip_output_edit = QLineEdit(str(Path.cwd() / "clips"))
        self.subtitle_mask_mode_combo = QComboBox()
        self.subtitle_mask_height_spin = QSpinBox()
        self.subtitle_mask_y_edit = QLineEdit()
        self.transition_mode_combo = QComboBox()
        self.transition_duration_spin = QDoubleSpinBox()
        self.fade_in_checkbox = QCheckBox("첫 장면 페이드 인")
        self.fade_out_checkbox = QCheckBox("마지막 장면 페이드 아웃")
        self.edge_fade_duration_spin = QDoubleSpinBox()
        self.subtitle_enabled_checkbox = QCheckBox("장면 자막 넣기")
        self.subtitle_enabled_checkbox.setChecked(True)
        self.scene_duration_spin = QDoubleSpinBox()
        self.table = QTableWidget(0, 8)
        self._updating_scene_duration_spin = False
        self._updating_scene_duration_from_range = False
        self.log_edit = QPlainTextEdit()
        self.progress_bar = QProgressBar()
        self.export_selected_clip_button = QPushButton("선택 구간 클립 저장")
        self.export_all_clips_button = QPushButton("전체 구간 클립 일괄 저장")
        self.export_button = QPushButton("최종 홍보영상 만들기")

        # 영상 미리보기 상태: QtMultimedia는 ffmpeg 자르기와 별개로 동작하므로
        # 코덱 문제로 재생에 실패해도 기존 클립 저장 기능은 계속 사용할 수 있습니다.
        self.preview_video_widget = QVideoWidget()
        self.preview_frame_label = QLabel()
        self.preview_stack = QStackedWidget()
        self.preview_player = QMediaPlayer(self)
        self.preview_audio_output = QAudioOutput(self)
        self.preview_slider = QSlider(Qt.Horizontal)
        self.preview_time_label = QLabel("00:00:00 / 00:00:00")
        self.preview_slider_dragging = False
        self.preview_mode = "frame"
        self.current_preview_seconds = 0.0
        self.preview_total_seconds = 0.0
        self.preview_current_path = ""
        self.preview_original_path = ""
        self.preview_frame_cache_dir = Path.cwd() / "preview-cache"
        self.preview_stop_at_ms: int | None = None
        self.preview_duration_check_token = 0
        self.preview_frame_timer = QTimer(self)
        self.duration_cache: dict[str, float] = {}
        self._refreshing_scene_table = False
        self._selection_event_count = 0
        self._last_selection_row: int | None = None
        self.scene_selection_timer = QTimer(self)
        self.scene_selection_timer.setSingleShot(True)
        self.scene_selection_timer.setInterval(150)

        self.worker_thread: QThread | None = None
        self.worker: ExportWorker | None = None
        self.clip_worker_thread: QThread | None = None
        self.clip_worker: ClipExportWorker | None = None

        self._build_ui()
        self._load_default_template()
        self._warn_if_ffmpeg_missing()

    def _build_ui(self) -> None:
        """버튼, 입력창, 표, 로그 영역을 배치합니다."""

        root = QWidget()
        main_layout = QVBoxLayout(root)
        main_layout.setContentsMargins(10, 10, 10, 10)
        self.setCentralWidget(root)

        # Main workspace: left controls and right preview stay resizable through a splitter.
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        main_layout.addWidget(splitter)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 6, 0)
        left_layout.setSpacing(8)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(6, 0, 0, 0)
        right_layout.setSpacing(8)

        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setSizes([580, 700])

        # Common settings: ffmpeg path used by all export and clipping jobs.
        path_box = QGroupBox("공통 설정")
        path_layout = QGridLayout(path_box)
        left_layout.addWidget(path_box)

        ffmpeg_button = QPushButton("ffmpeg 선택")
        path_layout.addWidget(QLabel("ffmpeg.exe"), 0, 0)
        path_layout.addWidget(self.ffmpeg_edit, 0, 1)
        path_layout.addWidget(ffmpeg_button, 0, 2)

        # Clip tools: import originals and save selected/all scene ranges as separate clips.
        clip_box = QGroupBox("1. 원본 영상 자르기 / 클립 만들기")
        clip_layout = QGridLayout(clip_box)
        left_layout.addWidget(clip_box)

        clip_output_button = QPushButton("잘라낸 클립 저장 폴더 선택")
        add_video_button = QPushButton("원본 영상 추가")
        self.subtitle_mask_mode_combo.addItems(SUBTITLE_MASK_MODES)
        self.subtitle_mask_height_spin.setRange(30, 400)
        self.subtitle_mask_height_spin.setValue(120)
        self.subtitle_mask_height_spin.setSuffix(" px")
        self.subtitle_mask_y_edit.setPlaceholderText("비우면 하단 자동")
        clip_layout.addWidget(add_video_button, 0, 0, 1, 3)
        clip_layout.addWidget(QLabel("클립 저장 폴더"), 1, 0)
        clip_layout.addWidget(self.clip_output_edit, 1, 1)
        clip_layout.addWidget(clip_output_button, 1, 2)
        clip_layout.addWidget(QLabel("기존 자막 처리 방식"), 2, 0)
        clip_layout.addWidget(self.subtitle_mask_mode_combo, 2, 1, 1, 2)
        clip_layout.addWidget(QLabel("자막 영역 높이"), 3, 0)
        clip_layout.addWidget(self.subtitle_mask_height_spin, 3, 1, 1, 2)
        clip_layout.addWidget(QLabel("자막 영역 시작 Y"), 4, 0)
        clip_layout.addWidget(self.subtitle_mask_y_edit, 4, 1, 1, 2)
        clip_layout.addWidget(self.export_selected_clip_button, 5, 0, 1, 2)
        clip_layout.addWidget(self.export_all_clips_button, 5, 2)

        # Scene table: give the list the remaining vertical space on the left.
        scene_list_box = QGroupBox("2. 장면 목록")
        scene_list_layout = QVBoxLayout(scene_list_box)
        left_layout.addWidget(scene_list_box, stretch=1)
        self._setup_table()
        self.table.setMinimumHeight(320)
        scene_list_layout.addWidget(self.table)

        # Scene management: reorder/delete scenes and restore the salt unloading template.
        scene_box = QGroupBox("3. 장면 목록 정리")
        scene_layout = QGridLayout(scene_box)
        add_scene_button = QPushButton("장면 추가")
        copy_button = QPushButton("장면 복사")
        self.scene_duration_spin.setRange(SCENE_MIN_DURATION_SECONDS, SCENE_MAX_DURATION_SECONDS)
        self.scene_duration_spin.setSingleStep(1.0)
        self.scene_duration_spin.setDecimals(1)
        self.scene_duration_spin.setValue(SCENE_DEFAULT_DURATION_SECONDS)
        self.scene_duration_spin.setSuffix(" 초")
        left_layout.addWidget(scene_box)

        remove_button = QPushButton("선택 장면 삭제")
        up_button = QPushButton("위로 이동")
        down_button = QPushButton("아래로 이동")
        template_button = QPushButton("소금하역 템플릿 불러오기")
        scene_layout.addWidget(remove_button, 0, 0)
        scene_layout.addWidget(up_button, 0, 1)
        scene_layout.addWidget(down_button, 0, 2)
        scene_layout.addWidget(template_button, 1, 0, 1, 3)
        scene_layout.addWidget(QLabel("선택 장면 재생시간"), 2, 0)
        scene_layout.addWidget(self.scene_duration_spin, 2, 1, 1, 2)
        scene_layout.addWidget(add_scene_button, 3, 0)
        scene_layout.addWidget(copy_button, 3, 1, 1, 2)

        # Project management: save and reload the current JSON project state.
        project_box = QGroupBox("4. 프로젝트 관리")
        project_layout = QGridLayout(project_box)
        left_layout.addWidget(project_box)

        save_button = QPushButton("프로젝트 저장")
        save_as_button = QPushButton("다른 이름으로 저장")
        load_button = QPushButton("프로젝트 불러오기")
        open_scene_editor_button = QPushButton("씬 편집 화면 열기")
        project_layout.addWidget(save_button, 0, 0)
        project_layout.addWidget(save_as_button, 0, 1)
        project_layout.addWidget(load_button, 1, 0, 1, 2)
        project_layout.addWidget(open_scene_editor_button, 2, 0, 1, 2)

        # Preview tools: the video area grows with the window and key range buttons stay visible.
        preview_box = QGroupBox("원본 클립 미리보기 / 구간 지정")
        preview_layout = QGridLayout(preview_box)
        right_layout.addWidget(preview_box, stretch=4)

        self.preview_mode_label = QLabel("선택한 원본 클립의 구간을 확인합니다.")
        preview_play_button = QPushButton("재생")
        preview_pause_button = QPushButton("일시정지")
        preview_stop_button = QPushButton("정지")
        preview_back_button = QPushButton("5초 뒤로")
        preview_forward_button = QPushButton("5초 앞으로")
        preview_set_start_button = QPushButton("현재 위치를 시작 시간으로")
        preview_set_end_button = QPushButton("현재 위치를 종료 시간으로")
        preview_range_button = QPushButton("선택 구간 미리보기")
        preview_icon_size = QSize(18, 18)
        preview_control_buttons = (
            (preview_play_button, QStyle.StandardPixmap.SP_MediaPlay),
            (preview_pause_button, QStyle.StandardPixmap.SP_MediaPause),
            (preview_stop_button, QStyle.StandardPixmap.SP_MediaStop),
            (preview_back_button, QStyle.StandardPixmap.SP_MediaSeekBackward),
            (preview_forward_button, QStyle.StandardPixmap.SP_MediaSeekForward),
        )
        for button, standard_icon in preview_control_buttons:
            button.setIcon(self.style().standardIcon(standard_icon))
            button.setIconSize(preview_icon_size)
            button.setMinimumSize(112, 36)
            button.setMaximumHeight(38)
        preview_action_buttons = (
            (preview_set_start_button, QStyle.StandardPixmap.SP_MediaSkipBackward),
            (preview_set_end_button, QStyle.StandardPixmap.SP_MediaSkipForward),
            (preview_range_button, QStyle.StandardPixmap.SP_DialogApplyButton),
        )
        for button, standard_icon in preview_action_buttons:
            button.setIcon(self.style().standardIcon(standard_icon))
            button.setIconSize(preview_icon_size)
            button.setMinimumHeight(38)
            button.setMaximumHeight(42)
        self.preview_stack.setMinimumHeight(320)
        self.preview_stack.setMaximumHeight(520)
        self.preview_stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.preview_video_widget.setMinimumHeight(320)
        self.preview_video_widget.setMaximumHeight(520)
        self.preview_video_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.preview_frame_label.setMinimumHeight(320)
        self.preview_frame_label.setMaximumHeight(520)
        self.preview_frame_label.setAlignment(Qt.AlignCenter)
        self.preview_frame_label.setStyleSheet("background: #050505; color: #d8d8d8;")
        self.preview_frame_label.setText("ffmpeg 프레임 미리보기")
        self.preview_frame_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.preview_stack.addWidget(self.preview_video_widget)
        self.preview_stack.addWidget(self.preview_frame_label)
        self.preview_stack.setCurrentIndex(1)
        self.preview_slider.setRange(0, 0)

        preview_layout.addWidget(self.preview_mode_label, 0, 0, 1, 6)
        preview_layout.addWidget(self.preview_stack, 1, 0, 1, 6)
        preview_layout.addWidget(preview_set_start_button, 2, 0, 1, 2)
        preview_layout.addWidget(preview_set_end_button, 2, 2, 1, 2)
        preview_layout.addWidget(preview_range_button, 2, 4, 1, 2)
        preview_layout.addWidget(self.preview_time_label, 3, 0, 1, 6)
        preview_layout.addWidget(self.preview_slider, 4, 0, 1, 6)
        preview_layout.addWidget(preview_play_button, 5, 0)
        preview_layout.addWidget(preview_pause_button, 5, 1)
        preview_layout.addWidget(preview_stop_button, 5, 2)
        preview_layout.addWidget(preview_back_button, 5, 3)
        preview_layout.addWidget(preview_forward_button, 5, 4)
        preview_layout.setRowStretch(1, 0)
        preview_layout.setRowStretch(2, 0)
        preview_layout.setRowStretch(3, 0)
        preview_layout.setRowStretch(4, 0)
        preview_layout.setRowStretch(5, 0)

        # Final export: logo, music, destination path, and render command.
        final_box = QGroupBox("최종 홍보영상 만들기")
        final_layout = QGridLayout(final_box)
        right_layout.addWidget(final_box)

        logo_button = QPushButton("회사 로고 PNG 선택")
        music_button = QPushButton("배경음악 MP3 선택")
        output_button = QPushButton("최종 영상 저장 위치 선택")
        preview_final_button = QPushButton("최종 결과 미리보기")
        # 회사 홍보영상에서는 기본적으로 크로스페이드 0.7초가 자연스럽습니다.
        self.transition_mode_combo.addItems(TRANSITION_MODES)
        self.transition_mode_combo.setCurrentText(TRANSITION_CROSSFADE)
        self.transition_duration_spin.setRange(0.2, 2.0)
        self.transition_duration_spin.setSingleStep(0.1)
        self.transition_duration_spin.setDecimals(1)
        self.transition_duration_spin.setValue(0.7)
        self.transition_duration_spin.setSuffix(" 초")
        self.fade_in_checkbox.setChecked(True)
        self.fade_out_checkbox.setChecked(True)
        self.edge_fade_duration_spin.setRange(0.2, 3.0)
        self.edge_fade_duration_spin.setSingleStep(0.1)
        self.edge_fade_duration_spin.setDecimals(1)
        self.edge_fade_duration_spin.setValue(1.0)
        self.edge_fade_duration_spin.setSuffix(" 초")
        final_layout.addWidget(QLabel("회사 로고 PNG"), 0, 0)
        final_layout.addWidget(self.logo_edit, 0, 1)
        final_layout.addWidget(logo_button, 0, 2)
        final_layout.addWidget(QLabel("배경음악 MP3"), 1, 0)
        final_layout.addWidget(self.music_edit, 1, 1)
        final_layout.addWidget(music_button, 1, 2)
        final_layout.addWidget(QLabel("최종 출력 MP4"), 2, 0)
        final_layout.addWidget(self.output_edit, 2, 1)
        final_layout.addWidget(output_button, 2, 2)
        final_layout.addWidget(QLabel("장면 전환 방식"), 3, 0)
        final_layout.addWidget(self.transition_mode_combo, 3, 1)
        final_layout.addWidget(QLabel("전환 시간"), 3, 2)
        final_layout.addWidget(self.transition_duration_spin, 3, 3)
        final_layout.addWidget(self.fade_in_checkbox, 4, 0, 1, 2)
        final_layout.addWidget(self.fade_out_checkbox, 4, 2, 1, 2)
        final_layout.addWidget(self.subtitle_enabled_checkbox, 5, 0, 1, 4)
        final_layout.addWidget(QLabel("시작/종료 페이드 시간"), 6, 0)
        final_layout.addWidget(self.edge_fade_duration_spin, 6, 1, 1, 3)
        final_layout.addWidget(preview_final_button, 7, 0, 1, 2)
        final_layout.addWidget(self.export_button, 7, 2, 1, 2)

        # Progress and logs: keep status visible without taking space from the preview.
        progress_box = QGroupBox("진행률 / 로그")
        progress_layout = QVBoxLayout(progress_box)
        right_layout.addWidget(progress_box, stretch=1)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("대기 중")
        progress_layout.addWidget(self.progress_bar)
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumBlockCount(3000)
        self.log_edit.setMaximumHeight(180)
        progress_layout.addWidget(self.log_edit)

        # Preview wiring: mirror QMediaPlayer state into the slider/time label.
        self.preview_player.setAudioOutput(self.preview_audio_output)
        self.preview_player.setVideoOutput(self.preview_video_widget)
        self.preview_audio_output.setVolume(0.7)
        self.preview_player.positionChanged.connect(self._preview_position_changed)
        self.preview_player.durationChanged.connect(self._preview_duration_changed)
        self.preview_player.errorOccurred.connect(self._preview_error_occurred)
        self.preview_slider.sliderPressed.connect(self._preview_slider_pressed)
        self.preview_slider.sliderReleased.connect(self._preview_slider_released)
        self.preview_slider.valueChanged.connect(self._preview_slider_value_changed)
        self.preview_frame_timer.setInterval(500)
        self.preview_frame_timer.timeout.connect(self._advance_frame_preview_playback)
        self.scene_selection_timer.timeout.connect(self.load_selected_row_preview)

        ffmpeg_button.clicked.connect(self.choose_ffmpeg)
        logo_button.clicked.connect(self.choose_logo)
        music_button.clicked.connect(self.choose_music)
        output_button.clicked.connect(self.choose_output)
        clip_output_button.clicked.connect(self.choose_clip_output_dir)
        add_video_button.clicked.connect(self.add_videos)
        add_scene_button.clicked.connect(self.add_scene)
        remove_button.clicked.connect(self.remove_selected_scene)
        copy_button.clicked.connect(self.copy_selected_scene)
        up_button.clicked.connect(lambda: self.move_selected_scene(-1))
        down_button.clicked.connect(lambda: self.move_selected_scene(1))
        self.scene_duration_spin.valueChanged.connect(self.update_selected_scene_duration)
        save_button.clicked.connect(self.save_project)
        save_as_button.clicked.connect(self.save_project_as)
        load_button.clicked.connect(lambda: self.load_project())
        open_scene_editor_button.clicked.connect(self.open_scene_editor)
        template_button.clicked.connect(self.reset_template)
        self.export_selected_clip_button.clicked.connect(self.export_selected_clip)
        self.export_all_clips_button.clicked.connect(self.export_all_clips)
        self.export_button.clicked.connect(self.export_video)
        preview_final_button.clicked.connect(self.preview_final_output)
        preview_play_button.clicked.connect(self._play_preview)
        preview_pause_button.clicked.connect(self._pause_preview)
        preview_stop_button.clicked.connect(self._stop_preview)
        preview_back_button.clicked.connect(lambda: self._seek_preview_by(-5000))
        preview_forward_button.clicked.connect(lambda: self._seek_preview_by(5000))
        preview_set_start_button.clicked.connect(self.set_preview_position_as_start_time)
        preview_set_end_button.clicked.connect(self.set_preview_position_as_end_time)
        preview_range_button.clicked.connect(self.preview_selected_range)

    def _setup_table(self) -> None:
        """장면 목록 표의 컬럼과 기본 편집 동작을 설정합니다."""

        headers = ["클립", "장면 제목", "시작", "종료", "재생시간(초)", "전환", "길이 보정", "자막"]
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.verticalHeader().setVisible(True)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(7, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._queue_selected_row_preview)
        self.table.itemChanged.connect(self._scene_table_item_changed)

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

        self._set_scenes([Scene(scene_name=scene_name, subtitle=subtitle) for scene_name, subtitle in DEFAULT_SCENES])

    def _append_scene(self, scene: Scene) -> None:
        """표 마지막에 장면 한 줄을 추가합니다."""

        row = self.table.rowCount()
        self.table.insertRow(row)
        values = [
            scene.video_path,
            scene.scene_name,
            scene.start_time,
            scene.end_time,
            f"{clamp_scene_duration(scene.duration):.1f}",
            normalize_scene_transition(scene.transition),
        ]
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column in (2, 3, 4):
                item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, column, item)
        mode_combo = QComboBox()
        mode_combo.addItems(list(DURATION_MODE_VALUES.keys()))
        mode_combo.setCurrentText(DURATION_MODE_LABELS[normalize_duration_mode(scene.duration_mode)])
        self.table.setCellWidget(row, 6, mode_combo)
        self.table.setItem(row, 7, QTableWidgetItem(scene.subtitle))

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
            duration=clamp_scene_duration(text(4)),
            transition=normalize_scene_transition(text(5)),
            duration_mode=self._duration_mode_from_row(row),
            subtitle=text(7),
        )

    def _duration_mode_from_row(self, row: int) -> str:
        widget = self.table.cellWidget(row, 6)
        if isinstance(widget, QComboBox):
            return normalize_duration_mode(widget.currentText())
        item = self.table.item(row, 6)
        return normalize_duration_mode(item.text() if item else "")

    def _set_table_text(self, row: int, column: int, value: str) -> None:
        """표 셀이 비어 있어도 안전하게 값을 넣기 위한 작은 헬퍼입니다."""

        item = self.table.item(row, column)
        if item is None:
            item = QTableWidgetItem()
            self.table.setItem(row, column, item)
        item.setText(value)
        if column in (2, 3, 4):
            item.setTextAlignment(Qt.AlignCenter)

    def _scene_table_item_changed(self, item: QTableWidgetItem) -> None:
        if self._refreshing_scene_table:
            return
        if self._updating_scene_duration_from_range:
            return
        if item.column() in (2, 3):
            self._update_scene_duration_from_range(item.row(), force=True)
            return
        if self._updating_scene_duration_spin or item.column() != 4:
            return
        duration = clamp_scene_duration(item.text())
        self._updating_scene_duration_spin = True
        try:
            item.setText(f"{duration:.1f}")
            item.setTextAlignment(Qt.AlignCenter)
            if item.row() == self.table.currentRow():
                self.scene_duration_spin.setValue(duration)
        finally:
            self._updating_scene_duration_spin = False

    def _update_scene_duration_from_range(self, row: int, force: bool = True) -> None:
        """시작/종료 시간 변경 시 재생시간(초)을 원본 구간 길이에 맞춥니다.

        향후에는 컬럼을 원본구간(초)과 최종재생시간(초)로 분리하면
        슬로우/정지/반복 의도가 더 명확해집니다.
        """

        if row < 0 or self._updating_scene_duration_from_range:
            return
        start_item = self.table.item(row, 2)
        end_item = self.table.item(row, 3)
        start_text = start_item.text().strip() if start_item is not None else "00:00:00"
        end_text = end_item.text().strip() if end_item is not None else ""
        if not end_text:
            return
        try:
            start_seconds = parse_time_to_seconds(start_text or "0")
            end_seconds = parse_time_to_seconds(end_text)
        except ValueError:
            return
        if end_seconds <= start_seconds:
            return

        source_duration = clamp_scene_duration(end_seconds - start_seconds)
        self._updating_scene_duration_from_range = True
        self._updating_scene_duration_spin = True
        try:
            self._set_table_text(row, 4, f"{source_duration:.1f}")
            if row == self.table.currentRow():
                self.scene_duration_spin.setValue(source_duration)
        finally:
            self._updating_scene_duration_spin = False
            self._updating_scene_duration_from_range = False
        self.log("시작/종료 시간 변경으로 재생시간을 원본 구간 길이에 맞췄습니다.")

    def _sync_duration_spin_to_selected_scene(self) -> None:
        row = self.table.currentRow()
        self.scene_duration_spin.setEnabled(row >= 0)
        if row < 0:
            return
        self._updating_scene_duration_spin = True
        try:
            self.scene_duration_spin.setValue(clamp_scene_duration(self._scene_from_row(row).duration))
        finally:
            self._updating_scene_duration_spin = False

    def update_selected_scene_duration(self, value: float) -> None:
        if self._updating_scene_duration_spin:
            return
        row = self.table.currentRow()
        if row >= 0:
            self._set_table_text(row, 4, f"{clamp_scene_duration(value):.1f}")

    def _queue_selected_row_preview(self) -> None:
        if self._refreshing_scene_table:
            return
        row = self.table.currentRow()
        self._selection_event_count += 1
        if row == self._last_selection_row:
            self.log(f"장면 선택 이벤트 중복 감지: row={row + 1 if row >= 0 else row}, count={self._selection_event_count}")
        self._last_selection_row = row
        self.scene_selection_timer.start()

    def load_selected_row_preview(self) -> None:
        """
        표에서 선택한 행의 원본 영상을 프로그램 안 미리보기 플레이어에 로드합니다.

        QMediaPlayer는 PC 코덱/드라이버 환경에 따라 일부 영상이 재생되지 않을 수 있습니다.
        이 경우에도 ffmpeg 기반 자르기 기능은 독립적으로 동작하므로 로그 안내만 남깁니다.
        """

        row = self.table.currentRow()
        if row < 0 or self._refreshing_scene_table:
            return
        started_at = time.perf_counter()
        self._sync_duration_spin_to_selected_scene()

        scene = self._scene_from_row(row)
        self.log(f"장면 선택 시작: row={row + 1}, title={scene.scene_name or '(제목 없음)'}")
        video_path = scene.video_path.strip()
        if not video_path:
            self.log(
                f"미리보기: {row + 1}번 장면에는 아직 영상이 배정되지 않았습니다. "
                "원본 영상 추가로 클립을 넣어 주세요."
            )
            return
        video_file = Path(video_path)
        exists = video_file.exists()
        self.log(f"미리보기 파일 확인: exists={exists}, path={video_path}")
        if not exists:
            self.log(f"미리보기: 영상 파일을 찾을 수 없습니다: {video_path}")
            return

        start_text = scene.start_time.strip() or "00:00:00"
        try:
            start_seconds = parse_time_to_seconds(start_text)
        except ValueError:
            start_seconds = 0.0

        if video_path != self.preview_original_path:
            self.preview_stop_at_ms = None
            self.preview_frame_timer.stop()
            self.preview_original_path = video_path
            self.current_preview_seconds = start_seconds
            self.preview_total_seconds = self._known_cached_duration_for(video_path) or 0.0
            self._load_video_into_preview(video_path, video_path, seek_seconds=start_seconds, auto_frame_fallback=False)
        else:
            self.current_preview_seconds = start_seconds
            start_ms = int(max(start_seconds, 0.0) * 1000)
            if self.preview_mode == "player":
                self.preview_player.setPosition(start_ms)
            else:
                self.preview_slider.setValue(start_ms)
                self._update_preview_time_label(start_ms, int(max(self.preview_total_seconds, 0.0) * 1000))

        end_text = scene.end_time.strip() or "(미입력)"
        self.log(f"선택 장면 구간: 시작 {start_text}, 종료 {end_text}")
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        self.log(f"장면 선택 완료: row={row + 1}, elapsed={elapsed_ms:.1f}ms")

    def _load_video_into_preview(
        self,
        preview_path: str,
        original_path: str,
        seek_seconds: float = 0.0,
        auto_frame_fallback: bool = True,
    ) -> None:
        """QMediaPlayer에 미리보기 파일을 로드하고 duration 0 상태를 별도로 확인합니다."""

        self.preview_mode = "player"
        self.preview_stack.setCurrentIndex(0)
        self.preview_current_path = preview_path
        self.preview_player.setSource(QUrl.fromLocalFile(preview_path))
        start_ms = int(max(seek_seconds, 0.0) * 1000)
        total_ms = int(max(self.preview_total_seconds, 0.0) * 1000)
        if total_ms > 0:
            self.preview_slider.setRange(0, total_ms)
        self.preview_slider.setValue(start_ms)
        self.preview_player.setPosition(start_ms)
        self.current_preview_seconds = max(seek_seconds, 0.0)
        if total_ms > 0:
            self._update_preview_time_label(start_ms, total_ms)
        else:
            self.preview_time_label.setText("00:00:00 / 00:00:00")
        if preview_path != original_path:
            self.preview_mode_label.setText("프록시 MP4 미리보기: 원본은 ffmpeg 렌더링에 사용됩니다.")
            self.log(f"미리보기 프록시 로드: {preview_path}")
        else:
            self.preview_mode_label.setText("선택한 원본 클립의 구간을 확인합니다.")
            self.log(f"미리보기 원본 로드: {preview_path}")
        self.preview_duration_check_token += 1
        if auto_frame_fallback:
            token = self.preview_duration_check_token
            QTimer.singleShot(1500, lambda: self._check_preview_duration_after_load(token, original_path))

    def _check_preview_duration_after_load(self, token: int, original_path: str) -> None:
        """setSource 뒤에도 길이가 0이면 QtMultimedia 코덱 문제 가능성을 안내합니다."""

        if token != self.preview_duration_check_token:
            return
        if self.preview_mode != "player":
            return
        if self.preview_player.duration() <= 0:
            self.log("내장 플레이어 미리보기 실패. ffmpeg 프레임 미리보기 모드로 전환합니다.")
            self._switch_to_frame_preview(original_path)

    def _switch_to_frame_preview(self, video_path: str) -> None:
        """QMediaPlayer 대신 ffmpeg로 추출한 정지 프레임을 표시하는 모드로 전환합니다."""

        if not video_path:
            return
        self.preview_player.pause()
        self.preview_frame_timer.stop()
        self.preview_mode = "frame"
        self.preview_stack.setCurrentIndex(1)
        self.preview_original_path = video_path
        self.preview_current_path = video_path
        if self.preview_total_seconds <= 0:
            self.preview_total_seconds = self._cached_duration_for(video_path) or 0.0
        total_ms = int(max(self.preview_total_seconds, 0.0) * 1000)
        self.preview_slider.setRange(0, total_ms)
        self.current_preview_seconds = max(0.0, min(self.current_preview_seconds, self.preview_total_seconds))
        self.preview_slider.setValue(int(self.current_preview_seconds * 1000))
        self._update_preview_time_label(int(self.current_preview_seconds * 1000), total_ms)
        self.preview_mode_label.setText("ffmpeg 프레임 미리보기: 슬라이더로 시점을 찾고 시작/종료 시간을 지정합니다.")
        self.log("ffmpeg 프레임 미리보기 모드로 전환했습니다.")
        self._show_preview_frame(video_path, self.current_preview_seconds)

    def render_preview_frame(self, video_path: str, seconds: float) -> Path:
        """ffmpeg로 현재 시점의 프레임 JPG를 추출합니다."""

        ffmpeg_path = self.ffmpeg_edit.text().strip()
        if not ffmpeg_path or not Path(ffmpeg_path).exists():
            raise FileNotFoundError("ffmpeg.exe 경로가 없습니다.")
        if not video_path or not Path(video_path).exists():
            raise FileNotFoundError(f"영상 파일을 찾을 수 없습니다: {video_path}")

        if self.preview_total_seconds > 0:
            safe_seconds = max(0.0, min(seconds, max(self.preview_total_seconds - 0.1, 0.0)))
        else:
            safe_seconds = max(seconds, 0.0)
        frame_ms = int(round(safe_seconds * 1000))
        self.preview_frame_cache_dir.mkdir(parents=True, exist_ok=True)
        frame_path = self.preview_frame_cache_dir / f"frame_preview_{frame_ms:010d}.jpg"
        command = [
            ffmpeg_path,
            "-y",
            "-ss",
            format_seconds_for_ffmpeg(safe_seconds),
            "-i",
            video_path,
            "-frames:v",
            "1",
            "-update",
            "1",
            "-vf",
            "scale=960:-2",
            str(frame_path),
        ]
        started_at = time.perf_counter()
        self.log(f"preview frame 추출 시작: {video_path}, seconds={safe_seconds:.3f}")
        try:
            process = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                timeout=10,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed_ms = (time.perf_counter() - started_at) * 1000
            self.log(f"preview frame 추출 시간 초과: elapsed={elapsed_ms:.1f}ms")
            raise RuntimeError("ffmpeg 프레임 추출 시간 초과") from exc
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        self.log(f"preview frame 추출 종료: elapsed={elapsed_ms:.1f}ms")
        if process.returncode != 0 or not frame_path.exists() or frame_path.stat().st_size <= 0:
            self.log(
                "ffmpeg 프레임 추출 실패: "
                f"returncode={process.returncode}, output={_short_process_output(process.stdout)}"
            )
            raise RuntimeError("ffmpeg 프레임 추출 실패")
        return frame_path

    def _show_preview_frame(self, video_path: str, seconds: float) -> None:
        """추출한 프레임을 QLabel에 비율 유지로 표시합니다."""

        try:
            frame_path = self.render_preview_frame(video_path, seconds)
        except Exception as exc:
            self.preview_frame_label.setText(f"프레임 미리보기 실패\n{exc}")
            return
        pixmap = QPixmap(str(frame_path))
        if pixmap.isNull():
            self.preview_frame_label.setText("프레임 이미지를 표시하지 못했습니다.")
            return
        scaled = pixmap.scaled(
            self.preview_frame_label.size(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self.preview_frame_label.setPixmap(scaled)

    def _create_preview_proxy(self, video_path: str) -> str:
        """QMediaPlayer가 읽기 쉬운 H.264/yuv420p 전체 프록시 MP4를 생성합니다."""

        # 프록시는 원본 전체 길이로 생성합니다. 여기에는 "-t 10" 같은 시간 제한을 넣지 않습니다.
        ffmpeg_path = self.ffmpeg_edit.text().strip()
        if not ffmpeg_path or not Path(ffmpeg_path).exists():
            self.log("미리보기 전체 프록시 생성 건너뜀: ffmpeg.exe 경로가 없습니다.")
            return ""

        cache_dir = self.preview_frame_cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)
        source_path = Path(video_path)
        safe_stem = sanitize_filename_part(source_path.stem, "video")
        source_hash = hashlib.sha1(str(source_path.resolve()).encode("utf-8", errors="ignore")).hexdigest()[:8]
        proxy_path = cache_dir / f"preview_proxy_{safe_stem}_{source_hash}.mp4"
        command = [
            ffmpeg_path,
            "-y",
            "-i",
            video_path,
            "-vf",
            "scale=1280:-2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-an",
            str(proxy_path),
        ]
        self.log(
            "미리보기 전체 프록시 생성 중... 긴 영상은 시간이 걸릴 수 있습니다: "
            + " ".join(f'"{part}"' if " " in part else part for part in command)
        )
        try:
            process = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            self.log("미리보기 전체 프록시 생성 시간 초과")
            return ""
        except Exception as exc:
            self.log(f"미리보기 전체 프록시 생성 실패: {exc}")
            return ""
        if process.returncode != 0 or not proxy_path.exists() or proxy_path.stat().st_size <= 0:
            self.log(
                "미리보기 전체 프록시 생성 실패: "
                f"returncode={process.returncode}, output={_short_process_output(process.stdout)}"
            )
            return ""
        self.log(f"미리보기 전체 프록시 생성 완료: {proxy_path}")
        return str(proxy_path)

    def _preview_position_changed(self, position_ms: int) -> None:
        """재생 위치가 바뀔 때 시간 표시와 슬라이더를 갱신하고 구간 미리보기를 종료합니다."""

        if self.preview_mode != "player":
            return
        self.current_preview_seconds = max(position_ms, 0) / 1000
        if not self.preview_slider_dragging:
            self.preview_slider.setValue(position_ms)
        duration_ms = int(max(self.preview_total_seconds * 1000, self.preview_player.duration(), 0))
        self._update_preview_time_label(position_ms, duration_ms)

        if self.preview_stop_at_ms is not None and position_ms >= self.preview_stop_at_ms:
            self.preview_player.pause()
            self.preview_stop_at_ms = None
            self.log("선택 구간 미리보기 종료")

    def _preview_duration_changed(self, duration_ms: int) -> None:
        """영상 길이가 확인되면 슬라이더 범위를 영상 전체 길이에 맞춥니다."""

        if self.preview_mode != "player":
            return
        if duration_ms > 0 and self.preview_total_seconds <= 0:
            self.preview_total_seconds = duration_ms / 1000
        range_ms = int(max(self.preview_total_seconds * 1000, duration_ms, 0))
        self.preview_slider.setRange(0, range_ms)
        self._update_preview_time_label(self.preview_player.position(), range_ms)

    def _preview_error_occurred(self, *args: Any) -> None:
        """미리보기 재생 실패를 로그에 남깁니다. ffmpeg 자르기 기능은 계속 사용할 수 있습니다."""

        if self.preview_mode == "frame":
            return
        error_message = self.preview_player.errorString().strip()
        if not error_message:
            error_message = "이 PC의 코덱/QtMultimedia 환경에서 미리보기를 재생하지 못했습니다."
        self.log(
            f"미리보기 오류: {error_message} "
            "미리보기 코덱 문제일 수 있으며, ffmpeg 렌더링은 별도로 가능합니다."
        )

        self.log("내장 플레이어 미리보기 실패. ffmpeg 프레임 미리보기 모드로 전환합니다.")
        self._switch_to_frame_preview(self.preview_original_path)

    def _preview_slider_pressed(self) -> None:
        """사용자가 슬라이더를 잡고 있는 동안 positionChanged 업데이트와 충돌하지 않게 표시합니다."""

        self.preview_slider_dragging = True

    def _preview_slider_released(self) -> None:
        """슬라이더에서 손을 떼면 플레이어 위치를 해당 지점으로 이동합니다."""

        self.preview_slider_dragging = False
        if self.preview_mode == "frame":
            self.current_preview_seconds = max(self.preview_slider.value(), 0) / 1000
            self._update_preview_time_label(self.preview_slider.value(), int(self.preview_total_seconds * 1000))
            self._show_preview_frame(self.preview_original_path, self.current_preview_seconds)
        else:
            self.preview_player.setPosition(self.preview_slider.value())

    def _preview_slider_value_changed(self, value: int) -> None:
        """드래그 중에는 시간 라벨을 먼저 갱신하고, 실제 이동은 release 시점에 수행합니다."""

        if self.preview_slider_dragging:
            if self.preview_mode == "frame":
                self.current_preview_seconds = max(value, 0) / 1000
                self._update_preview_time_label(value, int(self.preview_total_seconds * 1000))
            else:
                duration_ms = int(max(self.preview_total_seconds * 1000, self.preview_player.duration(), 0))
                self._update_preview_time_label(value, duration_ms)

    def _update_preview_time_label(self, position_ms: int, duration_ms: int) -> None:
        """현재 재생 위치와 전체 길이를 HH:MM:SS.mmm 형식으로 표시합니다."""

        current_text = format_milliseconds_for_display(position_ms)
        duration_text = format_milliseconds_for_display(duration_ms)
        self.preview_time_label.setText(f"{current_text} / {duration_text}")

    def get_current_preview_seconds(self) -> float:
        """현재 미리보기 위치를 player/frame 모드 공통 초 단위로 반환합니다."""

        if self.preview_mode == "player":
            return max(self.preview_player.position(), 0) / 1000
        return max(self.current_preview_seconds, 0.0)

    def _play_preview(self) -> None:
        """player 모드는 QMediaPlayer, frame 모드는 타이머 기반 프레임 진행으로 재생합니다."""

        if self.preview_mode == "frame":
            if self.preview_original_path:
                self.preview_frame_timer.start()
            return
        self.preview_player.play()

    def _pause_preview(self) -> None:
        if self.preview_mode == "frame":
            self.preview_frame_timer.stop()
        else:
            self.preview_player.pause()

    def _advance_frame_preview_playback(self) -> None:
        """frame 모드에서 0.5초씩 이동하며 프레임을 갱신합니다."""

        if self.preview_mode != "frame" or not self.preview_original_path:
            self.preview_frame_timer.stop()
            return
        limit = self.preview_stop_at_ms / 1000 if self.preview_stop_at_ms is not None else self.preview_total_seconds
        next_seconds = self.current_preview_seconds + 0.5
        if limit > 0 and next_seconds >= limit:
            next_seconds = limit
            self.preview_frame_timer.stop()
            self.preview_stop_at_ms = None
            self.log("선택 구간 프레임 미리보기 종료")
        self.current_preview_seconds = max(0.0, min(next_seconds, max(self.preview_total_seconds, next_seconds)))
        current_ms = int(self.current_preview_seconds * 1000)
        self.preview_slider.setValue(current_ms)
        self._update_preview_time_label(current_ms, int(self.preview_total_seconds * 1000))
        self._show_preview_frame(self.preview_original_path, self.current_preview_seconds)

    def _stop_preview(self) -> None:
        """미리보기 재생을 멈추고 구간 미리보기 종료 지점도 초기화합니다."""

        self.preview_stop_at_ms = None
        self.preview_frame_timer.stop()
        if self.preview_mode == "frame":
            self.current_preview_seconds = 0.0
            self.preview_slider.setValue(0)
            self._update_preview_time_label(0, int(self.preview_total_seconds * 1000))
            if self.preview_original_path:
                self._show_preview_frame(self.preview_original_path, 0.0)
        else:
            self.preview_player.stop()

    def _seek_preview_by(self, delta_ms: int) -> None:
        """Move the preview position while keeping it inside the loaded video range."""

        duration_ms = int(max(self.preview_total_seconds * 1000, self.preview_player.duration(), 0))
        current_ms = int(self.get_current_preview_seconds() * 1000)
        next_ms = max(0, min(current_ms + delta_ms, duration_ms))
        if self.preview_mode == "frame":
            self.current_preview_seconds = next_ms / 1000
            self.preview_slider.setValue(next_ms)
            if self.preview_original_path:
                self._show_preview_frame(self.preview_original_path, self.current_preview_seconds)
        else:
            self.preview_player.setPosition(next_ms)
        self._update_preview_time_label(next_ms, duration_ms)

    def set_preview_position_as_start_time(self) -> None:
        """현재 재생 위치를 선택 행의 시작 시간 컬럼에 입력합니다."""

        self._set_preview_position_to_time_column(2, "시작 시간")

    def set_preview_position_as_end_time(self) -> None:
        """현재 재생 위치를 선택 행의 종료 시간 컬럼에 입력합니다."""

        self._set_preview_position_to_time_column(3, "종료 시간")

    def _set_preview_position_to_time_column(self, column: int, label: str) -> None:
        """현재 미리보기 위치를 선택 행의 시간 컬럼에 기록합니다."""

        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "장면 선택 필요", "시간을 입력할 장면 행을 선택해 주세요.")
            return

        time_text = format_seconds_for_display(self.get_current_preview_seconds())
        signals_were_blocked = self.table.blockSignals(True)
        try:
            self._set_table_text(row, column, time_text)
        finally:
            self.table.blockSignals(signals_were_blocked)
        if column in (2, 3):
            self._update_scene_duration_from_range(row, force=True)
        self.log(f"{row + 1}번 장면 {label} 지정: {time_text}")

    def preview_selected_range(self) -> None:
        """선택 행의 시작~종료 시간만 플레이어에서 재생해 구간을 확인합니다."""

        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "장면 선택 필요", "미리보기할 장면 행을 선택해 주세요.")
            return

        scene = self._scene_from_row(row)
        if not scene.end_time.strip():
            QMessageBox.information(self, "종료 시간 필요", "선택 구간 미리보기를 위해 종료 시간을 지정해 주세요.")
            return

        try:
            start_seconds = parse_time_to_seconds(scene.start_time.strip() or "0")
            end_seconds = parse_time_to_seconds(scene.end_time.strip())
        except ValueError:
            QMessageBox.warning(self, "시간 형식 오류", "시작 시간 또는 종료 시간 형식이 올바르지 않습니다.")
            return

        if start_seconds < 0 or end_seconds <= start_seconds:
            QMessageBox.warning(self, "구간 확인 필요", "종료 시간은 시작 시간보다 커야 합니다.")
            return

        if scene.video_path.strip() and scene.video_path.strip() != self.preview_original_path:
            self.load_selected_row_preview()

        if self.preview_mode == "frame":
            self._open_frame_range_preview(scene.video_path.strip(), start_seconds, end_seconds)
            return

        self.preview_stop_at_ms = int(end_seconds * 1000)
        self.preview_player.setPosition(int(start_seconds * 1000))
        self.preview_player.play()
        self.log(
            "선택 구간 미리보기 시작: "
            f"{format_seconds_for_display(start_seconds)} ~ {format_seconds_for_display(end_seconds)}"
        )

    def _open_frame_range_preview(self, video_path: str, start_seconds: float, end_seconds: float) -> None:
        """frame 모드에서 선택 구간 MP4를 ffmpeg로 만든 뒤 외부 플레이어로 엽니다."""

        ffmpeg_path = self.ffmpeg_edit.text().strip()
        if not ffmpeg_path or not Path(ffmpeg_path).exists():
            QMessageBox.warning(self, "ffmpeg 필요", "선택 구간 미리보기를 만들려면 ffmpeg.exe 경로가 필요합니다.")
            return
        if not video_path or not Path(video_path).exists():
            QMessageBox.warning(self, "영상 없음", f"선택 구간 미리보기용 영상 파일을 찾을 수 없습니다:\n{video_path}")
            return
        self.preview_frame_cache_dir.mkdir(parents=True, exist_ok=True)
        output_path = self.preview_frame_cache_dir / "preview_range.mp4"
        duration_seconds = max(end_seconds - start_seconds, 0.001)
        command = [
            ffmpeg_path,
            "-y",
            "-ss",
            format_seconds_for_ffmpeg(start_seconds),
            "-i",
            video_path,
            "-t",
            format_seconds_for_ffmpeg(duration_seconds),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-an",
            str(output_path),
        ]
        self.log("frame 모드 선택 구간 미리보기 생성 시작: " + " ".join(f'"{part}"' if " " in part else part for part in command))
        try:
            process = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            self.log("frame 모드 선택 구간 미리보기 생성 시간 초과")
            QMessageBox.warning(self, "미리보기 생성 실패", "선택 구간 미리보기 MP4 생성 시간이 초과되었습니다.")
            return
        if process.returncode != 0 or not output_path.exists() or output_path.stat().st_size <= 0:
            self.log(
                "frame 모드 선택 구간 미리보기 생성 실패: "
                f"returncode={process.returncode}, output={_short_process_output(process.stdout)}"
            )
            QMessageBox.warning(self, "미리보기 생성 실패", "선택 구간 미리보기 MP4를 만들지 못했습니다. 로그를 확인해 주세요.")
            return
        self.log(f"frame 모드 선택 구간 미리보기 생성 완료: {output_path}")
        try:
            os.startfile(output_path)  # type: ignore[attr-defined]
        except Exception as exc:
            self.log(f"선택 구간 미리보기 열기 실패: {exc}")

    def _all_scenes(self) -> list[Scene]:
        """현재 표의 모든 장면을 순서대로 가져옵니다."""

        return [self._scene_from_row(row) for row in range(self.table.rowCount())]

    def _set_scenes(self, scenes: list[Scene]) -> None:
        """JSON 불러오기나 행 이동 후 표 전체를 다시 그립니다."""

        started_at = time.perf_counter()
        previous_row = self.table.currentRow()
        self._refreshing_scene_table = True
        signals_were_blocked = self.table.blockSignals(True)
        try:
            self.table.setRowCount(0)
            for scene in scenes:
                self._append_scene(scene)
            if scenes:
                restore_row = min(max(previous_row, 0), len(scenes) - 1)
                self.table.selectRow(restore_row)
            else:
                self.table.clearSelection()
        finally:
            self.table.blockSignals(signals_were_blocked)
            self._refreshing_scene_table = False
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        self.log(f"테이블 refresh 완료: rows={len(scenes)}, elapsed={elapsed_ms:.1f}ms")

    def log(self, message: str) -> None:
        """GUI 로그 창에 메시지를 누적합니다."""

        self.log_edit.appendPlainText(message)
        self.log_edit.verticalScrollBar().setValue(self.log_edit.verticalScrollBar().maximum())

    def _cached_duration_for(self, video_path: str, log_cache_hit: bool = True) -> float | None:
        """Return a cached ffprobe/ffmpeg duration for one source video."""

        if not video_path:
            return None
        key = str(Path(video_path).resolve())
        if key in self.duration_cache:
            if log_cache_hit:
                self.log(f"영상 길이 캐시 사용: {Path(video_path).name} = {self.duration_cache[key]:.3f}초")
            return self.duration_cache[key]
        started_at = time.perf_counter()
        self.log(f"ffprobe 호출 시작: {video_path}")
        duration = read_video_duration_seconds(self.ffmpeg_edit.text().strip(), video_path, self.log)
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        self.log(f"ffprobe 호출 종료: {video_path}, elapsed={elapsed_ms:.1f}ms")
        if duration is not None:
            self.duration_cache[key] = duration
        return duration

    def _known_cached_duration_for(self, video_path: str) -> float | None:
        if not video_path:
            return None
        return self.duration_cache.get(str(Path(video_path).resolve()))

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

    def choose_clip_output_dir(self) -> None:
        """개별 클립 MP4를 저장할 폴더를 선택합니다."""

        folder_path = QFileDialog.getExistingDirectory(
            self,
            "잘라낸 클립 저장 폴더 선택",
            self.clip_output_edit.text().strip() or str(Path.cwd()),
        )
        if folder_path:
            self.clip_output_edit.setText(folder_path)

    def add_videos(self) -> None:
        """여러 원본 영상 파일을 추가합니다. 빈 템플릿 행이 있으면 먼저 채웁니다."""

        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "원본 영상 여러 개 추가",
            str(Path.cwd()),
            "영상 파일 (*.mp4 *.mov *.m4v);;MP4 파일 (*.mp4);;MOV 파일 (*.mov *.m4v);;모든 파일 (*.*)",
        )
        if not file_paths:
            return

        for file_path in file_paths:
            exists = Path(file_path).exists()
            self.log(f"원본 영상 파일 확인: exists={exists}, path={file_path}")
            if not exists:
                self.log(f"원본 영상 추가 실패: 파일을 찾을 수 없습니다: {file_path}")
                continue
            if not is_supported_video_file(file_path):
                self.log(f"지원하지 않는 영상 형식: {Path(file_path).name}")
                continue

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
                        duration=clamp_scene_duration(parse_time_to_seconds(duration_text)) if duration_text else SCENE_DEFAULT_DURATION_SECONDS,
                        subtitle=subtitle,
                    )
                )
            else:
                self._set_table_text(target_row, 0, file_path)
                end_item = self.table.item(target_row, 3)
                end_text = end_item.text().strip() if end_item is not None else ""
                if duration_text and not end_text:
                    self._set_table_text(target_row, 3, duration_text)
                    self._update_scene_duration_from_range(target_row, force=True)

    def _duration_text_for_video(self, file_path: str) -> str:
        """영상 길이를 읽어 표에 넣을 종료 시간 문자열로 변환합니다."""

        duration = self._cached_duration_for(file_path)
        if duration is None:
            self.log(f"파일은 존재하지만 ffmpeg가 길이를 읽지 못했습니다: {file_path}")
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
                duration = self._cached_duration_for(scene.video_path)
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

    def add_scene(self) -> None:
        self._append_scene(
            Scene(
                scene_name="새 장면",
                duration=SCENE_DEFAULT_DURATION_SECONDS,
                transition=SCENE_DEFAULT_TRANSITION,
            )
        )
        self.table.selectRow(self.table.rowCount() - 1)

    def remove_selected_scene(self) -> None:
        selected = self.table.currentRow()
        if selected < 0:
            return
        answer = QMessageBox.question(self, "장면 삭제", "선택한 장면을 삭제할까요?")
        if answer != QMessageBox.Yes:
            return
        self.table.removeRow(selected)
        if self.table.rowCount():
            self.table.selectRow(min(selected, self.table.rowCount() - 1))
        else:
            self._sync_duration_spin_to_selected_scene()

    def copy_selected_scene(self) -> None:
        selected = self.table.currentRow()
        if selected < 0:
            return
        scenes = self._all_scenes()
        scene = self._scene_from_row(selected)
        scenes.insert(
            selected + 1,
            Scene(
                video_path=scene.video_path,
                scene_name=scene.scene_name,
                start_time=scene.start_time,
                end_time=scene.end_time,
                duration=scene.duration,
                transition=scene.transition,
                duration_mode=scene.duration_mode,
                subtitle=scene.subtitle,
            ),
        )
        self._set_scenes(scenes)
        self.table.selectRow(selected + 1)

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

    def _project_from_ui(self) -> Project:
        """현재 GUI 입력값을 프로젝트 저장 모델로 변환합니다."""

        return Project(
            ffmpeg_path=self.ffmpeg_edit.text().strip(),
            logo_path=self.logo_edit.text().strip(),
            music_path=self.music_edit.text().strip(),
            output_path=self.output_edit.text().strip(),
            scenes=self._all_scenes(),
            transition_mode=self.transition_mode_combo.currentText(),
            transition_duration=self.transition_duration_spin.value(),
            fade_in_enabled=self.fade_in_checkbox.isChecked(),
            fade_out_enabled=self.fade_out_checkbox.isChecked(),
            edge_fade_duration=self.edge_fade_duration_spin.value(),
            subtitle_enabled=self.subtitle_enabled_checkbox.isChecked(),
        )

    def save_project(self) -> None:
        if self.project_path is None:
            self.save_project_as()
            return

        self._write_project(self.project_path)

    def save_project_as(self) -> None:
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "프로젝트 JSON 저장",
            str(self.project_path or Path.cwd() / "marineglory_project.json"),
            "JSON 파일 (*.json)",
        )
        if not file_path:
            return
        path = Path(file_path)
        if path.suffix.lower() != ".json":
            path = path.with_suffix(".json")
        self.project_path = path
        self._write_project(path)

    def _write_project(self, path: Path) -> None:
        project = self._project_from_ui()
        path.write_text(
            json.dumps(project.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.project_path = path
        self.log(f"프로젝트 저장 완료: {path}")

    def open_scene_editor(self) -> None:
        """현재 프로젝트를 저장한 뒤 씬 편집 전용 UI를 같은 JSON 경로로 엽니다."""

        if self.project_path is None:
            self.save_project_as()
            if self.project_path is None:
                return
        else:
            self._write_project(self.project_path)

        editor_path = application_dir() / "video_editor_ui.py"
        if not editor_path.exists():
            QMessageBox.critical(
                self,
                "씬 편집 화면 열기 실패",
                f"video_editor_ui.py를 찾을 수 없습니다.\n{editor_path}",
            )
            return

        try:
            subprocess.Popen(
                [sys.executable, str(editor_path), str(self.project_path), "--opened-from-main"],
                cwd=str(application_dir()),
            )
            self.log(f"씬 편집 화면 열기: {self.project_path}")
        except Exception as exc:
            QMessageBox.critical(self, "씬 편집 화면 열기 실패", str(exc))

    def load_project(self, file_path: str | Path | None = None) -> None:
        if file_path is None:
            selected_path, _ = QFileDialog.getOpenFileName(
                self,
                "프로젝트 JSON 불러오기",
                str(Path.cwd()),
                "JSON 파일 (*.json);;모든 파일 (*.*)",
            )
            if not selected_path:
                return
            path = Path(selected_path)
        else:
            path = Path(file_path)

        data = json.loads(path.read_text(encoding="utf-8"))
        project = Project.from_dict(data)
        self.ffmpeg_edit.setText(project.ffmpeg_path)
        self.logo_edit.setText(project.logo_path)
        self.music_edit.setText(project.music_path)
        if project.output_path:
            self.output_edit.setText(project.output_path)
        self.transition_mode_combo.setCurrentText(project.transition_mode)
        self.transition_duration_spin.setValue(project.transition_duration)
        self.fade_in_checkbox.setChecked(project.fade_in_enabled)
        self.fade_out_checkbox.setChecked(project.fade_out_enabled)
        self.edge_fade_duration_spin.setValue(project.edge_fade_duration)
        self.subtitle_enabled_checkbox.setChecked(project.subtitle_enabled)
        self._set_scenes(project.scenes or [])
        self.project_path = path
        self.log(f"프로젝트 불러오기 완료: {path}")

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
        scene_durations: list[float] = []
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
            duration = clamp_scene_duration(scene.duration)
            scene_durations.append(duration)
            total_duration += duration

        transition_mode = self.transition_mode_combo.currentText()
        transition_duration = self.transition_duration_spin.value()
        fade_in_enabled = self.fade_in_checkbox.isChecked()
        fade_out_enabled = self.fade_out_checkbox.isChecked()
        edge_fade_duration = self.edge_fade_duration_spin.value()
        if transition_mode not in TRANSITION_MODES:
            errors.append("장면 전환 방식이 올바르지 않습니다.")
        if len(scene_durations) > 1 and transition_mode in (TRANSITION_CROSSFADE, TRANSITION_FADE_BLACK):
            shortest = min(scene_durations)
            if transition_duration >= shortest / 2:
                errors.append(
                    "전환 시간이 너무 깁니다. "
                    f"가장 짧은 장면({shortest:.1f}초)의 절반보다 짧게 설정해 주세요."
                )
        elif len(scene_durations) == 1 and transition_mode != TRANSITION_NONE:
            warnings.append("장면이 1개뿐이어서 클립 사이 전환 효과는 적용되지 않습니다.")
        if scene_durations:
            if fade_in_enabled and edge_fade_duration >= scene_durations[0]:
                errors.append("첫 장면 페이드 시간이 첫 장면 길이보다 짧아야 합니다.")
            if fade_out_enabled and edge_fade_duration >= scene_durations[-1]:
                errors.append("마지막 장면 페이드 시간이 마지막 장면 길이보다 짧아야 합니다.")
            if len(scene_durations) > 1 and transition_mode == TRANSITION_FADE_BLACK:
                if fade_in_enabled and edge_fade_duration + transition_duration >= scene_durations[0]:
                    errors.append("첫 장면 길이가 시작 페이드와 전환 페이드를 함께 적용하기에 너무 짧습니다.")
                if fade_out_enabled and edge_fade_duration + transition_duration >= scene_durations[-1]:
                    errors.append("마지막 장면 길이가 종료 페이드와 전환 페이드를 함께 적용하기에 너무 짧습니다.")

        summary.append(f"장면 수: {len(scenes)}개")
        summary.append(f"예상 영상 길이: {format_seconds_for_display(total_duration)}")
        summary.append(f"로고: {'사용' if logo_path else '없음'}")
        summary.append(f"배경음악: {'사용' if music_path else '없음'}")
        summary.append(f"장면 전환 방식: {transition_mode}")
        summary.append(f"전환 시간: {transition_duration:.1f}초")
        summary.append(f"첫 장면 페이드 인: {'사용' if fade_in_enabled else '없음'}")
        summary.append(f"마지막 장면 페이드 아웃: {'사용' if fade_out_enabled else '없음'}")
        summary.append(f"시작/종료 페이드 시간: {edge_fade_duration:.1f}초")
        summary.append(f"장면 자막: {'사용' if self.subtitle_enabled_checkbox.isChecked() else '사용 안 함'}")
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

    def export_selected_clip(self) -> None:
        """현재 선택된 표 행의 원본 영상 구간을 개별 MP4 클립으로 저장합니다."""

        selected_row = self.table.currentRow()
        if selected_row < 0:
            QMessageBox.information(self, "선택 장면 없음", "잘라 저장할 장면 행을 선택해 주세요.")
            return

        scene = self._scene_from_row(selected_row)
        self._start_clip_export([scene], 1, "선택 장면 클립 저장")

    def export_all_clips(self) -> None:
        """video_path가 입력된 모든 표 행을 순서대로 개별 MP4 클립으로 저장합니다."""

        scenes = [scene for scene in self._all_scenes() if scene.video_path.strip()]
        if not scenes:
            QMessageBox.information(self, "저장할 장면 없음", "video_path가 입력된 장면이 없습니다.")
            return

        self._start_clip_export(scenes, 1, "전체 장면 클립 저장")

    def _subtitle_mask_options(self) -> tuple[str, int, int | None] | None:
        """클립 저장에 사용할 기존 자막 가리기 옵션을 읽고 안내 메시지로 검증합니다."""

        subtitle_mask_mode = self.subtitle_mask_mode_combo.currentText().strip() or SUBTITLE_MASK_NONE
        subtitle_mask_height = self.subtitle_mask_height_spin.value()
        subtitle_y_text = self.subtitle_mask_y_edit.text().strip()

        if not 30 <= subtitle_mask_height <= 400:
            QMessageBox.warning(self, "자막 영역 높이 확인", "자막 영역 높이는 30~400px 범위로 입력해 주세요.")
            return None

        subtitle_mask_y: int | None = None
        if subtitle_y_text:
            try:
                subtitle_mask_y = int(subtitle_y_text)
            except ValueError:
                QMessageBox.warning(self, "자막 영역 시작 Y 확인", "자막 영역 시작 Y 위치는 숫자로 입력해 주세요.")
                return None
            if subtitle_mask_y < 0:
                QMessageBox.warning(self, "자막 영역 시작 Y 확인", "자막 영역 시작 Y 위치는 0 이상의 숫자로 입력해 주세요.")
                return None

        if subtitle_mask_mode not in SUBTITLE_MASK_MODES:
            QMessageBox.warning(self, "기존 자막 처리 방식 확인", "지원하지 않는 기존 자막 처리 방식입니다.")
            return None

        return subtitle_mask_mode, subtitle_mask_height, subtitle_mask_y

    def _start_clip_export(self, scenes: list[Scene], start_index: int, title: str) -> None:
        """클립 저장 작업을 별도 스레드에서 시작합니다."""

        output_dir = self.clip_output_edit.text().strip()
        if not output_dir:
            self.choose_clip_output_dir()
            output_dir = self.clip_output_edit.text().strip()
        if not output_dir:
            return

        subtitle_mask_options = self._subtitle_mask_options()
        if subtitle_mask_options is None:
            return
        subtitle_mask_mode, subtitle_mask_height, subtitle_mask_y = subtitle_mask_options

        # ClipExportWorker가 실제 ffmpeg 실행 전 모든 입력값을 다시 검증합니다.
        # 출력 폴더는 없으면 자동 생성되며, 한글 경로도 pathlib/리스트 인자로 그대로 전달합니다.
        self._set_clip_buttons_enabled(False)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("0% - 클립 저장 준비")
        self.log(f"===== {title} 시작 =====")
        if subtitle_mask_mode != SUBTITLE_MASK_NONE:
            y_message = "하단 자동" if subtitle_mask_y is None else f"Y={subtitle_mask_y}px"
            self.log(f"기존 자막 처리: {subtitle_mask_mode}, 높이={subtitle_mask_height}px, 시작={y_message}")

        self.clip_worker_thread = QThread(self)
        self.clip_worker = ClipExportWorker(
            ffmpeg_path=self.ffmpeg_edit.text().strip(),
            scenes=scenes,
            output_dir=output_dir,
            start_index=start_index,
            subtitle_mask_mode=subtitle_mask_mode,
            subtitle_mask_height=subtitle_mask_height,
            subtitle_mask_y=subtitle_mask_y,
        )
        self.clip_worker.moveToThread(self.clip_worker_thread)

        self.clip_worker_thread.started.connect(self.clip_worker.run)
        self.clip_worker.log.connect(self.log)
        self.clip_worker.progress.connect(self.update_progress)
        self.clip_worker.finished.connect(self._clip_export_finished)
        self.clip_worker.failed.connect(self._clip_export_failed)
        self.clip_worker.finished.connect(self.clip_worker_thread.quit)
        self.clip_worker.failed.connect(self.clip_worker_thread.quit)
        self.clip_worker_thread.finished.connect(self.clip_worker_thread.deleteLater)
        self.clip_worker_thread.start()

    def _set_clip_buttons_enabled(self, enabled: bool) -> None:
        """클립 저장 중 중복 실행을 막기 위해 관련 버튼 상태를 묶어서 바꿉니다."""

        self.export_selected_clip_button.setEnabled(enabled)
        self.export_all_clips_button.setEnabled(enabled)

    def _clip_export_finished(self, output_dir: str) -> None:
        self._set_clip_buttons_enabled(True)
        self.progress_bar.setValue(100)
        self.progress_bar.setFormat("100% - 클립 저장 완료")
        self.log(f"클립 저장 완료 폴더: {output_dir}")
        QMessageBox.information(self, "완료", f"클립 저장이 완료되었습니다.\n{output_dir}")
        self.clip_worker = None
        self.clip_worker_thread = None

    def _clip_export_failed(self, error_message: str) -> None:
        self._set_clip_buttons_enabled(True)
        self.progress_bar.setFormat("클립 저장 오류")
        self.log(f"클립 저장 오류: {error_message}")
        QMessageBox.critical(self, "클립 저장 오류", error_message)
        self.clip_worker = None
        self.clip_worker_thread = None

    def export_video(self) -> None:
        """입력값을 검증하고 별도 스레드에서 최종 MP4 생성을 시작합니다."""

        scenes = [scene for scene in self._all_scenes() if scene.video_path.strip()]
        scenes, duration_warnings = self._fill_missing_end_times(scenes)
        if not scenes:
            QMessageBox.warning(self, "장면 없음", "원본 영상을 하나 이상 추가해 주세요.")
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
        self.log(
            "전환 설정: "
            f"{self.transition_mode_combo.currentText()}, "
            f"전환 {self.transition_duration_spin.value():.1f}초, "
            f"첫 페이드 {'사용' if self.fade_in_checkbox.isChecked() else '없음'}, "
            f"마지막 페이드 {'사용' if self.fade_out_checkbox.isChecked() else '없음'}"
        )
        self.log(f"장면 자막: {'사용' if self.subtitle_enabled_checkbox.isChecked() else '사용 안 함'}")

        self.worker_thread = QThread(self)
        self.worker = ExportWorker(
            ffmpeg_path=self.ffmpeg_edit.text().strip(),
            scenes=scenes,
            logo_path=self.logo_edit.text().strip(),
            music_path=self.music_edit.text().strip(),
            output_path=output_path,
            transition_mode=self.transition_mode_combo.currentText(),
            transition_duration=self.transition_duration_spin.value(),
            fade_in_enabled=self.fade_in_checkbox.isChecked(),
            fade_out_enabled=self.fade_out_checkbox.isChecked(),
            edge_fade_duration=self.edge_fade_duration_spin.value(),
            subtitle_enabled=self.subtitle_enabled_checkbox.isChecked(),
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

    def preview_final_output(self) -> None:
        """최종 생성된 MP4를 같은 미리보기 플레이어에 로드하고 재생합니다."""

        output_path = self.output_edit.text().strip()
        if not output_path:
            QMessageBox.information(self, "최종 결과 미리보기", "먼저 최종 MP4를 생성해 주세요.")
            return

        path = Path(output_path)
        if not path.exists():
            QMessageBox.information(self, "최종 결과 미리보기", "먼저 최종 MP4를 생성해 주세요.")
            self.log(f"최종 결과 미리보기 실패: 파일 없음 - {path}")
            return

        self.preview_stop_at_ms = None
        self.preview_frame_timer.stop()
        self.preview_mode = "player"
        self.preview_stack.setCurrentIndex(0)
        self.preview_current_path = str(path)
        self.preview_original_path = str(path)
        self.preview_mode_label.setText("최종 결과 미리보기: 자막/로고/BGM/전환 효과가 적용된 MP4입니다.")
        self.preview_player.setSource(QUrl.fromLocalFile(str(path)))
        self.preview_slider.setValue(0)
        self.preview_player.play()
        self.log(f"최종 결과 미리보기 로드: {path}")

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
        self.output_edit.setText(output_path)
        self.preview_final_output()
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


def main(project_file: str | None = None) -> int:
    """Qt 애플리케이션 진입점입니다."""

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    window = MainWindow()
    if project_file:
        try:
            window.load_project(project_file)
        except Exception as exc:
            window.log(f"시작 프로젝트 자동 불러오기 실패: {project_file} - {exc}")
            QMessageBox.warning(
                window,
                "프로젝트 자동 불러오기 실패",
                f"프로젝트를 자동으로 불러오지 못했습니다.\n{project_file}\n\n수동으로 불러올 수 있습니다.",
            )
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))
