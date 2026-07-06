# 주간 업무 정리 자동화 (weekly-log)

git 커밋 로그를 기반으로 매주 작업 내역을 **마크다운 노트**로 자동 정리합니다.
커밋을 프로젝트별·날짜별로 묶고, GitHub PR 링크를 붙이고, "한 주 회고"를
LLM(선택)으로 요약합니다.

> **Obsidian 이 꼭 필요하진 않습니다.** 결과물은 평범한 `.md` 파일이라 아무
> 에디터/뷰어로 열립니다. Obsidian 으로 폴더를 vault 로 열면 frontmatter 태그,
> `> [!summary]` callout, 그래프 뷰까지 예쁘게 보이는 정도의 차이입니다.

## 설치 & 초기 설정

개인마다 다른 값(주 시작 요일·작성자·경로 등)은 코드가 아니라 `config.json` 에
두고, 클론 직후 **대화형 CLI 마법사로 1회 생성**합니다.

```bash
git clone <이 저장소> weekly-log
cd weekly-log/scripts

python3 generate_weekly.py --init      # ← 대화형 초기 설정
```

`--init` 이 순서대로 물어봅니다:

1. **주 시작 요일** — 한 주를 무슨 요일에 시작하는지(월~일 중 하나).
   이 하나만 정하면 종료 요일(시작+6일)과 파일명 라벨(예: `금~목`), 자동 실행일이
   전부 자동으로 따라옵니다.
2. **작성자** — '내 커밋'을 식별할 이름/이메일 (여러 개면 `|` 로 구분).
   `git config --global user.name/user.email` 값을 기본값으로 제안합니다.
3. **저장소 루트** — git 저장소들이 모여 있는 상위 폴더 (그 아래 2단계까지 탐색).
4. **노트 저장 폴더** — 생성된 `.md` 를 저장할 곳 (Obsidian vault 로 열어도 됨).
5. **회고 요약 모델** — `claude` CLI 로 쓸 모델(없으면 자동 건너뜀).

macOS 라면 이어서 **매주 자동 실행(launchd) 등록** 여부와 실행 시각도 물어봅니다.
완료되면 `config.json` 이 생성됩니다(개인정보 포함 → `.gitignore` 로 커밋 제외).
템플릿은 `config.example.json` 참고.

> `--init` 을 다시 실행하면 기존 `config.json` 을 덮어쓸지 물어봅니다.
> 요일 하나만 바꾸고 싶으면 `config.json` 의 `week_start` 값만 직접 고쳐도 됩니다.

## 구조

```
weekly-log/
├── config.json               ← 개인 설정 (--init 이 생성, gitignore)
├── config.example.json       ← 배포용 설정 템플릿
├── 주간 작업 정리/           ← 생성된 주간 노트가 쌓이는 곳 (gitignore)
│   └── 2026-06-26~07-02 (금~목).md
├── scripts/
│   ├── generate_weekly.py    ← 커밋 수집 + 노트 생성 + --init 마법사
│   └── run_weekly.sh         ← launchd 실행 래퍼 (경로 자동 계산)
├── logs/                     ← 실행 로그 (gitignore, 60일 후 자동 삭제)
└── README.md
```

## 수집 사이클: 주 시작 요일 ~ +6일 (7일)

작업 사이클은 `--init` 에서 고른 **주 시작 요일부터 7일**입니다. (예: 금요일
시작 → `금~목`, 월요일 시작 → `월~일`)

- 기본 동작은 오늘 이전에 **'완료된' 가장 최근 사이클**을 정리합니다. 진행 중인
  오늘이 섞이지 않도록, 종료일이 오늘보다 과거인 사이클을 고릅니다.
- 자동 실행은 **주 시작 요일**에 돌도록 잡힙니다 — 그날이면 방금 끝난 지난 한
  주가 이미 완결돼 있어, 반쪽짜리(진행 중) 날 없이 깔끔하게 정리됩니다.

## 동작 방식

1. 설정한 **저장소 루트** 하위의 모든 git 저장소를 **자동 탐색** (깊이 2)
2. 해당 사이클에 **내 커밋**(설정한 작성자)이 있는 저장소만 선별
3. 프로젝트(저장소)별 → 날짜별로 커밋을 정리
4. 각 커밋에 연결된 **GitHub PR 링크**를 `([#번호](링크))` 형태로 추가 (`gh` CLI)
5. **"한 주 회고"를 LLM(`claude -p`)으로 자동 생성** (선택)
6. frontmatter + callout 이 포함된 마크다운 노트로 저장

저장소가 추가/삭제돼도 매 실행 시점에 다시 탐색하므로 목록을 손볼 필요가 없습니다.

### PR 링크 매칭 규칙

한 커밋이 여러 PR(스택형 브랜치)에 잡힐 수 있어 다음 우선순위로 하나를 고릅니다.

1. **머지된 PR 우선** — 가장 먼저 머지된 것(실제로 들어간 PR)
2. 전부 open 이면 — **번호가 가장 큰(최근 생성) PR**

`gh` CLI 인증이 필요하며(`gh auth status`), 없거나 `--no-pr` 사용 시 링크 없이
메시지만 출력합니다.

## 사용법

```bash
cd weekly-log/scripts

python3 generate_weekly.py --init                    # (최초 1회) 대화형 초기 설정
python3 generate_weekly.py                           # 완료된 최근 사이클 (예약 실행 기본값)
python3 generate_weekly.py --prev                    # 그 직전 사이클
python3 generate_weekly.py --window-start 2026-06-05 # 그 시작일부터 7일
python3 generate_weekly.py --date 2026-06-10         # 해당 날짜가 속한 사이클
python3 generate_weekly.py --dry-run                 # 저장 없이 미리보기
python3 generate_weekly.py --no-pr                   # PR 링크 조회 생략 (빠름/오프라인)
python3 generate_weekly.py --no-summary              # LLM 회고 요약 생략 (빠름/오프라인)
```

