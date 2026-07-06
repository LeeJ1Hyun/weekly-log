#!/usr/bin/env python3
"""
git 커밋 로그 기반 주간 업무 정리 자동 생성기.

설정한 저장소 루트 하위의 모든 git 저장소를 자동 탐색하여, 한 주(7일)에
'내 커밋'이 있는 저장소만 골라 프로젝트별·날짜별로 정리한 마크다운 노트를
생성한다(Obsidian vault 로 열면 callout/그래프까지 보이지만 필수는 아님).

주 시작 요일·작성자·경로 등 개인마다 다른 값은 config.json 에 둔다.
`--init` 대화형 마법사로 만든다. 수집 단위는 '주 시작 요일 ~ +6일' 7일이며,
기본값은 오늘 이전에 '완료된' 가장 최근 사이클(진행 중인 오늘은 제외).

사용법:
    python generate_weekly.py --init             # (최초 1회) 대화형 초기 설정
    python generate_weekly.py                    # 완료된 최근 사이클(예약 실행 기본)
    python generate_weekly.py --prev             # 그 직전 사이클
    python generate_weekly.py --window-start 2026-06-05  # 그 시작일부터 7일
    python generate_weekly.py --date 2026-06-10  # 해당 날짜가 속한 사이클
    python generate_weekly.py --dry-run          # 파일 저장 없이 내용만 출력
    python generate_weekly.py --no-pr            # PR 링크 조회 생략
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
from pathlib import Path

# ───────────────────────── 설정 ─────────────────────────

WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]   # weekday(): 월=0 … 일=6
WEEKDAY_EN = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# claude 회고 요약 호출 최대 대기(초). 초과 시 커밋 나열 폴백.
# 커밋이 많은 주는 구조화 본문 생성이 길어진다. launchd 는 외부 타임아웃이
# 없으므로 넉넉히 잡아 폴백(나열)으로 떨어지지 않게 한다.
SUMMARY_TIMEOUT = 300

# 설정 파일 위치: 패키지 루트(scripts/ 의 부모)의 config.json.
# 개인마다 다른 값(주 시작 요일·작성자·경로 등)은 코드가 아니라 여기에 둔다.
# `--init` 대화형 마법사로 생성한다.
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


def parse_weekday(value) -> int:
    """'금' / 'fri' / 4 → weekday 인덱스(월=0 … 일=6)."""
    if isinstance(value, int):
        return value % 7
    s = str(value).strip().lower()
    if s in WEEKDAY_KR:
        return WEEKDAY_KR.index(s)
    if s[:3] in WEEKDAY_EN:
        return WEEKDAY_EN.index(s[:3])
    if s.isdigit():
        return int(s) % 7
    raise ValueError(f"알 수 없는 요일: {value!r} (월~일 또는 mon~sun 로 입력)")


class Config:
    """config.json 을 읽어 담는 런타임 설정.

    주 '종료 요일'과 파일명 라벨(예: 금~목)은 시작 요일 하나에서 자동
    파생된다(종료 = 시작 + 6일). 즉 개인이 정할 값은 '주 시작 요일' 하나뿐이다.
    """

    def __init__(self, data: dict):
        ws = parse_weekday(data.get("week_start", "월"))
        self.cycle_start_weekday = ws
        self.cycle_end_weekday = (ws + 6) % 7                 # 시작 + 6일
        self.author_regex = data.get("author_regex", "")
        self.repos_root = Path(data.get("repos_root", "~")).expanduser()
        self.vault_dir = Path(
            data.get("vault_dir", str(CONFIG_PATH.parent))).expanduser()
        self.notes_subdir = data.get("notes_subdir", "주간 작업 정리")
        self.scan_maxdepth = int(data.get("scan_maxdepth", 2))
        self.summary_model = data.get("summary_model", "sonnet")

    @property
    def cycle_label(self) -> str:
        """파일명/제목에 쓰는 사이클 라벨 (예: '금~목')."""
        return (f"{WEEKDAY_KR[self.cycle_start_weekday]}"
                f"~{WEEKDAY_KR[self.cycle_end_weekday]}")


def load_config() -> Config:
    """config.json 을 읽어 Config 로 반환. 없으면 안내 후 종료."""
    if not CONFIG_PATH.exists():
        print(f"[!] 설정 파일이 없습니다: {CONFIG_PATH}")
        print("    먼저 초기 설정을 실행하세요:")
        print("      python generate_weekly.py --init")
        sys.exit(2)
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        print(f"[!] 설정 파일을 읽을 수 없습니다: {e}")
        sys.exit(2)
    return Config(data)


# main()/run_init() 에서 채운다. git_log·사이클 계산·note_filename 등이 참조한다.
CFG: "Config"


# ───────────────────────── 날짜 계산 ─────────────────────────

def cycle_from_start(start: dt.date) -> tuple[dt.date, dt.date]:
    """시작일(금요일) → (시작일, 시작일+6=목요일) 7일 윈도우."""
    return start, start + dt.timedelta(days=6)


def cycle_containing(d: dt.date) -> tuple[dt.date, dt.date]:
    """주어진 날짜가 속한 사이클을 반환."""
    # d 이하의 가장 가까운 '주 시작 요일'을 시작일로
    offset = (d.weekday() - CFG.cycle_start_weekday) % 7
    start = d - dt.timedelta(days=offset)
    return cycle_from_start(start)


def latest_completed_cycle(today: dt.date) -> tuple[dt.date, dt.date]:
    """오늘 이전에 '완료된' 가장 최근 사이클을 반환.

    종료일(주 마지막 요일)은 오늘보다 반드시 과거여야 한다. 오늘이 바로 그
    종료 요일이면 그날은 진행 중이므로 한 주 전 종료일을 잡는다.
    """
    offset = (today.weekday() - CFG.cycle_end_weekday) % 7
    if offset == 0:          # 오늘이 종료 요일 → 진행 중, 지난 주 사용
        offset = 7
    end = today - dt.timedelta(days=offset)
    return end - dt.timedelta(days=6), end


# ───────────────────────── git 조회 ─────────────────────────

def find_git_repos(root: Path, maxdepth: int) -> list[Path]:
    """root 하위에서 .git 디렉토리를 가진 저장소 경로 목록을 반환."""
    repos: list[Path] = []
    root = root.resolve()

    def scan(path: Path, depth: int) -> None:
        if depth > maxdepth:
            return
        try:
            entries = list(path.iterdir())
        except (PermissionError, FileNotFoundError):
            return
        if any(e.name == ".git" for e in entries):
            repos.append(path)
            return  # 저장소 안쪽은 더 내려가지 않음
        for e in entries:
            if e.is_dir() and not e.name.startswith("."):
                scan(e, depth + 1)

    scan(root, 1)
    return sorted(repos)


def git_log(repo: Path, since: str, until: str) -> list[dict]:
    """저장소에서 기간 내 '내 커밋'을 {date, sha, msg} 목록으로 반환.

    --all 로 모든 브랜치를 포함하고, --no-merges 로 머지 커밋은 제외한다.
    같은 (날짜, 메시지)가 여러 번 나오면(여러 브랜치/리베이스) 중복 제거한다.
    """
    fmt = "%ad\t%H\t%s"
    cmd = [
        "git", "-C", str(repo), "log",
        # --all 은 stash(refs/stash)까지 포함하므로 제외한다.
        # stash 는 "WIP on ...", "index on ..." 형태의 가짜 커밋을 만든다.
        "--exclude=refs/stash", "--all", "--no-merges",
        "-P", f"--author={CFG.author_regex}",
        f"--since={since}", f"--until={until}",
        "--date=format:%Y-%m-%d", f"--format={fmt}",
    ]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return []

    seen: set[tuple[str, str]] = set()
    commits: list[dict] = []
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        date_s, sha, msg = parts
        msg = msg.strip()
        if not msg:
            continue
        # stash 잔여물("WIP on <branch>", "index on <branch>") 제외
        if msg.startswith("WIP on ") or msg.startswith("index on "):
            continue
        key = (date_s, msg)
        if key in seen:
            continue
        seen.add(key)
        try:
            d = dt.date.fromisoformat(date_s)
        except ValueError:
            continue
        commits.append({"date": d, "sha": sha, "msg": msg})
    return commits


def repo_slug(repo: Path) -> str | None:
    """저장소의 origin remote 에서 'owner/repo' 슬러그를 추출 (GitHub만)."""
    try:
        url = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None
    # git@github.com:owner/repo.git  또는  https://github.com/owner/repo.git
    if "github.com" not in url:
        return None
    if url.startswith("git@"):
        path = url.split(":", 1)[-1]
    else:
        path = url.split("github.com/", 1)[-1]
    return path.removesuffix(".git")


def resolve_pr(slug: str, sha: str) -> dict | None:
    """커밋 SHA 에 연결된 PR 중 하나를 골라 {number, url} 로 반환.

    한 커밋이 여러 PR(스택형 브랜치)에 잡힐 수 있어 다음 규칙으로 고른다.
      1) 머지된 PR 우선 — 가장 먼저 머지된 것(실제로 들어간 PR)
      2) 전부 open 이면 — 번호가 가장 큰(최근에 만든) PR
         (오래된 통합 브랜치보다 집중 기능 PR일 가능성이 높음)
    """
    try:
        out = subprocess.run(
            ["gh", "api", f"repos/{slug}/commits/{sha}/pulls"],
            capture_output=True, text=True, timeout=20,
        ).stdout
        prs = json.loads(out)
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
    if not isinstance(prs, list) or not prs:
        return None
    merged = [p for p in prs if p.get("merged_at")]
    if merged:
        chosen = min(merged, key=lambda p: p["merged_at"])
    else:
        chosen = max(prs, key=lambda p: p.get("number", 0))
    return {"number": chosen["number"], "url": chosen["html_url"]}


def resolve_all_prs(tasks: list[tuple[str, str]]) -> dict[str, dict]:
    """(slug, sha) 목록의 PR 을 병렬 조회하여 {sha: {number,url}} 로 반환."""
    pr_map: dict[str, dict] = {}
    if not tasks:
        return pr_map
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(resolve_pr, slug, sha): sha
                   for slug, sha in tasks}
        for fut, sha in futures.items():
            pr = fut.result()
            if pr:
                pr_map[sha] = pr
    return pr_map


# ───────────────────────── LLM 본문 큐레이션 ─────────────────────────

def _commits_as_text(repo_commits: dict[str, list[dict]],
                     pr_map: dict[str, dict]) -> str:
    """커밋을 '프로젝트 → 날짜 → 메시지(+PR)' 텍스트로 직렬화 (LLM 입력용)."""
    lines: list[str] = []
    for proj in sorted(repo_commits):
        lines.append(f"## {proj}")
        by_date: dict[dt.date, list[dict]] = {}
        for c in repo_commits[proj]:
            by_date.setdefault(c["date"], []).append(c)
        for d in sorted(by_date):
            wd = WEEKDAY_KR[d.weekday()]
            lines.append(f"### {d.strftime('%Y-%m-%d')} ({wd})")
            for c in by_date[d]:
                pr = pr_map.get(c["sha"])
                ref = f"   [PR #{pr['number']} {pr['url']}]" if pr else ""
                lines.append(f"- {c['msg']}{ref}")
        lines.append("")
    return "\n".join(lines)


BODY_INSTRUCTION = """\
너는 개발자의 주간 작업 노트 '본문'을 작성한다. 입력으로 '프로젝트(repo) →
날짜 → 커밋 메시지' 목록이 주어진다(각 커밋 뒤에 [PR #번호 URL] 이 붙어 있을
수 있음). 커밋을 그대로 나열하지 말고, 아래 구조의 한국어 마크다운 본문만
출력해라.

