#!/bin/zsh
# 수집물 신선도를 한 번에 보고, 낡은 것만 갱신한다.
#
# 왜 이게 필요한가: 수집기를 종목마다 따로 부르면 왕복이 늘고, 각 수집기가
# 표를 20~30줄씩 뱉어서 그게 전부 대화 컨텍스트에 쌓인다. 오늘 실제로 그렇게
# 태웠다. 그래서 두 가지를 지킨다.
#
#   1. **기본 무음** — 수집기 출력은 data/logs/ 로 보내고 한 줄만 보고한다.
#   2. **한 번에 표** — 어느 파일이 낡았는지 확인하려고 여러 번 조회하지 않는다.
#
# 비동기 주의: 지금 단계가 그 데이터를 읽으면 async 로 띄우면 안 된다. 비결정적으로
# 낡은 값으로 판정하게 되고 이 프로젝트의 지문·as-of 규율이 무너진다. manifest 의
# reads_before_decision 이 그 구분이다.
#
#   ./scripts/refresh.sh check            # 표만. 아무것도 안 받는다
#   ./scripts/refresh.sh sync             # 낡은 것 중 판정에 필요한 것만 동기 갱신
#   ./scripts/refresh.sh async            # 판정에 안 쓰는 것들을 백그라운드로
#   ./scripts/refresh.sh sync price-IBM   # 이름 지정

set -e
cd "$(dirname "$0")/.."
MODE="${1:-check}"
shift 2>/dev/null || true
LOGDIR=data/logs
mkdir -p "$LOGDIR" knowledge/events

STALE=$(python3 - "$MODE" "$@" <<'PY'
import json, sys, time
from pathlib import Path

mode = sys.argv[1]
named = set(sys.argv[2:])
manifest = json.loads(Path("knowledge/manifest.json").read_text(encoding="utf-8"))
now = time.time()

rows, refresh = [], []
for art in manifest["artifacts"]:
    path = Path(art["path"])
    if not path.exists():
        age_h, state = None, "없음"
    else:
        age_h = (now - path.stat().st_mtime) / 3600
        state = "낡음" if age_h > art["max_age_hours"] else "최신"
    rows.append((art["name"], state, age_h, art["max_age_hours"],
                 art.get("reads_before_decision", True)))
    if state == "최신":
        continue
    if named and art["name"] not in named:
        continue
    if not named and mode == "sync" and not art.get("reads_before_decision", True):
        continue
    if not named and mode == "async" and art.get("reads_before_decision", True):
        continue
    refresh.append((art["name"], " ".join(art["command"])))

print(f"{'수집물':22s}{'상태':>6s}{'경과':>9s}{'허용':>9s}{'판정입력':>9s}",
      file=sys.stderr)
print("-" * 58, file=sys.stderr)
for name, state, age, limit, reads in rows:
    age_s = f"{age:,.1f}h" if age is not None else "—"
    print(f"{name:22s}{state:>6s}{age_s:>9s}{limit:>8}h{'예' if reads else '아니오':>9s}",
          file=sys.stderr)
print("-" * 58, file=sys.stderr)
stale = sum(1 for r in rows if r[1] != "최신")
print(f"낡음·없음 {stale}건 / 전체 {len(rows)}건", file=sys.stderr)

for name, cmd in refresh:
    print(f"{name}\t{cmd}")
PY
)

if [[ "$MODE" == "check" ]]; then
  exit 0
fi
if [[ -z "$STALE" ]]; then
  echo "갱신할 것 없음 ($MODE)"
  exit 0
fi

echo "$STALE" | while IFS=$'\t' read -r NAME CMD; do
  [[ -z "$NAME" ]] && continue
  LOG="$LOGDIR/${NAME}.log"
  if [[ "$MODE" == "async" ]]; then
    # 판정에 안 쓰는 것만 여기로 온다. 기다리지 않는다.
    ( eval "$CMD" > "$LOG" 2>&1 && echo "ok" > "$LOG.done" \
      || echo "fail" > "$LOG.done" ) &
    echo "  $NAME  백그라운드 시작 → $LOG"
  else
    if eval "$CMD" > "$LOG" 2>&1; then
      echo "  $NAME  갱신 완료"
    else
      echo "  $NAME  실패 — $LOG 확인" >&2
    fi
  fi
done

[[ "$MODE" == "async" ]] && echo "비동기 작업 진행 중. 결과는 $LOGDIR/*.log.done"
exit 0