## 회고 자동 요약 (LLM, 선택)

`claude` CLI 를 **비대화형(`-p`) + 모든 도구 비활성화(`--tools ""`)** 로 호출해
"한 주 회고"를 자동 작성합니다.

- `--tools ""` 라서 claude 프로세스는 **텍스트 입출력만** 가능합니다. Bash·파일쓰기·
  웹접근이 구조적으로 불가능하므로 위험한 명령은 실행될 수 없습니다.
- git 커밋 수집은 결정론적으로 처리하고(LLM 미개입), LLM 은 수집된 커밋 텍스트를
  받아 요약 문단만 돌려줍니다 (하이브리드).
- 모델은 `config.json` 의 `summary_model`(기본 `sonnet`)로 조정합니다.
- claude 미설치/타임아웃/오류 시에는 요약을 건너뛰고, 커밋을 그대로 나열하는
  결정론적 폴백으로 노트를 생성합니다 (요약 실패가 노트 생성을 막지 않습니다).

## 설정 바꾸기

`config.json` 을 직접 수정하거나 `--init` 을 다시 실행합니다.

| 키 | 설명 |
| --- | --- |
| `week_start` | 주 시작 요일 (`월`~`일` 또는 `mon`~`sun`). 종료일·라벨·실행일 자동 파생 |
| `author_regex` | 내 커밋을 식별하는 작성자 정규식 (여러 개는 `\|` 로 구분) |
| `repos_root` | 저장소를 탐색할 루트 (`~` 확장 지원) |
| `vault_dir` | 노트를 저장할 폴더 |
| `notes_subdir` | 노트 하위 폴더명 (기본 `주간 작업 정리`) |
| `scan_maxdepth` | 저장소 탐색 깊이 (기본 2) |
| `summary_model` | 회고 요약 claude 모델 (기본 `sonnet`) |

## 자동 실행 (스케줄)

`--init` 이 OS 를 감지해 **매주 '주 시작 요일'의 지정 시각**에 자동 실행되도록
등록해 줍니다. 실행일이 주 시작 요일이라, 방금 끝난 지난 한 주를 정리합니다.

| OS | 등록 방식 | `--init` 자동 등록 |
| --- | --- | --- |
| macOS | launchd (LaunchAgent) | ✅ |
| Windows | 작업 스케줄러 (`schtasks`) | ✅ |
| Linux 등 | cron | ❌ (아래 수동 등록) |

### macOS (launchd)

`--init` 에서 등록을 선택하면 LaunchAgent(`net.weeklylog.generate`)가 설치됩니다.

| 파일 | 역할 |
| --- | --- |
| `scripts/run_weekly.sh` | launchd 가 호출하는 래퍼 (경로/PATH 자동 계산 + 로그 기록) |
| `~/Library/LaunchAgents/net.weeklylog.generate.plist` | 스케줄 정의 (`--init` 이 생성) |
| `logs/weekly-*.log` | 실행별 로그 (60일 후 자동 삭제) |

### 관리 명령

```bash
LABEL=net.weeklylog.generate
PLIST=~/Library/LaunchAgents/$LABEL.plist

# 다음 실행 시각/상태 확인
launchctl print gui/$(id -u)/$LABEL | grep -A4 -i calendar

# 지금 즉시 한 번 실행 (테스트)
launchctl kickstart -k gui/$(id -u)/$LABEL

# 일시 중지 / 재개
launchctl disable gui/$(id -u)/$LABEL
launchctl enable  gui/$(id -u)/$LABEL

# 완전 해제
launchctl bootout gui/$(id -u)/$LABEL
```

> 실행 요일/시각을 바꾸려면 `--init` 을 다시 실행하는 게 가장 안전합니다
> (요일이 사이클과 어긋나지 않게 함께 계산됩니다). 시스템 타임존 기준으로 동작합니다.

### Windows (작업 스케줄러)

`--init` 이 실행 시각(0-23시)을 물어본 뒤, `generate_weekly.py` 를 호출하는
배치 래퍼 `scripts/run_weekly.bat` 를 만들고 `schtasks` 로 **weekly-log** 라는
작업을 등록합니다. (bash 가 없어도 되며, python 이 PATH 에 있거나 `--init` 을
실행한 python 이 그대로 쓰입니다.)

```bat
:: 시각 변경 (예: 매주 정해진 요일 09:30)
schtasks /Change /TN weekly-log /ST 09:30

:: 상태 확인 / 즉시 실행 / 해제
schtasks /Query  /TN weekly-log /V /FO LIST
schtasks /Run    /TN weekly-log
schtasks /Delete /TN weekly-log /F

:: 수동 등록 예시 (금요일 09:00)
schtasks /Create /TN weekly-log /SC WEEKLY /D FRI /ST 09:00 ^
  /TR "C:\path\to\weekly-log\scripts\run_weekly.bat"
```

> 요일 약어: `MON TUE WED THU FRI SAT SUN`. 시각은 24시간제 `HH:MM`.
> Windows 는 실행 시각에 PC 가 꺼져 있으면 건너뛰므로, 작업 스케줄러 GUI 에서
> "예약된 시작 시간이 지난 후 최대한 빨리 작업 시작" 옵션을 켜두면 좋습니다.

### Linux 등 (cron)

`run_weekly.sh` 를 cron 에 등록하세요:

```bash
# 예: 매주 월요일 09시 (주 시작이 월요일일 때). cron 요일: 일=0 … 토=6
0 9 * * 1  /path/to/weekly-log/scripts/run_weekly.sh
```
