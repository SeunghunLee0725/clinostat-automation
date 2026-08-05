#!/usr/bin/env bash
# 부트스트랩 — venv + 의존성. 기본 대상은 라즈베리파이 clinostat-pi.
# 사용: bash integration/deploy/setup.sh
#
# MQTT 브로커와 Flutter 데스크톱 빌드 의존성은 DEC-012로 1차 마일스톤 런타임
# 경로에서 빠졌다. 되살리려면 SPACEBIO_WITH_MQTT=1 로 실행한다.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
echo "==> integration root: $HERE"

WITH_MQTT="${SPACEBIO_WITH_MQTT:-0}"

PLATFORM_OS="$(uname -s)"
PLATFORM_ARCH="$(uname -m)"
# 기본 python3 가 잠금 파일과 안 맞는 기계가 있다(macOS 기본이 3.13인데 잠금은
# 3.14뿐인 식). 그럴 때 PYTHON=python3.14 처럼 인터프리터를 지정한다.
PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null || { echo "ERROR: $PYTHON 을 찾을 수 없다" >&2; exit 2; }
PYTHON_MINOR="$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
case "$PLATFORM_OS:$PLATFORM_ARCH:$PYTHON_MINOR" in
  Darwin:arm64:3.14)
    CONSTRAINTS="$HERE/constraints-macos-arm64-py314-dev.txt"
    ;;
  Linux:aarch64:3.13)
    # clinostat-pi 실측: Raspberry Pi OS, Python 3.13.5, aarch64
    CONSTRAINTS="$HERE/constraints-linux-aarch64-py313.txt"
    ;;
  Linux:aarch64:3.11)
    CONSTRAINTS="$HERE/constraints-linux-aarch64-py311.txt"
    ;;
  *)
    echo "ERROR: no verified dependency lock for $PLATFORM_OS/$PLATFORM_ARCH Python $PYTHON_MINOR" >&2
    echo "  다른 인터프리터가 있으면 지정해라: PYTHON=python3.14 bash deploy/setup.sh" >&2
    echo "  검증된 조합: macOS arm64 3.14 / Linux aarch64 3.13 / Linux aarch64 3.11" >&2
    exit 2
    ;;
esac
echo "==> verified constraints: $CONSTRAINTS"

if [[ "$PLATFORM_OS" == "Linux" ]]; then
  echo "==> apt 패키지 설치 (venv/pip)"
  APT_PACKAGES=(python3-venv python3-pip)
  if [[ "$WITH_MQTT" == "1" ]]; then
    echo "    (SPACEBIO_WITH_MQTT=1 → mosquitto + Flutter 빌드 의존성 포함)"
    APT_PACKAGES+=(
      mosquitto mosquitto-clients
      clang cmake ninja-build pkg-config libgtk-3-dev
    )
  fi
  sudo apt-get update
  sudo apt-get install -y "${APT_PACKAGES[@]}"

  echo "==> 펌프 시리얼 권한: $USER 를 dialout 그룹에 추가 (재로그인 필요)"
  sudo usermod -aG dialout "$USER" || true
fi

echo "==> Python 가상환경 + 의존성"
"$PYTHON" -m venv "$HERE/.venv"
# shellcheck disable=SC1091
source "$HERE/.venv/bin/activate"
pip install --upgrade pip
pip install -r "$HERE/requirements.txt" -c "$CONSTRAINTS"

if [[ "$PLATFORM_OS" == "Linux" && "$WITH_MQTT" == "1" ]]; then
  # 설정이 인증을 요구하므로 패스워드 파일이 먼저 있어야 한다. 없이 깔면 mosquitto 가
  # 기동에 실패하고, 원인을 모른 채 allow_anonymous 로 되돌리기 쉽다 — 그러면 공인 IP
  # 망에서 브로커가 인터넷에 열린다. 그래서 여기서 막고 만드는 법을 알려준다.
  PASSWD_FILE=/etc/mosquitto/spacebio_passwd
  if [[ ! -f "$PASSWD_FILE" ]]; then
    echo "오류: $PASSWD_FILE 이 없다. 브로커 계정을 먼저 만든다:" >&2
    echo "  sudo mosquitto_passwd -c $PASSWD_FILE <계정>" >&2
    echo "  sudo chown root:mosquitto $PASSWD_FILE && sudo chmod 640 $PASSWD_FILE" >&2
    exit 1
  fi
  echo "==> Mosquitto 로컬 설정 적용 (인증 필수)"
  sudo cp "$HERE/deploy/mosquitto.conf" /etc/mosquitto/conf.d/spacebio.conf
  sudo systemctl enable --now mosquitto
fi

echo
echo "완료. Gateway 구동 확인:"
echo "  source $HERE/.venv/bin/activate"
echo "  cd $HERE && python -m uvicorn gateway.app:app --host 127.0.0.1 --port 8010"
if [[ "$WITH_MQTT" == "1" ]]; then
  echo
  echo "MQTT 경로(동결, DEC-012) 자체 점검:"
  echo "  cd $HERE && python -m gateway.main --self-test"
fi
