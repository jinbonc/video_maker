# MarineGlory Video Maker

PySide6 GUI와 `ffmpeg.exe`를 이용해 여러 MP4 현장 영상을 자르고, 순서대로 이어 붙이고, 장면별 자막/회사 로고/배경음악을 넣어 최종 홍보영상 MP4를 생성하는 프로그램입니다.

## 실행 준비

```powershell
cd "D:\Codex Project\VIDEO_TOOL"
pip install -r requirements.txt
python main.py
```

## ffmpeg.exe 준비

프로그램은 아래 순서로 `ffmpeg.exe`를 자동 인식합니다.

1. PyInstaller로 만든 exe와 같은 폴더
2. `main.py`와 같은 폴더
3. 현재 작업 폴더
4. Windows PATH

배포할 때는 생성된 exe 옆에 `ffmpeg.exe`를 함께 두는 방식을 권장합니다.

## 사용 흐름

1. `MP4 영상 추가`로 여러 영상을 선택합니다.
2. 영상 길이가 자동으로 읽히면 종료 시간이 전체 길이로 입력됩니다.
3. 장면명, 시작 시간, 종료 시간, 자막을 확인합니다.
4. 필요하면 장면을 위/아래로 이동해 순서를 정합니다.
5. 회사 로고 PNG와 배경음악 MP3를 선택합니다.
6. 최종 출력 MP4 경로를 선택합니다.
7. `최종 MP4 생성`을 누르면 입력값 검증 결과를 확인한 뒤 export가 시작됩니다.

시간 입력은 `12`, `12.5`, `01:23`, `00:01:23.5` 형식을 지원합니다.

## 자막 처리

한글 자막 안정성을 위해 `drawtext` 대신 임시 ASS 자막 파일을 생성하고 ffmpeg `subtitles` 필터로 영상에 입힙니다. Windows 기본 한글 폰트인 `Malgun Gothic`을 기준으로 스타일을 지정합니다.

## PyInstaller 빌드

기본 빌드:

```powershell
pyinstaller --noconfirm --onefile --windowed --name "MarineGlory Video Maker" main.py
```

빌드가 끝나면 아래처럼 배치합니다.

```text
dist/
  MarineGlory Video Maker.exe
  ffmpeg.exe
```

`ffmpeg.exe`를 exe와 같은 폴더에 두면 프로그램이 자동으로 인식합니다.

## 참고

- 출력 해상도는 1920x1080 기준입니다.
- 비율이 다른 영상은 중앙 크롭 방식으로 맞춥니다.
- 중간 임시 클립은 작업 중 임시 폴더에 생성되고 완료 후 자동 삭제됩니다.
