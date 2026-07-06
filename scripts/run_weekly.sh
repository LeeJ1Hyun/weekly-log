#!/bin/bash
# 주간 업무 정리 자동 실행 래퍼 (launchd 에서 호출).
# launchd 는 최소 PATH 로 실행되므로 git/gh/claude 를 찾을 수 있도록 PATH 를 명시한다.
# 경로는 이 스크립트의 실제 위치에서 자동 계산하므로 어디에 클론해도 동작한다.
set -uo pipefail

# git, gh, claude 등이 있는 경로 (subprocess 가 PATH 로 찾는다)
# claude CLI 는 보통 ~/.local/bin 에 설치되므로 launchd 최소 PATH 에 추가한다.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# 이 스크립트가 있는 scripts/ 디렉토리 (심볼릭 링크도 따라감)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/../logs"

# 사용 가능한 python3 를 자동 탐색 (pyenv/homebrew/system 무관)
PYTHON="$(command -v python3 || true)"
if [ -z "$PYTHON" ]; then
  echo "python3 를 찾을 수 없습니다. PATH 를 확인하세요." >&2
  exit 1
fi

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/weekly-$(date +%Y%m%d-%H%M%S).log"

{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') 주간 정리 시작 ====="
  cd "$SCRIPT_DIR" || exit 1
  "$PYTHON" generate_weekly.py
  echo "===== 종료 (exit=$?) ====="
} >> "$LOG_FILE" 2>&1

# 오래된 로그 정리 (60일 초과)
find "$LOG_DIR" -name 'weekly-*.log' -mtime +60 -delete 2>/dev/null

exit 0