[전체 구조]
1) 프로젝트마다 '## {프로젝트명}' 섹션을 만든다.
2) 각 프로젝트 첫머리에 한 줄 요약 callout:
   '> [!summary] {그 프로젝트 한 주 핵심 1줄} (M/D~M/D)'
3) 날짜별로 헤딩을 만들되, 그날을 대표하는 짧은 제목을 붙인다:
   '### M/D (요일) — {그날 작업 대표 제목}'
4) 그 아래 커밋들을 의미 단위(주제)로 묶는다:
   - '- **{주제}**' 로 묶고 그 아래 4칸 들여쓴 '    - {요약}' 으로 세부 항목.
   - 세부가 하나뿐이면 '- **{주제}**: {요약}' 한 줄로 쓴다.
5) 각 프로젝트 섹션 끝에 '---' 구분선을 둔다.
6) 마지막에 회고 섹션:
   '## 한 주 회고'
   '> [!note] 핵심 성과' 아래 프로젝트별 1줄 성과를 '> - ...' 로.

[규칙]
- 커밋 메시지를 복사하지 말고 '무엇을 왜 했는지'를 한 호흡으로 요약한다.
- 비슷한 커밋(같은 주제 반복/수정)은 하나의 항목으로 합친다.
- PR 링크는 그 커밋을 직접 요약한 세부 불릿의 끝에만 '([#번호](URL))' 로
  붙인다. 입력에 없는 PR 을 지어내지 마라.
