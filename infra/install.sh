#!/bin/bash
# infra/install.sh
# AI SI 매뉴팩처링 플랫폼 macOS 설치 스크립트
# 실행: bash infra/install.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PLIST_NAME="com.ai-si-platform.engine.plist"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
PLIST_SRC="$SCRIPT_DIR/$PLIST_NAME"
PLIST_DST="$LAUNCHD_DIR/$PLIST_NAME"

echo "=== AI SI 매뉴팩처링 플랫폼 설치 ==="
echo "프로젝트 경로: $PROJECT_DIR"

# 1. 의존성 설치
echo ""
echo "[1/4] pip 의존성 설치..."
pip install fastapi uvicorn aiohttp PyJWT cryptography structlog prometheus_client hypothesis pytest

# 2. 디렉토리 생성
echo ""
echo "[2/4] 디렉토리 생성..."
mkdir -p "$PROJECT_DIR/backups"
mkdir -p "$PROJECT_DIR/artifacts"
mkdir -p "$PROJECT_DIR/sandboxes"

# 3. WorkingDirectory 업데이트
echo ""
echo "[3/4] plist WorkingDirectory 설정..."
PLIST_TMP=$(mktemp)
sed "s|/Users/codename/Downloads/v8|$PROJECT_DIR|g" "$PLIST_SRC" > "$PLIST_TMP"

# Python 경로 자동 감지
PYTHON_PATH=$(which python3)
sed -i '' "s|/usr/local/bin/python3|$PYTHON_PATH|g" "$PLIST_TMP"

mkdir -p "$LAUNCHD_DIR"
cp "$PLIST_TMP" "$PLIST_DST"
rm "$PLIST_TMP"
echo "plist 설치됨: $PLIST_DST"

# 4. launchd 등록
echo ""
echo "[4/4] launchd 서비스 등록..."
# 기존 등록 해제 (있을 경우)
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"
echo "서비스 등록 완료."

echo ""
echo "=== 설치 완료 ==="
echo "상태 확인: launchctl list | grep ai-si-platform"
echo "로그 확인: tail -f /tmp/ai-si-platform.stdout.log"
echo "서비스 중지: launchctl stop com.ai-si-platform.engine"
echo "서비스 제거: launchctl unload $PLIST_DST"
echo ""
echo "대시보드: http://localhost:8000"
