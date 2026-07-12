# Tapo H200 Download

Tapo H200 허브에 연결된 D230 도어벨 녹화 영상을 로컬에서 조회, 다운로드, 재생하는 Python 도구입니다.

이 프로젝트는 공식 Tapo 공개 API가 아니라 커뮤니티 리버스 엔지니어링 기반 동작을 사용합니다. 먼저 `--setup`, `--children`, `--list` 순서로 본인 H200/D230 조합에서 동작하는지 확인하세요.

## 기능

- H200에 페어링된 자식 카메라 목록 조회
- 날짜별 녹화 목록 조회
- 녹화 영상을 MP4로 다운로드
- 이미 받은 파일 건너뛰기
- 로컬 Web UI에서 저장된 영상 재생 및 기기로 다운로드
- H200/Tapo 비밀번호를 로컬 암호화 config로 저장
- H200 IP와 D230 `device_id`/alias를 `.env`에 자동 저장

## 설치

```bash
git clone <repo-url>
cd tapo_h200_download
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Python 3.12 이상을 권장합니다.

## 첫 설정

새 PC나 새 작업 폴더에서는 먼저 setup을 실행합니다.

```bash
.venv/bin/python h200_recordings.py --setup
```

`--setup`은 다음 작업을 자동으로 처리합니다.

- H200 IP 입력 및 연결 확인
- H200/Tapo 계정 정보 입력
- `tapo_h200.local.json`에 암호화된 credential 저장
- `tapo_h200.local.key`에 로컬 복호화 키 저장
- H200에 연결된 D230 `device_id` 조회
- `.env`에 H200 IP, config/key 경로, D230 `device_id`, alias 자동 저장

생성되는 로컬 파일:

```text
.env
tapo_h200.local.json
tapo_h200.local.key
```

`.env` 예시:

```dotenv
TAPO_CONFIG=tapo_h200.local.json
TAPO_KEY_FILE=tapo_h200.local.key
TAPO_HUB_HOST=H200_IP
TAPO_USER=admin
TAPO_DEVICE_ID=D230_DEVICE_ID
TAPO_CAMERA_ALIAS=D230_ALIAS
```

비밀번호는 기본적으로 `.env`에 평문 저장하지 않습니다. `--setup`이 만든 `tapo_h200.local.json`에는 암호화된 payload가 저장되고, `tapo_h200.local.key`가 있으면 자동 복호화되어 비밀번호를 매번 입력하지 않아도 됩니다.

주의: `tapo_h200.local.json`과 `tapo_h200.local.key`를 둘 다 가진 사용자는 저장된 credential을 복호화할 수 있습니다. 두 파일과 `.env`는 공유하거나 커밋하지 마세요.

## 비밀번호 입력 기준

```text
H200/camera account password:
  Tapo 앱 > 고급 설정 > Camera Account를 만든 경우 그 비밀번호를 입력
  잘 모르겠으면 Tapo cloud password를 입력

Tapo cloud password:
  TP-Link/Tapo 계정 비밀번호를 입력
  첫 번째 입력값과 같으면 그냥 Enter