- 사실에 없는 내용을 지어내지 마라. 입력 커밋 범위만 요약한다.
- 코드/식별자(snake_case 변수, API 경로, 환경변수, 상수 등)는 백틱(`)으로
  감싼다. 밑줄(_)을 백슬래시로 이스케이프(\\_)하지 마라.
- frontmatter 나 '#' H1 제목은 출력하지 마라(상위에서 붙인다). '##' 부터 시작.

[출력 예]
## 1on1 코칭

> [!summary] AX 4.5 모델 전환 + AI Edit 모달 전면 개편 (5/18~5/19)

### 5/18 (월) — AX 4.5 전환 + 코칭 가이드 재생성

- **AX 4.5 모델 전환**
    - 서버 기본 모델 AX 4.5 적용 + manager_id 백필 ([#123](https://...))
    - 게이트웨이 700 오류 대응 + 캐싱 안정화
- **코칭 기능**: 챗봇 반영 버튼이 LLM에 가이드 재생성 요청

---

## 한 주 회고

> [!note] 핵심 성과
> - 1on1 코칭: AX 4.5 전환 완료 + AI Edit 모달로 UX 전면 개편
"""


def generate_body(repo_commits: dict[str, list[dict]],
                  pr_map: dict[str, dict],
                  start: dt.date, end: dt.date,
                  model: str = "sonnet") -> str | None:
    """claude CLI 로 '큐레이션된 노트 본문'(프로젝트 섹션 + 회고)을 생성한다.

    커밋을 나열하지 않고 의미 단위로 묶어 주제별 구조로 정리한다.
    claude 를 --tools "" (모든 도구 비활성화) 로 호출하므로 텍스트 입출력만
    가능하다 — 어떤 셸 명령/파일 변경도 구조적으로 일어날 수 없다.
    실패(미설치/타임아웃/오류)하면 None 을 반환하고, 노트는 결정론적 커밋
    나열 폴백으로 계속 생성된다 (LLM 실패가 노트 생성을 막지 않는다).
    """
    if not repo_commits:
        return None
    if not shutil.which("claude"):
        print("[!] claude CLI 가 없어 LLM 본문 생성을 건너뜁니다.")
        return None

    period = f"{start.strftime('%m/%d')}~{end.strftime('%m/%d')}"
    commit_text = (f"기간: {period} ({start.isoformat()} ~ {end.isoformat()})\n\n"
                   + _commits_as_text(repo_commits, pr_map))
    try:
        proc = subprocess.run(
            ["claude", "-p", BODY_INSTRUCTION,
             "--tools", "", "--model", model],
            input=commit_text, capture_output=True, text=True,
            timeout=SUMMARY_TIMEOUT,
        )
    except (subprocess.SubprocessError, OSError) as e:
        print(f"[!] LLM 본문 호출 실패: {e}")
        return None
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()[:300]
        print(f"[!] claude 가 비정상 종료(exit={proc.returncode}): {err}")
        return None
    body = proc.stdout.strip()
    if not body:
        print("[!] LLM 본문 결과가 비어 있어 건너뜁니다.")
        return None
    return body


# ───────────────────────── 노트 생성 ─────────────────────────

def build_note(start: dt.date, end: dt.date,
               repo_commits: dict[str, list[dict]],
               pr_map: dict[str, dict],
               body: str | None = None) -> str:
    """주간 노트를 Obsidian 마크다운으로 조립.

    body(LLM 큐레이션 본문)가 있으면 frontmatter + H1 아래에 그대로 쓴다.
    없으면(LLM 실패/생략) 커밋을 프로젝트별·날짜별로 그대로 나열하는
    결정론적 폴백 본문을 쓴다 (pr_map[sha] 가 있으면 ([#번호](링크)) 부착).
    """
    s_wd, e_wd = WEEKDAY_KR[start.weekday()], WEEKDAY_KR[end.weekday()]
    title_range = (f"{start.strftime('%Y-%m-%d')}({s_wd}) ~ "
                   f"{end.strftime('%m-%d')}({e_wd})")

    projects = sorted(repo_commits.keys())

    lines: list[str] = []
    # frontmatter
    lines.append("---")
    lines.append("tags:")
    lines.append("  - weekly-log")
    # 연도는 태그가 아니라 별도 속성으로 둔다.
    # (순수 숫자 '2026' 은 Obsidian 에서 유효한 태그가 아니라 경고가 뜬다.)
    lines.append(f"year: {start.year}")
    lines.append(f"period: {start.isoformat()} ~ {end.isoformat()}")
    lines.append("projects:")
    for p in projects:
        lines.append(f"  - {p}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {title_range} 주간 작업 정리")
    lines.append("")

    if not projects:
        lines.append("> [!warning] 이 기간에 커밋이 없습니다.")
        lines.append("")
        return "\n".join(lines)

    # LLM 큐레이션 본문이 있으면 그대로 사용 (프로젝트 섹션 + 회고 포함)
    if body:
        lines.append(body)
        lines.append("")
        return "\n".join(lines)

    # ── 폴백: 커밋을 그대로 나열하는 결정론적 본문 ──
    # 프로젝트별 섹션
    for proj in projects:
        commits = repo_commits[proj]
        lines.append(f"## {proj}")
        lines.append("")
        lines.append(f"> [!summary] 커밋 {len(commits)}건")
        lines.append("")

        # 날짜별 그룹화
        by_date: dict[dt.date, list[dict]] = {}
        for c in commits:
            by_date.setdefault(c["date"], []).append(c)

        for d in sorted(by_date):
            wd = WEEKDAY_KR[d.weekday()]
            lines.append(f"### {d.strftime('%m/%d')} ({wd})")
            for c in by_date[d]:
                pr = pr_map.get(c["sha"])
                suffix = f" ([#{pr['number']}]({pr['url']}))" if pr else ""
                lines.append(f"- {c['msg']}{suffix}")
            lines.append("")
        lines.append("---")
        lines.append("")

    # 한 주 회고 (폴백 본문에서는 직접 작성용 플레이스홀더)
    lines.append("## 한 주 회고")
    lines.append("")
    lines.append("> [!note] 핵심 성과")
    lines.append("> - (프로젝트별 1줄 요약을 작성하세요)")
    lines.append("")

    return "\n".join(lines)


def note_filename(start: dt.date, end: dt.date) -> str:
    # 시작일 기준으로 정렬되도록 ISO 날짜로 시작. 라벨(예: 금~목)은 설정에서 파생.
    return f"{start.isoformat()}~{end.strftime('%m-%d')} ({CFG.cycle_label}).md"


# ───────────────────────── 초기 설정(--init) ─────────────────────────

def _prompt(question: str, default: str = "") -> str:
    """한 줄 입력을 받는다. 빈 입력이면 default. 파이프 입력(EOF)도 안전 처리."""
    suffix = f" [{default}]" if default else ""
    try:
        ans = input(f"  {question}{suffix}: ").strip()
    except EOFError:
        ans = ""
    return ans or default


def _git_global(key: str) -> str:
    """git 전역 설정값을 읽는다(없으면 빈 문자열). init 기본값 제안용."""
    try:
        return subprocess.run(
            ["git", "config", "--global", key],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def install_launchd(week_start: int, hour: int) -> None:
    """주 시작 요일 오전 `hour` 시에 실행되는 macOS LaunchAgent 를 설치한다.

    실행일은 '주 시작 요일'로 잡는다 — 그날 돌리면 방금 끝난 지난 한 주가
    이미 완료돼 있어 반쪽짜리 날이 섞이지 않는다.
    """
    label = "net.weeklylog.generate"
    wrapper = Path(__file__).resolve().parent / "run_weekly.sh"
    # launchd Weekday: 일=0 … 토=6.  Python weekday(월=0…일=6) → (+1)%7
    lw = (week_start + 1) % 7
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>{wrapper}</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key><integer>{lw}</integer>
    <key>Hour</key><integer>{hour}</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>RunAtLoad</key><false/>
</dict>
</plist>
"""
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / f"{label}.plist"
    plist_path.write_text(plist, encoding="utf-8")

    uid = subprocess.run(["id", "-u"], capture_output=True,
                         text=True).stdout.strip()
    domain = f"gui/{uid}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{label}"],
                   capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)],
                   capture_output=True)
    subprocess.run(["launchctl", "enable", f"{domain}/{label}"],
                   capture_output=True)
    print(f"  [OK] 자동 실행 등록: 매주 {WEEKDAY_KR[week_start]}요일 {hour:02d}:00")
    print(f"       plist: {plist_path}")


def install_schtasks(week_start: int, hour: int) -> None:
    """매주 지정 요일/시각에 실행되는 Windows 작업 스케줄러(schtasks) 항목을 등록한다.

    launchd 처럼 실행일은 '주 시작 요일'로 잡는다. bash 래퍼(run_weekly.sh) 대신
    Windows 용 배치 래퍼(run_weekly.bat)를 생성해 그걸 예약한다.
    """
    # schtasks 요일 약어. Python weekday(월=0 … 일=6) 순서와 그대로 대응.
    days = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
    scripts_dir = Path(__file__).resolve().parent
    bat = scripts_dir / "run_weekly.bat"
    py = sys.executable or "python"
    # 배치 래퍼: 스크립트 폴더로 이동해 python 실행, logs\weekly.log 에 append.
    bat.write_text(
        "@echo off\r\n"
        "setlocal\r\n"
        'set "SCRIPT_DIR=%~dp0"\r\n'
        'set "LOG_DIR=%SCRIPT_DIR%..\\logs"\r\n'
        'if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"\r\n'
        f'"{py}" "%SCRIPT_DIR%generate_weekly.py" >> "%LOG_DIR%\\weekly.log" 2>&1\r\n',
        encoding="utf-8",
    )
    task_name = "weekly-log"
    subprocess.run(
        ["schtasks", "/Create", "/F", "/TN", task_name, "/SC", "WEEKLY",
         "/D", days[week_start], "/ST", f"{hour:02d}:00", "/TR", str(bat)],
        capture_output=True,
    )
    print(f"  [OK] 작업 스케줄러 등록: 매주 {WEEKDAY_KR[week_start]}요일 "
          f"{hour:02d}:00 (작업 이름: {task_name})")
    print(f"       배치 래퍼: {bat}")
    print(f"       변경/해제: schtasks /Change /TN {task_name} /ST HH:MM  |  "
          f"schtasks /Delete /TN {task_name} /F")


# ───────────────────────── Obsidian 연동 ─────────────────────────

def obsidian_config_path() -> Path | None:
    """Obsidian 이 vault 목록을 저장하는 obsidian.json 경로 (OS 별)."""
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support"
                / "obsidian" / "obsidian.json")
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        return Path(appdata) / "obsidian" / "obsidian.json" if appdata else None
    return Path.home() / ".config" / "obsidian" / "obsidian.json"


def list_obsidian_vaults() -> list[Path]:
    """Obsidian 에 등록된 기존 vault 경로 목록(존재하는 폴더만)."""
    p = obsidian_config_path()
    if not p or not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    vaults = []
    for v in (data.get("vaults") or {}).values():
        raw = v.get("path")
        if raw and Path(raw).is_dir():
            vaults.append(Path(raw))
    return sorted(set(vaults), key=str)


def create_obsidian_vault(path: Path) -> None:
    """폴더를 Obsidian vault 로 만든다(.obsidian 생성). 열면 등록된다."""
    path.mkdir(parents=True, exist_ok=True)
    dot = path / ".obsidian"
    dot.mkdir(exist_ok=True)
    app = dot / "app.json"
    if not app.exists():                 # 최소 설정. 나머지는 Obsidian 이 채운다.
        app.write_text("{}\n", encoding="utf-8")


def open_in_obsidian(vault_dir: Path) -> bool:
    """obsidian://open URI 로 vault 를 연다(설치돼 있으면 등록+열림). 성공 여부 반환."""
    uri = f"obsidian://open?path={quote(str(vault_dir.resolve()))}"
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", uri], check=True, capture_output=True)
        elif sys.platform.startswith("win"):
            os.startfile(uri)            # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", uri], check=True, capture_output=True)
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def choose_vault_dir(default_dir: str) -> tuple[str, bool]:
    """노트 저장 폴더를 정한다. Obsidian vault 를 인식·생성·선택한다.

    반환: (vault_dir 문자열, obsidian 연결 여부).
    """
    vaults = list_obsidian_vaults()

    # 기존 vault 가 없음 → 생성할지 물어본다.
    if not vaults:
        print("      (등록된 Obsidian vault 를 찾지 못했습니다.)")
        if _prompt("새 Obsidian vault 를 생성할까요? (Y/n)", "Y").lower() \
                in ("y", "yes"):
            target = _prompt("vault 로 만들 폴더", default_dir)
            create_obsidian_vault(Path(target).expanduser())
            print(f"      → vault 생성: {target}")
            return target, True
        return _prompt("노트 저장 폴더", default_dir), False

    # 기존 vault 가 있음 → 목록에서 고르게 한다.
    print("      기존 Obsidian vault 를 찾았습니다. 노트를 어디에 저장할까요?")
    for i, v in enumerate(vaults, 1):
        print(f"        {i}) {v}")
    n = len(vaults)
    print(f"        {n + 1}) 새 vault 생성")
    print(f"        {n + 2}) Obsidian 연결 없이 그냥 폴더에 저장")
    while True:
        sel = _prompt("번호 선택", "1")
        if not sel.isdigit():
            print("      숫자로 입력하세요.")
            continue
        k = int(sel)
        if 1 <= k <= n:
            chosen = str(vaults[k - 1])
            print(f"      → '{chosen}' vault 안에 저장합니다.")
            return chosen, True
        if k == n + 1:
            target = _prompt("vault 로 만들 폴더", default_dir)
            create_obsidian_vault(Path(target).expanduser())
            print(f"      → vault 생성: {target}")
            return target, True
        if k == n + 2:
            return _prompt("노트 저장 폴더", default_dir), False
        print(f"      1 ~ {n + 2} 사이 번호를 입력하세요.")


def run_init() -> int:
    """다운로드 직후 1회 실행하는 대화형 초기 설정. config.json 을 만든다."""
    print("=" * 56)
    print(" 주간 업무 정리 초기 설정 (weekly-log)")
    print("=" * 56)
    if CONFIG_PATH.exists():
        if _prompt(f"이미 설정이 있습니다 ({CONFIG_PATH.name}). 덮어쓸까요? (y/N)",
                   "N").lower() not in ("y", "yes"):
            print("취소했습니다. 기존 설정을 유지합니다.")
            return 0

    # 1) 주 시작 요일 — 개인이 정할 유일한 '주 단위' 값. 종료·라벨은 자동 파생.
    print("\n[1/5] 한 주는 무슨 요일에 시작하나요? (월 화 수 목 금 토 일)")
    print("      예) '금' → 금~목 7일 사이클 / '월' → 월~일")
    while True:
        try:
            ws = parse_weekday(_prompt("주 시작 요일", "월"))
            break
        except ValueError as e:
            print(f"      {e}")
    end_wd = (ws + 6) % 7
    print(f"      → {WEEKDAY_KR[ws]}~{WEEKDAY_KR[end_wd]} 사이클로 설정합니다.")

    # 2) 작성자 식별 (git 전역 설정에서 기본값 제안)
    print("\n[2/5] '내 커밋'을 식별할 작성자 (이름/이메일). 여러 개면 | 로 구분.")
    default_authors = "|".join(
        v for v in (_git_global("user.name"), _git_global("user.email")) if v)
    author_regex = _prompt("작성자 정규식", default_authors)
    while not author_regex:
        print("      작성자는 반드시 입력해야 합니다.")
        author_regex = _prompt("작성자 정규식", default_authors)

    # 3) 저장소 탐색 루트
    print("\n[3/5] git 저장소들이 모여 있는 상위 폴더 (그 아래 2단계까지 탐색).")
    repos_root = _prompt("저장소 루트", "~")

    # 4) 노트 저장 위치 + Obsidian 연결 (기존 vault 선택 / 신규 생성 / 연결 안 함)
    print("\n[4/5] 생성된 주간 노트(.md)를 저장할 폴더 (= Obsidian vault).")
    vault_dir, obsidian_linked = choose_vault_dir(str(CONFIG_PATH.parent))

    # 5) 회고 요약 모델
    print("\n[5/5] 회고 요약에 쓸 claude 모델 (claude CLI 없으면 자동 건너뜀).")
    summary_model = _prompt("모델", "sonnet")

    cfg = {
        "week_start": WEEKDAY_KR[ws],
        "author_regex": author_regex,
        "repos_root": repos_root,
        "vault_dir": vault_dir,
        "notes_subdir": "주간 작업 정리",
        "scan_maxdepth": 2,
        "summary_model": summary_model,
    }
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n[OK] 설정 저장: {CONFIG_PATH}")

    # 자동 실행 등록 — macOS(launchd) / Windows(작업 스케줄러) 지원
    if sys.platform == "darwin":
        if _prompt("\n매주 자동 실행(launchd)을 등록할까요? (Y/n)", "Y").lower() \
                in ("y", "yes"):
            hour_s = _prompt("실행 시각 (0-23시)", "11")
            try:
                install_launchd(ws, int(hour_s))
            except (ValueError, subprocess.SubprocessError, OSError) as e:
                print(f"  [!] 자동 실행 등록 실패(수동 등록 필요): {e}")
    elif sys.platform.startswith("win"):
        if _prompt("\n매주 자동 실행(작업 스케줄러)을 등록할까요? (Y/n)", "Y").lower() \
                in ("y", "yes"):
            hour_s = _prompt("실행 시각 (0-23시)", "9")
            try:
                install_schtasks(ws, int(hour_s))
            except (ValueError, subprocess.SubprocessError, OSError) as e:
                print(f"  [!] 자동 실행 등록 실패(수동 등록 필요): {e}")
    else:
        print("\n(자동 실행 등록은 macOS/Windows 에서 지원합니다. "
              "Linux 등은 cron 으로 run_weekly.sh 를 예약하세요.)")

    # Obsidian 으로 지금 열기 (연결을 선택한 경우에만 물어본다)
    if obsidian_linked:
        if _prompt("\n지금 Obsidian 으로 이 vault 를 열까요? (Y/n)", "Y").lower() \
                in ("y", "yes"):
            if open_in_obsidian(Path(vault_dir).expanduser()):
                print("  [OK] Obsidian 으로 여는 중... (설치돼 있으면 vault 가 열립니다)")
            else:
                print("  [!] Obsidian 을 열지 못했습니다. 설치돼 있는지 확인하세요:")
                print("      https://obsidian.md/download")
                print(f"      수동: Obsidian → 'Open folder as vault' → {vault_dir}")

    print("\n완료! 지금 한 번 미리보기:")
    print("  python generate_weekly.py --dry-run")
    return 0


# ───────────────────────── main ─────────────────────────

def main() -> int:
    global CFG
    parser = argparse.ArgumentParser(
        description="주간 업무 정리 노트 생성 (7일 사이클, 주 시작 요일은 설정에서)")
    parser.add_argument("--init", action="store_true",
                        help="대화형 초기 설정 (config.json 생성). 다운로드 후 1회 실행")
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--prev", action="store_true",
                   help="한 사이클 전 (기본 사이클의 직전 주)")
    g.add_argument("--window-start",
                   help="윈도우 시작일(주 시작 요일) 지정 → 그날부터 7일 (예: 2026-06-05)")
    g.add_argument("--date",
                   help="해당 날짜가 속한 사이클 (예: 2026-06-10)")
    parser.add_argument("--dry-run", action="store_true",
                        help="파일 저장 없이 내용만 출력")
    parser.add_argument("--no-pr", action="store_true",
                        help="PR 링크 조회 생략 (오프라인/빠른 실행)")
    parser.add_argument("--no-summary", action="store_true",
                        help="LLM 본문 큐레이션 생략 → 커밋 나열 폴백 (오프라인/빠른 실행)")
    args = parser.parse_args()

    if args.init:
        return run_init()

    CFG = load_config()

    today = dt.date.today()
    if args.window_start:
        start, end = cycle_from_start(dt.date.fromisoformat(args.window_start))
    elif args.date:
        start, end = cycle_containing(dt.date.fromisoformat(args.date))
    elif args.prev:
        base_start, _ = latest_completed_cycle(today)
        start, end = cycle_from_start(base_start - dt.timedelta(days=7))
    else:
        # 기본: 오늘 이전에 완료된 가장 최근 금~목 사이클 (예약 실행용)
        start, end = latest_completed_cycle(today)

    # git 기간: 시작일 00:00:00 ~ 종료일 23:59:59
    since = f"{start.isoformat()} 00:00:00"
    until = f"{end.isoformat()} 23:59:59"

    print(f"[*] 기간: {start} ~ {end} ({CFG.cycle_label})")
    print(f"[*] 저장소 탐색: {CFG.repos_root}")

    repos = find_git_repos(CFG.repos_root, CFG.scan_maxdepth)
    print(f"[*] git 저장소 {len(repos)}개 발견, 내 커밋 조회 중...")

    repo_commits: dict[str, list[dict]] = {}
    slug_map: dict[str, str | None] = {}
    for repo in repos:
        commits = git_log(repo, since, until)
        if commits:
            repo_commits[repo.name] = commits
            slug_map[repo.name] = repo_slug(repo)
            print(f"    - {repo.name}: {len(commits)}건")

    # PR 링크 조회 (GitHub 저장소 + gh CLI 있을 때만)
    pr_map: dict[str, dict] = {}
    if not args.no_pr and shutil.which("gh"):
        tasks = [
            (slug_map[proj], c["sha"])
            for proj, commits in repo_commits.items()
            if slug_map.get(proj)
            for c in commits
        ]
        if tasks:
            print(f"[*] PR 링크 조회 중... (커밋 {len(tasks)}건)")
            pr_map = resolve_all_prs(tasks)
            print(f"[*] PR 매칭: {len(pr_map)}/{len(tasks)}건")
    elif not args.no_pr:
        print("[!] gh CLI 가 없어 PR 링크를 건너뜁니다.")

    # LLM 본문 큐레이션 (커밋을 의미 단위로 묶어 구조화). 실패 시 커밋 나열 폴백.
    body: str | None = None
    if not args.no_summary and repo_commits:
        print(f"[*] LLM 본문 큐레이션 중... (claude {CFG.summary_model}, 도구 비활성화)")
        body = generate_body(repo_commits, pr_map, start, end, CFG.summary_model)
        print("[*] 본문 큐레이션 완료" if body else "[!] LLM 본문 없음(커밋 나열 폴백)")

    note = build_note(start, end, repo_commits, pr_map, body)

    if args.dry_run:
        print("\n" + "=" * 60)
        print(note)
        return 0

    out_dir = CFG.vault_dir / CFG.notes_subdir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / note_filename(start, end)
    out_path.write_text(note, encoding="utf-8")
    print(f"\n[OK] 저장됨: {out_path}")
    total = sum(len(v) for v in repo_commits.values())
    print(f"[OK] 프로젝트 {len(repo_commits)}개 / 커밋 {total}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