```

## D230 deviceId 자동 저장

일반적으로는 `--setup` 한 번이면 D230 `device_id`가 `.env`에 자동 저장됩니다.

나중에 D230을 다시 찾거나 `.env`를 갱신하려면:

```bash
.venv/bin/python h200_recordings.py --children --save-env
```

같은 작업을 하는 조회 전용 스크립트도 있습니다. 기본 동작은 조회 후 `.env` 갱신입니다.

```bash
.venv/bin/python find_d230_deviceid.py
```

조회만 하고 `.env`를 건드리지 않으려면:

```bash
.venv/bin/python find_d230_deviceid.py --no-save-env
```

출력 예:

```text
Children: 1
0: alias=D230_ALIAS model=D230 device_id=D230_DEVICE_ID mac=D230_MAC
Saved local runtime settings to /path/to/tapo_h200_download/.env
```

여러 카메라가 있을 때 특정 항목을 저장하려면:

```bash
.venv/bin/python h200_recordings.py --children --save-env --camera-index 0
```

`.env`에 저장된 `TAPO_DEVICE_ID`가 있으면 CLI와 Web UI 모두 그 D230을 우선 선택합니다. 그래도 찾지 못하면 alias, `D230`/`doorbell` 문자열, 첫 번째 카메라 순서로 선택합니다.

## CLI 사용

자식 장치 목록 확인:

```bash
.venv/bin/python h200_recordings.py --children
```

오늘 녹화 목록 확인:

```bash
.venv/bin/python h200_recordings.py --list
```

특정 날짜 녹화 목록 확인:

```bash
.venv/bin/python h200_recordings.py --list --start-date 20260709 --end-date 20260709
```

목록에서 0번 영상을 MP4로 다운로드:

```bash
.venv/bin/python h200_recordings.py --start-date 20260709 --end-date 20260709 --download-index 0
```

오늘 날짜 전체 다운로드, 이미 있는 파일은 건너뛰기:

```bash
.venv/bin/python h200_recordings.py --download-all --skip-existing
```

특정 날짜 전체 다운로드, 이미 있는 파일은 건너뛰기:

```bash
.venv/bin/python h200_recordings.py --start-date 20260709 --end-date 20260709 --download-all --skip-existing
```

출력 파일은 기본적으로 아래 폴더에 저장됩니다.

```text
recordings/YYYYMMDD/CAMERA_ALIAS_YYYYMMDD_HHMMSS.mp4
```

## H200 IP 변경 또는 No Route To Host

H200 IP가 바뀐 것 같거나 `No route to host`가 나오면 후보 IP를 확인합니다.

```bash
.venv/bin/python h200_recordings.py --scan
```

아직 `.env`나 local config에 H200 IP가 없다면 같은 대역의 IP를 하나 넘겨 스캔 기준 네트워크를 알려주세요.

```bash
.venv/bin/python h200_recordings.py --scan --host H200_IP_OR_SAME_SUBNET_IP
```

`No route to host`는 비밀번호 문제가 아니라 PC가 H200에 네트워크로 접근하지 못한다는 뜻입니다. H200 전원, PC와 H200의 LAN/VLAN, 게스트 Wi-Fi/AP isolation, H200 IP 변경 여부를 확인하세요.

## Web UI

직접 실행:

```bash
.venv/bin/python h200_web.py --bind 0.0.0.0 --port 8092
```

관리 스크립트 사용:

```bash
./start_h200_web
./stop_h200_web
H200_WEB_PORT=8093 ./start_h200_web
```

LAN 또는 WireGuard/VPN에서 접속:

```text
http://PC_IP:8092
```

Web UI는 저장된 MP4 재생, 누락된 클립 다운로드, 현재 접속한 휴대폰/태블릿/PC로 MP4 다운로드를 지원합니다.

주의: Web UI에는 자체 인증이 없습니다. 신뢰하는 LAN/VPN에서만 바인딩하고 공용 인터넷에 직접 노출하지 마세요.

## 향후 스트리밍 연동 참고

스트리밍 기능은 아직 이 프로젝트에 구현되어 있지 않습니다. 나중에 go2rtc 같은 도구와 연동할 때는 H200 IP, D230 `device_id`, 그리고 H200/camera password의 SHA256 hash가 필요할 수 있습니다.

```yaml
streams:
  d230:
    - tapo://admin:YOUR_UPPERCASE_SHA256_PASSWORD_HASH@H200_IP/?deviceId=D230_DEVICE_ID
```

참고:

- go2rtc username은 보통 Tapo 이메일이 아니라 `admin`입니다.
- `deviceId`는 D230 child `device_id`이고 H200 MAC 주소가 아닙니다.
- SHA256 hash는 H200/camera password에서 만들어집니다.
- 이 hash도 비밀번호처럼 취급하고 커밋하지 마세요.

## Git에 올리면 안 되는 파일

`.gitignore`에 포함되어 있지만, 커밋 전 반드시 확인하세요.

```text
.env
tapo_h200.local.json
tapo_h200.local.key
recordings/
*.mp4
*.download.ts
*.log
*.pid
.venv/
__pycache__/
*.pcap
*.pcapng
```

커밋 전 확인:

```bash
git status --short
git status --ignored --short
```

H200는 내부적으로 MPEG-TS chunks를 전송합니다. 이 도구는 임시 `.download.ts` 파일을 받은 뒤 private G.711 A-law audio stream을 AAC로 변환하고 H264 video와 함께 MP4로 remux합니다. 기본값으로 임시 TS 파일은 제거됩니다.
