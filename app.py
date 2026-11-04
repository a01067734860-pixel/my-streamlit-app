from types import NoneType

# AI 개인비서 '에이블' (Streamlit)
# 실행: streamlit run app.py
# 같은 폴더에 필요한 파일:
#   - api_key.txt : OPENAI_API_KEY=..., GMAIL_ADDRESS=..., GMAIL_APP_PASSWORD=..., (선택) MY_EMAIL=...
#   - meeting.txt : (선택) 회의 분석 탭에서 기본으로 불러올 회의록
#
# 화면 구성
#   메인 탭  : 🤖 에이블 비서 (음성/텍스트 대화, 파일 첨부, 이메일 발송 도구, 음성 답변, 멀티턴, 대화 기록/새 채팅)
#   다른 탭  : 📊 대시보드(캘린더·주요 일정·해야 할 일) / 🎙️ 회의 분석(실시간 녹음·음성 파일·텍스트 파일)
#              / 📥 메일 비서(메일 발송·받은 메일 요약·연락처, 보내기 전 AI 확인)
#   사이드바 : 새 채팅·대화 기록 / 앞으로 3일 일정·할 일 마감(실시간 연동) / 최근 회의록 요약 / 최근 받은 메일 5개 요약
#   일정     : 종류별 색상·소요시간 추정(체크인 20/회의 30/온라인 45/고객사 60분), 겹치거나 간격 10분 미만이면
#              충돌 감지 → 대체 시간 3개 추천 → 승인(대체 시간/원래 시간/취소), 음성·텍스트 요청도 동일하게 검사
import os
import re
import json
import uuid
import hashlib
import html
import email
import shutil
import smtplib
import imaplib
import tempfile
import subprocess
import calendar as cal
import datetime as dt
from pathlib import Path
from email import policy
from email.mime.text import MIMEText
from email.utils import parseaddr, parsedate_to_datetime

import streamlit as st
from openai import OpenAI

# ─────────────────────────────────────────────
# 기본 설정
# ─────────────────────────────────────────────
# 노트북 셀에서 실행해도 오류가 나지 않도록 __file__ 이 없으면 현재 폴더 사용
BASE = Path(__file__).parent if "__file__" in globals() else Path.cwd()
DATA_FILE = BASE / "able_data.json"
MODEL = "gpt-4o-mini"
KST = dt.timezone(dt.timedelta(hours=9))
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]
PRIORITIES = ["높음", "보통", "낮음"]
PRIORITY_ORDER = {p: i for i, p in enumerate(PRIORITIES)}
PRIORITY_COLOR = {"높음": "#dc2626", "보통": "#d97706", "낮음": "#6b7280"}
# 일정 종류별 색상 · 기본 소요시간(분)
DEFAULT_EVENT_TYPES = {
    "내부 체크인": {"color": "#0d9488", "minutes": 20},
    "일반 회의": {"color": "#2563eb", "minutes": 30},
    "온라인 회의": {"color": "#7c3aed", "minutes": 45},
    "고객사 미팅": {"color": "#e11d48", "minutes": 60},
    "업무": {"color": "#d97706", "minutes": 60},
    "개인": {"color": "#16a34a", "minutes": 60},
    "기타": {"color": "#64748b", "minutes": 60},
}
# 할 일 분류별 색상 (일정과 다른 색)
DEFAULT_TODO_CATS = {"보고·문서": "#ea580c", "조사·분석": "#c026d3", "연락·메일": "#0891b2",
             "일반 업무": "#4f46e5", "개인": "#84cc16"}
LEGACY_CATS = {"회의": "일반 회의"}          # 이전 버전 분류 → 새 분류
GAP_MIN = 10                                 # 앞뒤 일정과 이 간격(분) 미만이면 충돌
WORK_START, WORK_END, LUNCH = 9 * 60, 18 * 60, (12 * 60, 13 * 60)   # 대체 시간 추천 범위
TIME_OPTIONS = ["종일"] + [f"{h:02d}:{m:02d}" for h in range(7, 22) for m in range(0, 60, 10)]
DURATIONS = {"자동 (종류별)": 0, "20분": 20, "30분": 30, "45분": 45, "1시간": 60,
             "1시간 30분": 90, "2시간": 120, "3시간": 180}

st.set_page_config(page_title="AI 개인비서 에이블", page_icon="🤖", layout="wide")


# ─────────────────────────────────────────────
# API 키 읽기 (노트북과 같은 api_key.txt 형식)
# ─────────────────────────────────────────────
def load_keys():
    key_file = BASE / "api_key.txt"
    if not key_file.exists():
        return
    for line in key_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_keys()


def get_client():
    # 키가 없으면 앱 전체를 멈추지 않고, 부른 곳에서 오류 메시지를 보여 주도록 예외를 냄
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY가 없습니다. api_key.txt를 확인하세요.")
    return OpenAI()


def gmail_ready():
    return bool(os.getenv("GMAIL_ADDRESS") and os.getenv("GMAIL_APP_PASSWORD"))


# ─────────────────────────────────────────────
# 데이터 저장/불러오기
# ─────────────────────────────────────────────
DATA_DEFAULTS = {"todos": [], "events": [], "meeting_notes": [], "mail_summaries": {},
                 "processed_mails": [], "settings": {}, "contacts": [], "chats": [],
                 "mail_analysis": {"when": "", "mails": []}}


def load_data():
    d = {}
    if DATA_FILE.exists():
        try:
            d = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            d = {}
    for k, v in DATA_DEFAULTS.items():
        d.setdefault(k, json.loads(json.dumps(v)))
    return d


def save_data():
    DATA_FILE.write_text(json.dumps(st.session_state.data, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_note(n):
    # 예전 버전(회의 녹음 탭)에서 저장한 회의록도 새 형식으로 읽을 수 있게 빈 항목을 채움
    n.setdefault("id", uuid.uuid4().hex[:8])
    n.setdefault("title", "회의")
    n.setdefault("when", "")
    n.setdefault("attendees", "")
    n.setdefault("source", "이전 버전 회의록")
    n.setdefault("text", n.get("transcript", ""))
    n.setdefault("minutes", "")
    for k in ("summary", "decisions", "kim_tasks", "schedules"):
        if not isinstance(n.get(k), list):
            n[k] = []
    for t in n["kim_tasks"]:
        for k, v in {"title": "", "detail": "", "requester": "", "due_text": "", "due_type": "none",
                     "due_date": "", "priority": "보통", "evidence": "", "status": "nodue", "todo_id": ""}.items():
            t.setdefault(k, v)
    for sc in n["schedules"]:
        for k, v in {"title": "", "when_text": "", "date_type": "none", "date": "", "evidence": "",
                     "verified": True}.items():
            sc.setdefault(k, v)
    return n


if "data" not in st.session_state:
    st.session_state.data = load_data()
data = st.session_state.data
data.setdefault("settings", {})

# 일정 종류 · 할 일 분류: 사용자가 직접 정한 값이 있으면 그것을, 없으면 기본값 사용
EVENT_TYPES = {k: {"color": v.get("color", "#64748b"), "minutes": int(v.get("minutes", 60))}
               for k, v in (data["settings"].get("event_types") or DEFAULT_EVENT_TYPES).items()}
TODO_CATS = dict(data["settings"].get("todo_cats") or DEFAULT_TODO_CATS)
EVENT_CATS = list(EVENT_TYPES)


def fallback_event_type():
    return "기타" if "기타" in EVENT_TYPES else EVENT_CATS[0]


def fallback_todo_cat():
    return "일반 업무" if "일반 업무" in TODO_CATS else list(TODO_CATS)[0]


def type_minutes(cat):
    return EVENT_TYPES.get(cat, {}).get("minutes", 60)


def type_color(cat):
    return EVENT_TYPES.get(cat, {}).get("color", "#64748b")


def type_guide():
    return ", ".join(f"{k} {v['minutes']}분" for k, v in EVENT_TYPES.items())
for k, v in DATA_DEFAULTS.items():
    data.setdefault(k, json.loads(json.dumps(v)))
data["meeting_notes"] = [normalize_note(x) for x in data["meeting_notes"] if isinstance(x, dict)]

today = dt.datetime.now(KST).date()   # 한국 시간 기준 (Colab 서버는 UTC)


def now_kst():
    return dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M")


def my_email():
    # 내 이메일 주소: 메일 비서에서 저장한 주소 → api_key.txt의 MY_EMAIL → 발송용 Gmail 주소 순서로 항상 적용
    return (data["settings"].get("my_email") or os.getenv("MY_EMAIL") or os.getenv("GMAIL_ADDRESS") or "").strip()

def parse_date(s):
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF⌀-⏿☀-➿⬀-⯿️‍⃣]")


def clean_text(t):
    # 등록되는 일정·할 일에는 이모티콘을 넣지 않음
    return re.sub(r"[ \t]+", " ", EMOJI_RE.sub("", t or "")).strip()


def _guess_todo_base(title):
    t = title or ""
    if re.search(r"보고|문서|초안|작성|기획안|제안서|자료|회의록", t):
        return "보고·문서"
    if re.search(r"조사|분석|리서치|검토|비교|리스트|사례", t):
        return "조사·분석"
    if re.search(r"메일|연락|전화|답장|회신|공유|전달", t):
        return "연락·메일"
    if re.search(r"병원|생일|휴가|연차|운동|가족|개인|여행", t):
        return "개인"
    return "일반 업무"


def guess_todo_cat(title):
    for name in TODO_CATS:
        if name not in DEFAULT_TODO_CATS and name in (title or ""):
            return name
    g = _guess_todo_base(title)
    return g if g in TODO_CATS else fallback_todo_cat()


def add_todo(title, due="", priority="보통", detail="", source="직접 입력", category=""):
    item = {
        "id": uuid.uuid4().hex[:8],
        "title": clean_text(title),
        "detail": clean_text(detail),
        "due": due if parse_date(due) else "",
        "priority": priority if priority in PRIORITIES else "보통",
        "category": category if category in TODO_CATS else guess_todo_cat(title),
        "done": False,
        "source": source,
    }
    data["todos"].append(item)
    save_data()
    return item


def update_todo(todo_id, title, due, priority, category, detail):
    t = next((x for x in data["todos"] if x["id"] == todo_id), None)
    if t is None:
        return {"status": "failed", "error": "할 일을 찾을 수 없습니다."}
    t.update(title=clean_text(title), due=due if parse_date(due) else "",
             priority=priority if priority in PRIORITIES else "보통",
             category=category if category in TODO_CATS else guess_todo_cat(title), detail=clean_text(detail))
    save_data()
    return dict(t, status="success")


def parse_time(s):
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(s or ""))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return h * 60 + mi if h < 24 and mi < 60 else None


def hhmm(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def end_from(start, minutes):
    s = parse_time(start)
    return hhmm(min(s + minutes, 24 * 60 - 1)) if s is not None and minutes else ""


def _guess_category_base(title):
    # 제목으로 일정 종류 추정 → 기본 소요시간이 정해짐
    t = title or ""
    if re.search(r"체크인|스탠드업|데일리|싱크|1:1|원온원", t, re.I):
        return "내부 체크인"
    if re.search(r"온라인|화상|줌|zoom|teams|팀즈|meet|웨비나|비대면", t, re.I):
        return "온라인 회의"
    if re.search(r"고객|거래처|클라이언트|파트너사|외부\s*미팅|업체|방문", t):
        return "고객사 미팅"
    if re.search(r"회의|미팅|면담|보고회|워크숍|세미나|간담회|브리핑", t):
        return "일반 회의"
    if re.search(r"병원|생일|약속|휴가|연차|반차|운동|가족|여행|결혼|개인", t):
        return "개인"
    return "업무"


def guess_category(title):
    for name in EVENT_TYPES:
        if name not in DEFAULT_EVENT_TYPES and name in (title or ""):
            return name
    g = _guess_category_base(title)
    return g if g in EVENT_TYPES else fallback_event_type()


def norm_times(start, end, category="기타"):
    # 끝나는 시간이 없으면 종류별 소요시간으로 추정
    s = parse_time(start)
    if s is None:
        return "", ""
    e = parse_time(end)
    if e is None or e <= s:
        e = min(s + type_minutes(category), 24 * 60 - 1)
    return hhmm(s), hhmm(e)


def event_span(e):
    s, en = parse_time(e.get("start")), parse_time(e.get("end"))
    if s is None:
        return None
    if en is None or en <= s:
        en = min(s + type_minutes(e.get("category")), 24 * 60 - 1)
    return s, en


def event_time_text(e):
    sp = event_span(e)
    return "종일" if sp is None else f"{hhmm(sp[0])}–{hhmm(sp[1])}"


def conflict_reason(a, b):
    # 겹치거나, 앞뒤 간격이 10분 미만이면 충돌
    if a[0] < b[1] and b[0] < a[1]:
        return "시간 겹침"
    gap = max(b[0] - a[1], a[0] - b[1])
    return f"간격 {gap}분" if gap < GAP_MIN else None


def find_conflicts(date, start="", end="", exclude_id=None):
    # 시간이 있는 일정끼리만 비교 (시간 없이 등록된 이전 일정은 그대로 두고 비교에서 제외)
    mine = event_span({"start": start, "end": end})
    if mine is None:
        return []
    out = []
    for e in data["events"]:
        if e["id"] == exclude_id or e["date"] != date:
            continue
        other = event_span(e)
        reason = conflict_reason(mine, other) if other else None
        if reason:
            out.append(dict(e, reason=reason))
    return out


def is_free(date_iso, s, e, exclude_id=None):
    for ev in data["events"]:
        if ev["id"] == exclude_id or ev["date"] != date_iso:
            continue
        sp = event_span(ev)
        if sp and conflict_reason((s, e), sp):
            return False
    return True


def suggest_slots(date_iso, start="", end="", exclude_id=None, n=3):
    # 충돌하지 않는 다른 시간 최대 3개: 같은 날(원래 시간과 가까운 순) → 이후 평일
    span = event_span({"start": start, "end": end})
    if span is None:
        return []
    dur = span[1] - span[0]
    base = parse_date(date_iso) or today
    first = max(base, today)
    now = dt.datetime.now(KST)
    out = []
    for i in range(14):
        d = first + dt.timedelta(days=i)
        if d.weekday() >= 5 and d != base:
            continue
        iso = d.isoformat()
        t = WORK_START
        if d == today:
            t = max(t, ((now.hour * 60 + now.minute) // 10 + 1) * 10)
        cands = []
        while t + dur <= WORK_END:
            if not (t < LUNCH[1] and LUNCH[0] < t + dur) and is_free(iso, t, t + dur, exclude_id):
                cands.append(t)
            t += 10
        cands.sort(key=lambda x: abs(x - span[0]))
        picked = []
        for c in cands:                    # 같은 날 추천끼리는 30분 이상 떨어지게
            if all(abs(c - p) >= 30 for p in picked):
                picked.append(c)
            if len(picked) >= (2 if d == base else 1):
                break
        out += [{"date": iso, "start": hhmm(p), "end": hhmm(p + dur)} for p in sorted(picked)]
        if len(out) >= n:
            break
    return out[:n]


def slot_text(s):
    return f"{fmt_date(parse_date(s['date']))} {s['start']}–{s['end']}"


def conflict_text(c):
    return f"{c['title']} ({event_time_text(c)}, {c['reason']})"


def build_event(title, date, start="", end="", category="", note=""):
    cat = LEGACY_CATS.get(category, category) if category not in EVENT_TYPES else category
    cat = cat if cat in EVENT_TYPES else guess_category(title)
    s, e = norm_times(start, end, cat)
    return {"title": clean_text(title), "date": str(date)[:10], "start": s, "end": e,
            "category": cat, "note": clean_text(note)}


def commit_event(fields, mode="add", event_id=None, source="직접 입력"):
    if mode == "edit":
        ev = next((e for e in data["events"] if e["id"] == event_id), None)
        if ev is None:
            return None
        ev.update(fields)
        save_data()
        return ev
    item = {"id": uuid.uuid4().hex[:8], **fields, "source": source}
    data["events"].append(item)
    save_data()
    return item


def request_event(fields, mode="add", event_id=None, source="직접 입력"):
    # 모든 일정 추가·수정은 여기서 충돌 검사 → 겹치지 않으면 바로 반영, 겹치면 김 대리 승인 대기
    if not fields["title"]:
        return {"status": "failed", "error": "일정 제목이 없습니다."}
    if not parse_date(fields["date"]):
        return {"status": "failed", "error": "날짜 형식은 YYYY-MM-DD 입니다."}
    conflicts = find_conflicts(fields["date"], fields["start"], fields["end"], exclude_id=event_id)
    if not conflicts:
        item = commit_event(fields, mode, event_id, source)
        if item is None:
            return {"status": "failed", "error": "일정을 찾을 수 없습니다."}
        return dict(item, status="success")
    sugg = suggest_slots(fields["date"], fields["start"], fields["end"], exclude_id=event_id)
    st.session_state.pending_event = {"mode": mode, "event_id": event_id, "fields": fields,
                                      "source": source, "suggestions": sugg}
    st.session_state.pending_toast = f"⚠️ '{fields['title']}' 일정이 기존 일정과 겹쳐요. 화면 위에서 시간을 골라 주세요."
    return {"status": "needs_approval", "title": fields["title"], "category": fields["category"],
            "requested": f"{fields['date']} {event_time_text(fields)}",
            "conflicts": [conflict_text(c) for c in conflicts],
            "suggestions": [{"no": i + 1, "time": slot_text(s)} for i, s in enumerate(sugg)],
            "message": "아직 등록하지 않았습니다. 대체 시간 선택(번호) / 원래 시간으로 등록 / 취소 중 김 대리의 결정이 필요합니다."}


def decide_pending(choice):
    # 사용자 승인: 대체 시간(1~3) / 원래 시간으로 등록(original) / 취소(cancel)
    p = st.session_state.get("pending_event")
    if not p:
        return {"status": "failed", "error": "승인을 기다리는 일정이 없습니다."}
    f = dict(p["fields"])
    if choice == "cancel":
        st.session_state.pop("pending_event", None)
        return {"status": "success", "decision": "취소", "title": f["title"]}
    if choice in ("1", "2", "3"):
        i = int(choice) - 1
        if i >= len(p["suggestions"]):
            return {"status": "failed", "error": f"{choice}번 대체 시간이 없습니다."}
        s = p["suggestions"][i]
        f.update(date=s["date"], start=s["start"], end=s["end"])
        if find_conflicts(f["date"], f["start"], f["end"], p["event_id"]):
            return {"status": "failed", "error": "그 사이 그 시간에 다른 일정이 생겼습니다. 다른 시간을 골라 주세요."}
    elif choice != "original":
        return {"status": "failed", "error": "1, 2, 3, original, cancel 중에서 골라 주세요."}
    item = commit_event(f, p["mode"], p["event_id"], p["source"])
    st.session_state.pop("pending_event", None)
    if item is None:
        return {"status": "failed", "error": "일정을 찾을 수 없습니다."}
    return {"status": "success", "decision": "원래 시간으로 등록" if choice == "original" else f"{choice}번 대체 시간",
            "title": item["title"], "date": item["date"], "time": event_time_text(item)}


def add_event(title, date, note="", source="직접 입력", start="", end="", category=""):
    return request_event(build_event(title, date, start, end, category, note), "add", None, source)


def update_event(event_id, title, date, start="", end="", category="", note=""):
    return request_event(build_event(title, date, start, end, category, note), "edit", event_id)


def move_event(event_id, date, start="", end=""):
    ev = next((e for e in data["events"] if e["id"] == event_id), None)
    if ev is None:
        return {"status": "failed", "error": "일정을 찾을 수 없습니다."}
    old = event_span(ev)
    if parse_time(start) is not None and parse_time(end) is None and old:
        end = end_from(start, old[1] - old[0])           # 시작만 바꾸면 원래 길이 유지
    return update_event(event_id, ev["title"], date, start, end, ev["category"], ev["note"])


def set_todo_done(todo_id, done=True):
    for t in data["todos"]:
        if t["id"] == todo_id:
            t["done"] = done
            save_data()
            return {"status": "success", "todo": t["title"]}
    return {"status": "failed", "error": f"id {todo_id} 를 찾을 수 없습니다."}


def delete_item(kind, item_id):
    data[kind] = [x for x in data[kind] if x["id"] != item_id]
    save_data()


def d_day(d):
    diff = (d - today).days
    if diff == 0:
        return "D-Day"
    return f"D-{diff}" if diff > 0 else f"D+{-diff}"


def fmt_date(d):
    return f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})"


def date_table():
    # '이번 주 목요일' 같은 표현을 LLM이 직접 계산하지 않고 표에서 찾아 쓰도록, 오늘 기준 날짜표를 매번 새로 만듦
    # (today가 실행할 때마다 바뀌므로 내년·연말에도 그 주에 맞는 날짜가 자동으로 들어감)
    monday = today - dt.timedelta(days=today.weekday())
    near = [f"{label} {(today + dt.timedelta(days=i)).isoformat()}({WEEKDAYS[(today + dt.timedelta(days=i)).weekday()]})"
            for i, label in enumerate(["오늘", "내일", "모레", "글피"])]
    lines = ["[날짜표] 한 주는 월요일~일요일입니다. 날짜는 직접 계산하지 말고 반드시 이 표에서 골라 씁니다.",
             "- " + ", ".join(near)]
    for w, label in enumerate(["지난 주", "이번 주", "다음 주", "다다음 주"], start=-1):
        days = [monday + dt.timedelta(days=7 * w + i) for i in range(7)]
        lines.append(f"- {label}: " + ", ".join(f"{WEEKDAYS[d.weekday()]} {d.isoformat()}" for d in days))
    lines += ["- '이번 주/다음 주' 없이 요일만 말하면(예: '목요일에') 오늘 이후(오늘 포함) 가장 가까운 그 요일입니다.",
              "- 시간에 오전/오후가 없을 때: 1~7시는 오후(13:00~19:00), 8~11시는 오전, 12시는 12:00으로 봅니다.",
              "  '오전', '새벽', '아침'이라고 말했을 때만 1~7시를 오전으로 씁니다."]
    return "\n".join(lines)


def check_weekday(date_iso, weekday):
    # 김 대리가 말한 요일과 도구에 넘어온 날짜의 요일이 다르면 등록하지 않고 다시 계산하게 함
    d = parse_date(date_iso)
    if not weekday or d is None or WEEKDAYS[d.weekday()] == weekday:
        return None
    return {"status": "failed", "retry": True,
            "error": f"{date_iso}는 {WEEKDAYS[d.weekday()]}요일인데 요청한 요일은 {weekday}요일입니다. "
                     "처리하지 않았습니다. [날짜표]에서 날짜를 다시 찾아 도구를 다시 호출하세요."}


# 예전 버전에서 저장한 일정·할 일·연락처·대화도 새 형식으로 읽기 (시간 없는 일정은 종일 일정으로 그대로 유지)
for _e in data["events"]:
    _e.setdefault("note", "")
    _e.setdefault("source", "")
    _cat = LEGACY_CATS.get(_e.get("category"), _e.get("category"))
    _e["category"] = _cat if _cat in EVENT_TYPES else guess_category(_e.get("title", ""))
    _e["start"], _e["end"] = norm_times(_e.get("start", ""), _e.get("end", ""), _e["category"])
for _t in data["todos"]:
    _t.setdefault("detail", "")
    _t.setdefault("source", "")
    if _t.get("category") not in TODO_CATS:
        _t["category"] = guess_todo_cat(_t.get("title", ""))
for _c in data["contacts"]:
    _c.setdefault("id", uuid.uuid4().hex[:8])
    for _k in ("name", "role", "team", "alias", "email", "memo"):
        _c.setdefault(_k, "")
if not isinstance(data.get("mail_analysis"), dict):
    data["mail_analysis"] = {"when": "", "mails": []}
data["mail_analysis"].setdefault("when", "")
data["mail_analysis"]["mails"] = [m for m in data["mail_analysis"].get("mails", [])
                                  if isinstance(m, dict) and isinstance(m.get("proposal"), dict)]
data["chats"] = [c for c in data["chats"] if isinstance(c, dict) and c.get("id")]
for _ch in data["chats"]:
    _ch.setdefault("title", "대화")
    _ch.setdefault("created", "")
    _ch.setdefault("updated", _ch["created"])
    _ch.setdefault("turns", [])
    for _t in _ch["turns"]:
        _t.setdefault("id", uuid.uuid4().hex[:8])
        for _k, _v in {"q": "", "a": "", "stt": None, "via": "text", "file": "", "actions": [],
                       "mail": None, "time": ""}.items():
            _t.setdefault(_k, _v)



# ─────────────────────────────────────────────
# 이메일 보내기 / 받기
# ─────────────────────────────────────────────
def send_email(to, subject, body, reply_to_id=""):
    sender = os.getenv("GMAIL_ADDRESS")
    password = os.getenv("GMAIL_APP_PASSWORD")
    if not sender or not password:
        return {"status": "failed", "error": "GMAIL_ADDRESS / GMAIL_APP_PASSWORD 가 설정되지 않았습니다."}
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    if reply_to_id and reply_to_id.startswith("<"):   # 답장이면 원래 메일과 같은 대화로 묶기
        msg["In-Reply-To"] = reply_to_id
        msg["References"] = reply_to_id
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.starttls()
            s.login(sender, password)
            s.sendmail(sender, to, msg.as_string())
        return {"status": "success"}
    except Exception as e:
        return {"status": "failed", "error": str(e)}


def mail_text(msg):
    # 메일 본문을 글자만 뽑기 (html이면 태그 제거)
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is None:
            return ""
        body = part.get_content()
        if part.get_content_type() == "text/html":
            body = re.sub(r"<(style|script).*?</\1>", " ", body, flags=re.S | re.I)
            body = re.sub(r"<[^>]+>", " ", body)
        return re.sub(r"\s+", " ", body).strip()
    except Exception:
        return ""


def parse_mail(raw_bytes, flags_line):
    msg = email.message_from_bytes(raw_bytes, policy=policy.default)
    try:
        a = msg["From"].addresses[0]
        name, addr = a.display_name, a.addr_spec
    except Exception:
        name, addr = parseaddr(str(msg.get("From", "")))
    try:
        reply = (msg["Reply-To"] or msg["From"]).addresses[0].addr_spec
    except Exception:
        reply = addr
    try:
        when = parsedate_to_datetime(msg["Date"]).astimezone(KST).strftime("%m/%d %H:%M")
    except Exception:
        when = ""
    subject = str(msg.get("Subject", "")).strip() or "(제목 없음)"
    body = mail_text(msg)[:2500]
    return {
        "subject": subject, "from": name or addr, "addr": reply, "date": when,
        "unread": b"\\Seen" not in flags_line,
        "msg_id": str(msg.get("Message-ID", "")).strip() or f"{subject}|{when}",
        "body": body,
    }


@st.cache_data(ttl=120, show_spinner=False)   # 2분 동안은 다시 불러오지 않음
def fetch_mails(n=5):
    try:
        with imaplib.IMAP4_SSL("imap.gmail.com") as imap:
            imap.login(os.getenv("GMAIL_ADDRESS"), os.getenv("GMAIL_APP_PASSWORD"))
            imap.select("INBOX", readonly=True)          # 읽기 전용: 읽음 표시가 바뀌지 않음
            _, found = imap.search(None, "ALL")
            mails = []
            for mid in found[0].split()[-n:][::-1]:      # 최신 n개
                _, parts = imap.fetch(mid, "(FLAGS BODY.PEEK[])")
                for p in parts:
                    if isinstance(p, tuple):
                        mails.append(parse_mail(p[1], p[0]))
                        break
            return {"mails": mails}
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────
# 음성 (STT / TTS)
# ─────────────────────────────────────────────
AUDIO_TYPES = ["m4a", "mp3", "wav", "webm", "mp4", "mpeg", "mpga", "ogg"]
STT_LIMIT = 24 * 1024 * 1024   # 음성 인식 API 파일 한도(25MB)보다 조금 작게


def shrink_audio(audio_bytes, name):
    # 25MB가 넘는 긴 녹음은 ffmpeg로 음성용 저용량 mp3로 변환 (Colab에는 ffmpeg가 기본 설치)
    if len(audio_bytes) <= STT_LIMIT or not shutil.which("ffmpeg"):
        return audio_bytes, name
    ext = name.rsplit(".", 1)[-1] if "." in name else "m4a"
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in." + ext)
        dst = os.path.join(tmp, "out.mp3")
        with open(src, "wb") as f:
            f.write(audio_bytes)
        subprocess.run(["ffmpeg", "-y", "-i", src, "-ac", "1", "-ar", "16000", "-b:a", "32k", dst],
                       capture_output=True)
        if os.path.exists(dst):
            with open(dst, "rb") as f:
                return f.read(), "meeting.mp3"
    return audio_bytes, name


def speech_to_text(audio_bytes, name="voice.wav", hint=None):
    kwargs = {"model": "whisper-1", "file": (name, audio_bytes), "language": "ko"}
    if hint:
        kwargs["prompt"] = hint
    return get_client().audio.transcriptions.create(**kwargs).text


def text_to_speech(text, voice="alloy"):
    return get_client().audio.speech.create(model="gpt-4o-mini-tts", voice=voice, input=text[:4000]).content


# ─────────────────────────────────────────────
# 지어내지 않기: 근거 문장 확인 + 김 대리 업무 반영 규칙
# ─────────────────────────────────────────────
def _squash(s):
    return re.sub(r"[\s\"'“”‘’.,!?…·:;()\[\]-]", "", s or "")


def found_in(evidence, source):
    # AI가 제시한 근거 문장이 실제 원문에 있는지 확인 (띄어쓰기·문장부호 차이는 무시)
    e, s = _squash(evidence), _squash(source)
    if len(e) < 4:
        return False
    return e in s or (len(e) >= 12 and (e[:12] in s or e[-12:] in s))


TASK_ITEM = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "업무 이름 (짧게)"},
        "detail": {"type": "string", "description": "원문에 있는 세부 내용만"},
        "requester": {"type": "string", "description": "업무를 맡긴 사람, 원문에 없으면 빈 문자열"},
        "due_text": {"type": "string", "description": "기한 표현 원문 그대로, 없으면 빈 문자열"},
        "due_type": {"type": "string", "enum": ["exact", "approximate", "none"]},
        "due_date": {"type": "string", "description": "due_type이 exact일 때만 YYYY-MM-DD, 아니면 빈 문자열"},
        "priority": {"type": "string", "enum": PRIORITIES},
        "evidence": {"type": "string", "description": "원문에서 그대로 복사한 근거 문장 1개"},
    },
    "required": ["title", "detail", "requester", "due_text", "due_type", "due_date", "priority", "evidence"],
    "additionalProperties": False,
}

TASK_RULES = [
    "- 원문에 실제로 있는 내용만 씁니다. 추측하거나 지어내지 않고, 모르면 빈 문자열이나 빈 배열로 둡니다.",
    "- kim_tasks에는 '김대리/김 대리'에게 맡겨졌다고 원문에 나온 업무만 넣습니다. 다른 사람 업무나 팀 전체 논의는 넣지 않습니다.",
    "- 같은 업무가 여러 번 나오면 하나로 합치고 세부 내용을 모읍니다.",
    "- evidence에는 그 업무가 김 대리에게 맡겨졌다는 근거 문장을 원문에서 글자 그대로 한 문장 복사합니다.",
    "- due_type: 날짜나 요일이 구체적이면 exact(예: 11월 14일까지, 이번 주 금요일까지),",
    "  '다음 주까지', '다음 달 초', '빨리', '출시 2주 전'처럼 대략적이면 approximate, 기한 언급이 없으면 none.",
    "- due_text는 원문 표현 그대로, due_date는 exact일 때만 [날짜표]에서 찾아 YYYY-MM-DD로 씁니다.",
    "- priority: 원문에 '중요', '꼭', '빨리', '급하게' 같은 표현이 있으면 높음, 없으면 보통.",
]


def apply_kim_tasks(tasks, source_text, source_label):
    # 김 대리 업무만 할 일에 추가. 정확한 날짜가 있는 업무만 캘린더(마감일)에 올리고,
    # 대략적인 기한은 날짜 없이 추가해 분석 화면에서 확인하게 함. 근거가 원문에 없으면 추가하지 않음.
    undone_titles = {t["title"].strip(): t["id"] for t in data["todos"] if not t["done"]}
    out = []
    for t in tasks:
        t = dict(t)
        title = t["title"].strip()
        if not title:
            continue
        if not found_in(t["evidence"], source_text):
            t.update(status="unverified", todo_id="")
        elif title in undone_titles:
            t.update(status="duplicate", todo_id=undone_titles[title])
        else:
            exact = t["due_type"] == "exact" and parse_date(t["due_date"])
            detail = t["detail"].strip()
            if t["requester"]:
                detail += f" / 요청: {t['requester']}"
            if t["due_text"] and not exact:
                detail += f" / 기한(대략): {t['due_text']}"
            item = add_todo(title, t["due_date"] if exact else "", t["priority"], detail.strip(" /"), source_label)
            undone_titles[title] = item["id"]
            t.update(status="calendar" if exact else ("approximate" if t["due_type"] == "approximate" else "nodue"),
                     todo_id=item["id"])
        out.append(t)
    return out


def task_status_line(t):
    d = parse_date(t.get("due_date"))
    return {
        "calendar": f"📅 캘린더에 추가됨 · 마감 {fmt_date(d) if d else ''}",
        "approximate": f"🕓 대략적인 기한 '{t['due_text']}' — 캘린더에는 넣지 않고 할 일 목록에만 추가했어요",
        "nodue": "📝 기한 언급 없음 — 할 일 목록에만 추가했어요",
        "duplicate": "↔️ 이미 할 일 목록에 있는 업무예요",
        "unverified": "⚠️ 원문에서 근거 문장을 찾지 못해 추가하지 않았어요",
    }.get(t.get("status"), "")


def status_report_text():
    lines = [f"[{today.isoformat()}] 에이블 업무 현황", ""]
    undone = sorted([t for t in data["todos"] if not t["done"]],
                    key=lambda t: (t["due"] or "9999", PRIORITY_ORDER[t["priority"]]))
    lines.append(f"■ 해야 할 일 ({len(undone)}건)")
    for t in undone:
        due = f" (마감 {t['due']}, {d_day(parse_date(t['due']))})" if t["due"] else ""
        lines.append(f" - [{t['priority']}] {t['title']}{due}")
        if t["detail"]:
            lines.append(f"     {t['detail']}")
    upcoming = sorted([e for e in data["events"] if parse_date(e["date"]) and parse_date(e["date"]) >= today],
                      key=lambda e: (e["date"], e["start"] or "00:00"))
    lines += ["", f"■ 다가오는 일정 ({len(upcoming)}건)"]
    for e in upcoming:
        lines.append(f" - {e['date']} {event_time_text(e)} [{e['category']}] {e['title']} ({d_day(parse_date(e['date']))})")
    return "\n".join(lines)

# ─────────────────────────────────────────────
# 연락처 · 메일 보내기 전 AI 확인
# ─────────────────────────────────────────────
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
SELF_WORDS = {"나", "내", "저", "제", "나에게", "내메일", "제메일", "김대리", "본인", "나한테"}
REVIEW_ITEMS = ["받는 사람", "내용 정확성", "빠진 내용", "말투·맞춤법", "민감 정보"]
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item": {"type": "string", "enum": REVIEW_ITEMS},
                    "ok": {"type": "boolean"},
                    "comment": {"type": "string"},
                },
                "required": ["item", "ok", "comment"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["checks", "summary"],
    "additionalProperties": False,
}


def latest_note():
    return data["meeting_notes"][0] if data["meeting_notes"] else None


def contact_label(c):
    label = f"{c['name']} {c['role']}".strip()
    return label + (f" ({c['team']})" if c["team"] else "")


def upsert_contact(name, email_addr, role="", team="", alias="", memo=""):
    email_addr = email_addr.strip()
    fields = {"name": name.strip(), "email": email_addr, "role": role.strip(), "team": team.strip(),
              "alias": alias.strip(), "memo": memo.strip()}
    c = next((x for x in data["contacts"] if x["email"].lower() == email_addr.lower()), None)
    if c:
        c.update({k: v for k, v in fields.items() if v})
    else:
        c = {"id": uuid.uuid4().hex[:8], **fields}
        data["contacts"].append(c)
    save_data()
    return c


def contact_keys(c):
    n = re.sub(r"\s", "", c["name"])
    r = re.sub(r"\s", "", c["role"])
    keys = {n, n + r, re.sub(r"\s", "", c["alias"])}
    if n and r:
        keys.add(n[0] + r)                 # 박민수 + 과장 → 박과장
    return {k for k in keys if k}


def resolve_recipient(to):
    # 받는 사람(이메일 주소 또는 연락처 이름) → 실제 주소. 찾지 못하면 주소를 지어내지 않고 이유를 돌려줌
    raw = (to or "").strip()
    m = EMAIL_RE.search(raw)               # "박과장 <park@x.com>" 형식도 허용
    if m:
        addr = m.group(0).strip("<>.,;:()\"'")
        c = next((x for x in data["contacts"] if x["email"].lower() == addr.lower()), None)
        is_me = addr.lower() == my_email().lower()
        return {"email": addr, "name": contact_label(c) if c else ("나(김 대리)" if is_me else ""), "contact": c}, ""
    key = re.sub(r"(님|씨|에게|께|한테)$", "", re.sub(r"\s", "", raw))
    if not key or key in SELF_WORDS:
        if not my_email():
            return None, "내 이메일 주소가 설정되지 않았습니다. 📥 메일 비서 탭에서 저장해 주세요."
        return {"email": my_email(), "name": "나(김 대리)", "contact": None}, ""
    hits = [c for c in data["contacts"] if key in contact_keys(c)]
    if not hits and len(key) >= 2:
        hits = [c for c in data["contacts"] if any(key in k for k in contact_keys(c))]
    if len(hits) == 1:
        return {"email": hits[0]["email"], "name": contact_label(hits[0]), "contact": hits[0]}, ""
    if len(hits) > 1:
        return None, (f"'{raw}'에 해당하는 연락처가 여러 명입니다: "
                      + ", ".join(f"{contact_label(c)} <{c['email']}>" for c in hits) + ". 누구에게 보낼지 알려 주세요.")
    return None, f"연락처에서 '{raw}'을(를) 찾지 못했습니다. 이메일 주소를 알려 주시거나 📇 연락처에 먼저 추가해 주세요."


def brief_context():
    # 메일 점검·대화에 쓰는 업무 자료 요약 (지어낸 내용인지 비교하는 기준)
    undone = [t for t in data["todos"] if not t["done"]]
    lines = ["[할 일]"] + [f"- {t['title']} (마감 {t['due'] or '없음'})" for t in undone[:20]]
    up = sorted([e for e in data["events"] if parse_date(e["date"]) and parse_date(e["date"]) >= today],
                key=lambda e: (e["date"], e["start"] or "00:00"))[:15]
    lines += ["[다가오는 일정]"] + [f"- {e['date']} {event_time_text(e)} {e['title']}" for e in up]
    note = latest_note()
    if note:
        lines += [f"[최근 회의] {note['title']}"] + [f"- {s}" for s in note["summary"]]
        lines += [f"- 김 대리 업무: {t['title']} (기한: {t['due_text'] or '언급 없음'})" for t in note["kim_tasks"]]
    return "\n".join(lines)


def review_email(rcpt, subject, body, context=""):
    # 1) 코드로 확인할 수 있는 것 + 2) AI가 한 번 더 점검
    checks = []
    if rcpt["contact"]:
        checks.append({"item": "연락처 확인", "ok": True,
                       "comment": f"연락처에 있는 {contact_label(rcpt['contact'])}님 주소예요."})
    elif rcpt["name"].startswith("나("):
        checks.append({"item": "연락처 확인", "ok": True, "comment": "김 대리님 본인 주소예요."})
    else:
        checks.append({"item": "연락처 확인", "ok": False,
                       "comment": "연락처에 없는 주소예요. 주소가 맞는지 확인해 주세요."})
    if not subject.strip():
        checks.append({"item": "제목", "ok": False, "comment": "제목이 비어 있어요."})
    summary = ""
    who = rcpt["name"] or "(이름 모름)"
    if rcpt["contact"]:
        c = rcpt["contact"]
        who += f" / 직함: {c['role'] or '-'} / 소속: {c['team'] or '-'} / 메모: {c['memo'] or '-'}"
    try:
        resp = get_client().responses.create(
            model=MODEL,
            instructions="\n".join([
                "당신은 '김 대리'의 개인 비서 '에이블'입니다. 김 대리가 메일을 보내기 전에 마지막으로 점검합니다.",
                "아래 다섯 항목을 모두 점검해 항목마다 ok(true/false)와 짧은 comment를 씁니다.",
                "- 받는 사람: [요청/상황]에서 의도한 사람과 [받는 사람] 정보가 맞는지. 요청에 받는 사람 언급이 없으면 ok.",
                "- 내용 정확성: 본문에 [요청/상황]·[참고 자료]에 없는 날짜·숫자·약속·사실이 들어갔는지. 지어낸 내용이 있으면 false.",
                "- 빠진 내용: 요청한 내용이 빠졌는지, '첨부'를 언급했는데 실제 첨부가 없는지(이 메일은 첨부를 보낼 수 없음).",
                "- 말투·맞춤법: 받는 사람에게 맞는 정중한 말투인지, 오탈자가 있는지.",
                "- 민감 정보: 비밀번호·주민번호·계좌번호 같은 민감 정보가 들어 있는지.",
                "비교할 자료가 없어 판단할 수 없으면 추측하지 말고 ok=true, comment에 '비교할 자료 없음'이라고 씁니다.",
                "summary: 김 대리에게 하는 한 문장 결론 (예: '보내도 괜찮아 보여요.' / '요청에 없는 날짜가 있어 확인이 필요해요.').",
            ]),
            input=(f"[받는 사람] {who} <{rcpt['email']}>\n\n[요청/상황]\n{context or '(없음)'}\n\n"
                   f"[제목] {subject}\n\n[본문]\n{body}"),
            text={"format": {"type": "json_schema", "name": "mail_review", "schema": REVIEW_SCHEMA, "strict": True}},
        )
        r = json.loads(resp.output_text)
        checks += r["checks"]
        summary = r["summary"]
    except Exception as e:
        checks.append({"item": "AI 확인", "ok": False, "comment": f"AI 확인을 하지 못했어요 ({e}). 직접 한 번 더 확인해 주세요."})
    ok = all(c["ok"] for c in checks)
    return {"email": rcpt["email"], "name": rcpt["name"], "ok": ok, "checks": checks,
            "summary": summary or ("보내도 괜찮아 보여요." if ok else "확인할 점이 있어요.")}


def show_review(rv):
    (st.success if rv["ok"] else st.warning)(("✅ " if rv["ok"] else "⚠️ ") + rv["summary"])
    for c in rv["checks"]:
        st.markdown(f"{'✅' if c['ok'] else '⚠️'} **{c['item']}** — {c['comment']}")


def confirm_and_send(key, to_raw, subject, body, context="", reply_to_id=""):
    # 보내기 전 AI 확인 → 김 대리가 확인 후 발송. 발송하면 "sent" 반환
    rcpt, err = resolve_recipient(to_raw)
    if err:
        st.error(err)
        return None
    st.caption(f"받는 사람: {rcpt['name'] + ' ' if rcpt['name'] else ''}<{rcpt['email']}>")
    sig = json.dumps([to_raw, subject, body], ensure_ascii=False)
    rv = st.session_state.get(f"rv_{key}")
    if rv and rv.get("sig") != sig:          # 내용을 고치면 다시 확인해야 함
        rv = None
    if rv is None:
        if st.button("🔍 AI 확인 후 보내기", key=f"rvbtn_{key}", type="primary",
                     disabled=not (subject.strip() and body.strip())):
            with st.spinner("에이블이 보내기 전에 한 번 더 확인하고 있어요..."):
                rv = review_email(rcpt, subject, body, context)
            rv["sig"] = sig
            st.session_state[f"rv_{key}"] = rv
    if rv:
        with st.container(border=True):
            st.markdown("**🔍 에이블의 발송 전 확인**")
            show_review(rv)
            a, b = st.columns(2)
            if a.button("✅ 확인했어요, 보내기" if rv["ok"] else "⚠️ 그래도 보내기", key=f"sendbtn_{key}",
                        type="primary", use_container_width=True):
                sr = send_email(rcpt["email"], subject, body, reply_to_id)
                if sr["status"] == "success":
                    st.session_state.pop(f"rv_{key}", None)
                    return "sent"
                st.error(f"발송 실패: {sr['error']}")
            if b.button("✏️ 고칠게요", key=f"fixbtn_{key}", use_container_width=True):
                st.session_state.pop(f"rv_{key}", None)
                st.rerun()
    return None


# ─────────────────────────────────────────────
# 캘린더 · 화면 스타일
# ─────────────────────────────────────────────
APP_CSS = [
    ".able-hero {padding:18px 22px; border-radius:16px; margin-bottom:10px;"
    " background:linear-gradient(135deg, rgba(62,123,250,.16), rgba(139,92,246,.12));"
    " border:1px solid rgba(128,128,128,.2);}",
    ".able-title {font-size:1.65rem; font-weight:700; line-height:1.3;}",
    ".able-sub {opacity:.75; font-size:.9rem; margin:2px 0 10px;}",
    ".able-chip {display:inline-block; font-size:.78rem; padding:3px 10px; margin:2px 6px 2px 0;"
    " border-radius:999px; background:rgba(128,128,128,.16);}",
    ".able-chip.warn {background:rgba(229,72,77,.2);}",
    ".able-badge {display:inline-block; padding:1px 8px; border-radius:999px; font-size:.72rem;"
    " font-weight:600; margin-right:4px; vertical-align:1px;}",
    ".able-meta {opacity:.75; font-size:.83rem;}",
    ".able-warn {color:#e5484d; font-size:.8rem;}",
    ".able-cal .pill {display:block; font-size:.7rem; line-height:1.35; margin-top:2px; padding:1px 4px;"
    " border-radius:4px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}",
    ".able-cal .pill.done {text-decoration:line-through; opacity:.45;}",
    ".able-cal .more {font-size:.68rem; opacity:.6; margin-top:2px;}",
    ".legend {margin-top:4px;} .legend span {font-size:.74rem; margin-right:10px; white-space:nowrap;}",
    ".cal-head {text-align:center; font-size:.8rem; font-weight:600; opacity:.8; padding-bottom:2px;}",
    ".cal-cell {height:58px; overflow:hidden; margin-top:-10px;}",
    ".cal-cell.other {opacity:.35;}",
    ".st-key-able_cal button {padding:0 4px; min-height:32px;}",
    ".st-key-able_cal [data-testid='stHorizontalBlock'] {gap:.35rem;}",
    "[class*='st-key-act_'] {gap:.3rem;}",
    "[class*='st-key-act_'] button {min-height:28px; padding:2px 6px;}",
    "[class*='st-key-act_'] button p {font-size:.76rem; white-space:nowrap;}",
    "[class*='st-key-chatdel_'] button {padding:0 !important; min-width:0 !important; min-height:38px;}",
    "[class*='st-key-chatdel_'] button p {font-size:1rem; line-height:1; margin:0;}",
]
st.markdown("<style>" + " ".join(APP_CSS) + "</style>", unsafe_allow_html=True)


def ev_color(e):
    return type_color(e["category"])


def badge(text, color):
    return (f'<span class="able-badge" style="color:{color}; background:{color}1f; '
            f'border:1px solid {color}66">{html.escape(text)}</span>')


def items_by_date():
    # 날짜별 캘린더 표시 항목: (정렬 키, 종류, 글자, 색) — 이모티콘 없이 분류 색으로 구분
    m = {}
    for e in data["events"]:
        d = parse_date(e["date"])
        if d:
            label = (e["start"] + " " if e["start"] else "") + e["title"]
            m.setdefault(d, []).append((e["start"] or "00:00", "event", label, ev_color(e)))
    for t in data["todos"]:
        d = parse_date(t["due"])
        if d:
            kind = "done" if t["done"] else "todo"
            color = "#9ca3af" if t["done"] else TODO_CATS.get(t["category"], "#64748b")
            m.setdefault(d, []).append(("99:99", kind, "마감 " + t["title"], color))
    for v in m.values():
        v.sort(key=lambda x: x[0])
    return m


def pill_html(kind, label, color):
    t = html.escape(label)
    line = "solid" if kind == "event" else "dashed"            # 할 일 마감은 점선
    return (f'<span class="pill {kind}" title="{t}" '
            f'style="border-left:3px {line} {color}; background:{color}24">{t}</span>')


def legend_html():
    ev = "".join(f'<span><b style="color:{v["color"]}">■</b> {k}</span>' for k, v in EVENT_TYPES.items())
    td = "".join(f'<span><b style="color:{c}">┆</b> {k}</span>' for k, c in TODO_CATS.items())
    return (f'<div class="legend"><span><b>일정</b></span>{ev}</div>'
            f'<div class="legend"><span><b>할 일 마감(점선)</b></span>{td}</div>')


def event_html(e, show_date=True):
    d = parse_date(e["date"])
    color = "#e5484d" if d <= today else ("#d97706" if (d - today).days <= 3 else "#2563eb")
    when = (f'<b style="color:{color}">{d_day(d)}</b> · {fmt_date(d)} ' if show_date else "") + event_time_text(e)
    out = (f'{badge(e["category"], ev_color(e))}<b>{html.escape(e["title"])}</b>'
           f'<br><span class="able-meta">{when}</span>')
    conf = find_conflicts(e["date"], e["start"], e["end"], e["id"])
    if conf:
        out += '<br><span class="able-warn">충돌: ' + html.escape(", ".join(conflict_text(c) for c in conf)) + "</span>"
    return out


def todo_html(t):
    d = parse_date(t["due"])
    title = html.escape(t["title"])
    if t["done"]:
        title = f"<s>{title}</s>"
    if d:
        color = "#9ca3af" if t["done"] else ("#e5484d" if d < today else "#d97706" if (d - today).days <= 3 else "#2563eb")
        due = f'<b style="color:{color}">{d_day(d)}</b> · 마감 {fmt_date(d)}'
    else:
        due = "기한 없음"
    return (f'{badge(t["category"], TODO_CATS.get(t["category"], "#64748b"))}'
            f'{badge(t["priority"], PRIORITY_COLOR[t["priority"]])}<b>{title}</b>'
            f'<br><span class="able-meta">{due}</span>')


def action_buttons(col, uid):
    # 수정·삭제 버튼: 작은 글씨로 세로로 놓아 좁은 화면에서도 글자가 잘리지 않게
    with col.container(key=f"act_{uid}"):
        edit = st.button("수정", key=f"ed_{uid}", use_container_width=True)
        delete = st.button("삭제", key=f"del_{uid}", use_container_width=True)
    return edit, delete


def time_index(options, value):
    return options.index(value) if value in options else 0


def event_form(key, ev=None, default_date=None):
    # 일정 추가/수정 폼. 제출하면 일정 정보(dict)를 돌려줌
    with st.form(key, clear_on_submit=ev is None):
        title = st.text_input("일정 제목", value=ev["title"] if ev else "")
        c1, c2 = st.columns(2)
        date = c1.date_input("날짜", value=parse_date(ev["date"]) if ev else (default_date or today))
        cat_opts = ["자동 (제목으로 추정)"] + EVENT_CATS
        cat = c2.selectbox("종류", cat_opts, index=cat_opts.index(ev["category"]) if ev and ev["category"] in cat_opts else 0)
        c3, c4 = st.columns(2)
        t_opts = list(TIME_OPTIONS)
        cur = (ev["start"] or "종일") if ev else "10:00"
        if cur not in t_opts:
            t_opts.insert(1, cur)
        stime = c3.selectbox("시작 시간", t_opts, index=time_index(t_opts, cur))
        d_opts = dict(DURATIONS)
        d_idx = 0
        if ev and event_span(ev):
            sp = event_span(ev)
            length = sp[1] - sp[0]
            match = [k for k, v in d_opts.items() if v == length]
            if not match:
                d_opts[f"지금 길이 ({length}분)"] = length
                match = [f"지금 길이 ({length}분)"]
            d_idx = list(d_opts).index(match[0])
        dur = c4.selectbox("소요 시간", list(d_opts), index=d_idx)
        note = st.text_input("메모", value=ev["note"] if ev else "")
        st.caption(f"소요 시간을 '자동'으로 두면 종류별로 계산해요 ({type_guide()}). "
                   "기존 일정과 겹치거나 간격이 10분 미만이면 알려 드려요.")
        if st.form_submit_button("저장" if ev else "추가", type="primary"):
            if not title.strip():
                st.warning("제목을 입력하세요.")
                return None
            s = "" if stime == "종일" else stime
            return {"title": title, "date": date.isoformat(), "start": s,
                    "end": end_from(s, d_opts[dur]), "category": "" if cat.startswith("자동") else cat, "note": note}
    return None


def todo_form(key, t=None, default_due=None):
    with st.form(key, clear_on_submit=t is None):
        title = st.text_input("할 일", value=t["title"] if t else "")
        c1, c2, c3 = st.columns(3)
        due = c1.date_input("마감일", value=(parse_date(t["due"]) if t else default_due))
        prio = c2.selectbox("우선순위", PRIORITIES, index=PRIORITIES.index(t["priority"]) if t else 1)
        cat_opts = ["자동 (제목으로 추정)"] + list(TODO_CATS)
        cat = c3.selectbox("분류", cat_opts, index=cat_opts.index(t["category"]) if t and t["category"] in cat_opts else 0)
        detail = st.text_input("세부 내용", value=t["detail"] if t else "")
        if st.form_submit_button("저장" if t else "추가", type="primary"):
            if not title.strip():
                st.warning("할 일을 입력하세요.")
                return None
            return {"title": title, "due": due.isoformat() if due else "", "priority": prio,
                    "category": "" if cat.startswith("자동") else cat, "detail": detail}
    return None


def _types_ver():
    return st.session_state.get("types_ver", 0)


def save_event_types(rows, new_row):
    # rows: (원래 이름, 새 이름, 색, 소요시간, 삭제 여부)
    keep = [(old, clean_text(nm), col, int(mins)) for old, nm, col, mins, rm in rows if not rm]
    if new_row and clean_text(new_row[0]):
        keep.append((None, clean_text(new_row[0]), new_row[1], int(new_row[2])))
    names = [k[1] for k in keep]
    if not keep:
        return "일정 종류는 하나 이상 있어야 해요."
    if any(not n for n in names):
        return "종류 이름을 비워 둘 수 없어요."
    if len(set(names)) != len(names):
        return "같은 이름의 종류가 있어요."
    rename = {old: nm for old, nm, _, _ in keep if old}
    new_types = {nm: {"color": col, "minutes": mins} for _, nm, col, mins in keep}
    fallback = "기타" if "기타" in new_types else names[0]
    for e in data["events"]:                         # 이름을 바꾸거나 지운 종류를 쓰던 일정도 함께 정리
        e["category"] = rename.get(e["category"], fallback)
    data["settings"]["event_types"] = new_types
    save_data()
    return ""


def save_todo_cats(rows, new_row):
    keep = [(old, clean_text(nm), col) for old, nm, col, rm in rows if not rm]
    if new_row and clean_text(new_row[0]):
        keep.append((None, clean_text(new_row[0]), new_row[1]))
    names = [k[1] for k in keep]
    if not keep:
        return "할 일 분류는 하나 이상 있어야 해요."
    if any(not n for n in names):
        return "분류 이름을 비워 둘 수 없어요."
    if len(set(names)) != len(names):
        return "같은 이름의 분류가 있어요."
    rename = {old: nm for old, nm, _ in keep if old}
    fallback = "일반 업무" if "일반 업무" in names else names[0]
    for t in data["todos"]:
        t["category"] = rename.get(t["category"], fallback)
    data["settings"]["todo_cats"] = {nm: col for _, nm, col in keep}
    save_data()
    return ""


def _reset_types(kind):
    data["settings"].pop("event_types" if kind == "event" else "todo_cats", None)
    defaults = DEFAULT_EVENT_TYPES if kind == "event" else DEFAULT_TODO_CATS
    fb = "기타" if kind == "event" else "일반 업무"
    for x in data["events" if kind == "event" else "todos"]:
        if x["category"] not in defaults:
            x["category"] = fb
    save_data()
    st.session_state.types_ver = _types_ver() + 1
    st.session_state.pending_toast = ("일정 종류" if kind == "event" else "할 일 분류") + "를 기본값으로 되돌렸어요."


def render_type_settings():
    # 일정 종류 · 할 일 분류를 사용자가 직접 정하기 (이름, 색, 일정 기본 소요시간)
    v = _types_ver()
    st.caption("여기서 정한 종류는 일정·할 일 추가/수정 화면, 캘린더 색상, 에이블 비서(음성·글 요청), 회의·메일 분석에 모두 쓰여요. "
               "종류를 지우면 그 종류의 일정은 '기타'(할 일은 '일반 업무', 없으면 첫 번째 종류)로 바뀌어요.")
    left, right = st.columns(2, gap="large")
    with left:
        st.markdown("**일정 종류** · 이름 / 색 / 기본 소요시간(분)")
        with st.form(f"ev_types_{v}"):
            rows = []
            for i, (name, info) in enumerate(EVENT_TYPES.items()):
                c1, c2, c3, c4 = st.columns([3, 1, 1.6, 1])
                nm = c1.text_input("이름", value=name, key=f"et_n_{v}_{i}", label_visibility="collapsed")
                col = c2.color_picker("색", value=info["color"], key=f"et_c_{v}_{i}", label_visibility="collapsed")
                mins = c3.number_input("분", min_value=5, max_value=600, step=5, value=int(info["minutes"]),
                                       key=f"et_m_{v}_{i}", label_visibility="collapsed")
                rm = c4.checkbox("삭제", key=f"et_d_{v}_{i}")
                rows.append((name, nm, col, mins, rm))
            st.caption("새 종류 추가")
            c1, c2, c3, _ = st.columns([3, 1, 1.6, 1])
            n_nm = c1.text_input("새 이름", key=f"et_new_n_{v}", placeholder="예: 교육", label_visibility="collapsed")
            n_col = c2.color_picker("새 색", value="#0ea5e9", key=f"et_new_c_{v}", label_visibility="collapsed")
            n_min = c3.number_input("새 분", min_value=5, max_value=600, step=5, value=60, key=f"et_new_m_{v}",
                                    label_visibility="collapsed")
            if st.form_submit_button("일정 종류 저장", type="primary"):
                err = save_event_types(rows, (n_nm, n_col, n_min))
                if err:
                    st.error(err)
                else:
                    st.session_state.types_ver = v + 1
                    st.session_state.pending_toast = "일정 종류를 저장했어요."
                    st.rerun()
        st.button("일정 종류 기본값으로", key=f"et_reset_{v}", on_click=_reset_types, args=("event",))
    with right:
        st.markdown("**할 일 분류** · 이름 / 색")
        with st.form(f"td_types_{v}"):
            rows = []
            for i, (name, color) in enumerate(TODO_CATS.items()):
                c1, c2, c3 = st.columns([3, 1, 1])
                nm = c1.text_input("이름", value=name, key=f"tc_n_{v}_{i}", label_visibility="collapsed")
                col = c2.color_picker("색", value=color, key=f"tc_c_{v}_{i}", label_visibility="collapsed")
                rm = c3.checkbox("삭제", key=f"tc_d_{v}_{i}")
                rows.append((name, nm, col, rm))
            st.caption("새 분류 추가")
            c1, c2, _ = st.columns([3, 1, 1])
            n_nm = c1.text_input("새 이름", key=f"tc_new_n_{v}", placeholder="예: 결재", label_visibility="collapsed")
            n_col = c2.color_picker("새 색", value="#14b8a6", key=f"tc_new_c_{v}", label_visibility="collapsed")
            if st.form_submit_button("할 일 분류 저장", type="primary"):
                err = save_todo_cats(rows, (n_nm, n_col))
                if err:
                    st.error(err)
                else:
                    st.session_state.types_ver = v + 1
                    st.session_state.pending_toast = "할 일 분류를 저장했어요."
                    st.rerun()
        st.button("할 일 분류 기본값으로", key=f"tc_reset_{v}", on_click=_reset_types, args=("todo",))


def _toggle_from_dialog(todo_id):
    done = st.session_state[f"dlg_chk_{todo_id}"]
    set_todo_done(todo_id, done)
    st.session_state[f"chk_{todo_id}"] = done  # 대시보드 목록의 체크박스도 같이 맞춤
    st.session_state.dlg_dirty = True           # 콜백 안에서는 rerun이 안 되므로 표시만 해 둠


def _refresh_day(d):
    # 팝업(fragment) 안에서 데이터가 바뀌면 앱 전체를 다시 실행해 사이드바·상단 칩·캘린더까지 반영하고,
    # 같은 날짜 팝업을 다시 열어 팝업이 닫히지 않은 것처럼 보이게 함
    st.session_state.dlg_reopen = d.isoformat()
    st.rerun()


@st.dialog("하루 일정", width="large")
def day_dialog(d):
    if st.session_state.pop("dlg_dirty", False):     # 팝업 안 체크박스로 완료 처리한 경우
        _refresh_day(d)
    iso = d.isoformat()
    st.markdown(f"### {d.year}년 {d.month}월 {d.day}일 ({WEEKDAYS[d.weekday()]}) · {d_day(d)}")

    events = sorted([e for e in data["events"] if e["date"] == iso], key=lambda e: e["start"] or "00:00")
    st.markdown(f"#### 일정 ({len(events)}건)")
    if not events:
        st.caption("등록된 일정이 없습니다.")
    for e in events:
        with st.container(border=True):
            if st.session_state.get("dlg_edit") == e["id"]:
                f = event_form(f"dlg_ev_edit_{e['id']}", e)
                if f:
                    r = update_event(e["id"], **f)
                    st.session_state.dlg_edit = None
                    if r["status"] == "needs_approval":
                        st.rerun()             # 충돌 → 창을 닫고 화면 위에서 시간 선택
                    _refresh_day(d)
                if st.button("취소", key=f"dlg_edit_cancel_{e['id']}"):
                    st.session_state.dlg_edit = None
                    st.rerun(scope="fragment")
            else:
                a, b = st.columns([6, 1])
                a.markdown(event_html(e, show_date=False), unsafe_allow_html=True)
                if e["note"]:
                    a.caption(e["note"])
                ed, dl = action_buttons(b, f"dlgev_{e['id']}")
                if ed:
                    st.session_state.dlg_edit = e["id"]
                    st.rerun(scope="fragment")
                if dl:
                    delete_item("events", e["id"])
                    _refresh_day(d)

    todos = [t for t in data["todos"] if t["due"] == iso]
    st.markdown(f"#### 이 날 마감인 할 일 ({len(todos)}건)")
    if not todos:
        st.caption("이 날 마감인 할 일이 없습니다.")
    for t in todos:
        with st.container(border=True):
            if st.session_state.get("dlg_edit_td") == t["id"]:
                f = todo_form(f"dlg_td_edit_{t['id']}", t)
                if f:
                    update_todo(t["id"], **f)
                    st.session_state.dlg_edit_td = None
                    _refresh_day(d)
                if st.button("취소", key=f"dlg_td_cancel_{t['id']}"):
                    st.session_state.dlg_edit_td = None
                    st.rerun(scope="fragment")
            else:
                a, b, c = st.columns([0.6, 6, 1])
                a.checkbox("완료", value=t["done"], key=f"dlg_chk_{t['id']}",
                           on_change=_toggle_from_dialog, args=(t["id"],), label_visibility="collapsed")
                b.markdown(todo_html(t), unsafe_allow_html=True)
                if t["detail"]:
                    b.caption(t["detail"])
                ed, dl = action_buttons(c, f"dlgtd_{t['id']}")
                if ed:
                    st.session_state.dlg_edit_td = t["id"]
                    st.rerun(scope="fragment")
                if dl:
                    delete_item("todos", t["id"])
                    _refresh_day(d)

    st.markdown("#### 이 날에 추가")
    kind = st.radio("종류", ["일정", "할 일(마감)"], horizontal=True, key=f"dlg_kind_{iso}",
                    label_visibility="collapsed")
    if kind == "일정":
        f = event_form(f"dlg_add_ev_{iso}", default_date=d)
        if f:
            r = add_event(f["title"], f["date"], f["note"], "직접 입력", f["start"], f["end"], f["category"])
            if r["status"] == "needs_approval":
                st.rerun()
            _refresh_day(d)
    else:
        f = todo_form(f"dlg_add_td_{iso}", default_due=d)
        if f:
            add_todo(f["title"], f["due"], f["priority"], f["detail"], "직접 입력", f["category"])
            _refresh_day(d)

    st.divider()
    if st.button("닫기", type="primary", use_container_width=True):
        st.rerun()


def render_calendar(year, month):
    m = items_by_date()
    weeks = cal.Calendar(firstweekday=6).monthdatescalendar(year, month)  # 일요일 시작

    head = st.columns(7)
    for i, w in enumerate(["일", "월", "화", "수", "목", "금", "토"]):
        color = "#e5484d" if i == 0 else "#3e7bfa" if i == 6 else "inherit"
        head[i].markdown(f'<div class="cal-head" style="color:{color}">{w}</div>', unsafe_allow_html=True)

    reopen = st.session_state.pop("dlg_reopen", None)     # 팝업에서 바꾼 뒤 같은 날짜 팝업 다시 열기
    with st.container(key="able_cal"):
        for week in weeks:
            cols = st.columns(7)
            for i, d in enumerate(week):
                with cols[i]:
                    items = m.get(d, [])
                    clicked = st.button(str(d.day), key=f"day_{d.isoformat()}",
                                        type="primary" if d == today else "secondary",
                                        use_container_width=True,
                                        help=f"{d.month}월 {d.day}일 일정 보기·추가·수정·삭제")
                    if clicked or reopen == d.isoformat():
                        day_dialog(d)
                    pills = "".join(pill_html(k, lb, c) for _, k, lb, c in items[:2])
                    if len(items) > 2:
                        pills += f'<div class="more">+{len(items) - 2}건</div>'
                    other = " other" if d.month != month else ""
                    st.markdown(f'<div class="able-cal cal-cell{other}">{pills}</div>', unsafe_allow_html=True)

    st.markdown(legend_html(), unsafe_allow_html=True)
    st.caption("날짜를 누르면 그 날 일정을 보고 추가·수정·삭제할 수 있어요.")


def count_overlaps():
    n = 0
    for e in data["events"]:
        d = parse_date(e["date"])
        if d and d >= today and find_conflicts(e["date"], e["start"], e["end"], e["id"]):
            n += 1
    return n


# ─────────────────────────────────────────────
# 자연어로 일정 삭제: 대상을 보여 주고 김 대리가 승인한 뒤에만 삭제
# ─────────────────────────────────────────────
DELETE_MAX = 5
DELETE_STOP = {"일정", "스케줄", "거", "것", "그거", "약속잡은거"}


def _nospace(s):
    return re.sub(r"\s", "", s or "").lower()


def find_delete_candidates(date_iso, keyword):
    # 날짜(없으면 오늘 이후 전체) 안에서 키워드가 제목·메모·종류에 많이 들어간 일정 순으로 최대 5개
    words = [_nospace(w) for w in re.split(r"[\s,]+", keyword or "") if w and _nospace(w) not in DELETE_STOP]
    if date_iso:
        pool = [e for e in data["events"] if e["date"] == date_iso]
    else:
        pool = [e for e in data["events"] if (d := parse_date(e["date"])) and d >= today]
        if not words:
            return []
    scored = []
    for e in pool:
        text = _nospace(e["title"] + " " + e.get("note", "") + " " + e["category"])
        score = sum(w in text for w in words)
        if score or not words:
            scored.append((-score, e["date"], e["start"] or "00:00", e))
    scored.sort(key=lambda x: x[:3])
    if scored and words:                         # 키워드가 가장 많이 맞는 일정만 후보로 (예: '점심 약속' > '팀 점심')
        scored = [x for x in scored if x[0] == scored[0][0]]
    return [x[3] for x in scored[:DELETE_MAX]]


def request_delete(date_iso, keyword):
    # 바로 지우지 않고 삭제 후보를 승인 대기(pending_delete)로 저장
    if date_iso and not parse_date(date_iso):
        return {"status": "failed", "error": "날짜 형식은 YYYY-MM-DD 입니다."}
    cands = find_delete_candidates(date_iso, keyword)
    if not cands:
        same_day = sorted([e for e in data["events"] if e["date"] == date_iso], key=lambda e: e["start"] or "00:00")
        return {"status": "failed",
                "error": f"'{keyword or date_iso}'에 해당하는 일정을 찾지 못했습니다. 아무것도 삭제하지 않았습니다.",
                "events_on_date": [f"{e['title']} ({event_time_text(e)})" for e in same_day]}
    st.session_state.pending_delete = {"ids": [e["id"] for e in cands], "query": keyword or date_iso}
    st.session_state.pending_toast = "⚠️ 삭제할 일정을 화면 위 카드에서 확인해 주세요. 승인하기 전에는 지우지 않아요."
    return {"status": "needs_approval", "kind": "delete",
            "candidates": [{"no": i + 1, "title": e["title"], "category": e["category"],
                            "when": f"{fmt_date(parse_date(e['date']))} {event_time_text(e)}"}
                           for i, e in enumerate(cands)],
            "message": "아직 삭제하지 않았습니다. 삭제 대상을 보여 주고 김 대리의 승인(번호/전부/취소)을 기다립니다."}


def pending_delete_events():
    p = st.session_state.get("pending_delete")
    if not p:
        return []
    by_id = {e["id"]: e for e in data["events"]}
    return [by_id[i] for i in p["ids"] if i in by_id]


def decide_pending_delete(choice):
    # 김 대리 승인: 번호(1~5) / all(후보 전부) / cancel(삭제하지 않음)
    evs = pending_delete_events()
    if not st.session_state.get("pending_delete"):
        return {"status": "failed", "error": "삭제 승인을 기다리는 일정이 없습니다."}
    if choice == "cancel" or not evs:
        st.session_state.pop("pending_delete", None)
        return {"status": "success", "decision": "취소", "deleted": []}
    if choice == "all":
        targets = evs
    elif choice.isdigit() and 1 <= int(choice) <= len(evs):
        targets = [evs[int(choice) - 1]]
    else:
        return {"status": "failed", "error": f"{choice}번 일정이 없습니다. 1~{len(evs)}, all, cancel 중에서 골라 주세요."}
    for e in targets:
        delete_item("events", e["id"])
    st.session_state.pop("pending_delete", None)
    return {"status": "success", "decision": "삭제",
            "deleted": [f"{e['title']} ({fmt_date(parse_date(e['date']))} {event_time_text(e)})" for e in targets]}


def _delete_cb(choice):
    r = decide_pending_delete(choice)
    if r["status"] != "success":
        st.session_state.pending_toast = f"⚠️ {r['error']}"
    elif r["decision"] == "취소":
        st.session_state.pending_toast = "일정 삭제를 취소했어요. 일정은 그대로 있어요."
    else:
        st.session_state.pending_toast = "일정을 삭제했어요: " + ", ".join(r["deleted"])


def render_pending_delete():
    if not st.session_state.get("pending_delete"):
        return
    evs = pending_delete_events()
    if not evs:                                  # 그 사이 다른 곳에서 지워졌으면 카드 닫기
        st.session_state.pop("pending_delete", None)
        return
    with st.container(border=True):
        st.error(f"🗑️ 일정 삭제 확인 — '{st.session_state.pending_delete['query']}'에 해당하는 일정 {len(evs)}건")
        st.caption("승인하기 전에는 삭제하지 않아요. 지울 일정을 골라 주세요. 에이블 비서에게 '1번 지워줘', '취소'라고 말해도 돼요.")
        for i, e in enumerate(evs):
            a, b = st.columns([5, 1.3])
            a.markdown(f"**{i + 1}.** " + event_html(e), unsafe_allow_html=True)
            b.button("🗑️ 삭제", key=f"pd_del_{e['id']}", on_click=_delete_cb, args=(str(i + 1),), type="primary",
                     use_container_width=True)
        c1, c2, _ = st.columns([1.4, 1, 3])
        if len(evs) > 1:
            c1.button(f"모두 삭제 ({len(evs)}건)", key="pd_all", on_click=_delete_cb, args=("all",),
                      use_container_width=True)
        c2.button("취소", key="pd_cancel", on_click=_delete_cb, args=("cancel",), use_container_width=True)


def _decide_cb(choice):
    r = decide_pending(choice)
    if r["status"] == "success":
        st.session_state.pending_toast = (f"'{r['title']}' 일정 등록을 취소했어요." if r["decision"] == "취소"
                                          else f"'{r['title']}' 일정을 {r['date']} {r['time']}에 등록했어요.")
    else:
        st.session_state.pending_toast = f"⚠️ {r['error']}"


def render_pending_event():
    # 일정 충돌 → 사용자 승인: 대체 시간 선택 / 원래 시간으로 등록 / 취소
    p = st.session_state.get("pending_event")
    if not p:
        return
    f = p["fields"]
    conflicts = find_conflicts(f["date"], f["start"], f["end"], p["event_id"])
    with st.container(border=True):
        what = "수정" if p["mode"] == "edit" else "추가"
        st.warning(f"⚠️ 일정 충돌 감지 — {what}하려는 '{f['title']}' "
                   f"({fmt_date(parse_date(f['date']))} {event_time_text(f)}, {f['category']})")
        if conflicts:
            st.markdown("<br>".join(f"{badge(c['category'], ev_color(c))}{html.escape(c['title'])} · "
                                    f"{event_time_text(c)} — <b>{c['reason']}</b>" for c in conflicts),
                        unsafe_allow_html=True)
            st.caption("기존 일정과 겹치거나 앞뒤 간격이 10분 미만이에요. 어떻게 할까요?")
        else:
            st.caption("지금은 겹치는 일정이 없어졌어요. 원래 시간으로 등록할 수 있어요.")
        sugg = p["suggestions"]
        if sugg:
            st.markdown("**대체 시간 추천** · 업무 시간 9~18시, 점심 12~13시 제외, 앞뒤 10분 여유")
            cols = st.columns(len(sugg))
            for i, s in enumerate(sugg):
                ok = is_free(s["date"], parse_time(s["start"]), parse_time(s["end"]), p["event_id"])
                cols[i].button(f"{i + 1}. {slot_text(s)}", key=f"pe_s{i}", on_click=_decide_cb, args=(str(i + 1),),
                               use_container_width=True, disabled=not ok, type="primary" if i == 0 else "secondary")
        else:
            st.caption("2주 안에서 충돌하지 않는 업무 시간을 찾지 못했어요.")
        a, b, _ = st.columns([1.4, 1, 3])
        a.button("원래 시간으로 등록", key="pe_orig", on_click=_decide_cb, args=("original",), use_container_width=True)
        b.button("취소", key="pe_cancel", on_click=_decide_cb, args=("cancel",), use_container_width=True)


# ─────────────────────────────────────────────
# 화면
# ─────────────────────────────────────────────
if st.session_state.get("pending_toast"):
    _msg = st.session_state.pop("pending_toast")
    _warn = _msg.startswith("⚠️")
    st.toast(_msg.lstrip("⚠️ ") if _warn else _msg, icon="⚠️" if _warn else "✅")

_undone = [t for t in data["todos"] if not t["done"]]
_chips = [f"오늘 일정 {sum(e['date'] == today.isoformat() for e in data['events'])}건",
          f"오늘 마감 {sum(t['due'] == today.isoformat() for t in _undone)}건",
          f"남은 할 일 {len(_undone)}건"]
_chip_html = "".join(f'<span class="able-chip">{html.escape(c)}</span>' for c in _chips)
_ov = count_overlaps()
if _ov:
    _chip_html += f'<span class="able-chip warn">충돌 일정 {_ov}건</span>'
st.markdown(
    f'<div class="able-hero"><div class="able-title">🤖 AI 개인비서 에이블</div>'
    f'<div class="able-sub">김 대리님의 업무를 돕는 음성 AI 비서 · {today.year}년 {today.month}월 {today.day}일 '
    f'{WEEKDAYS[today.weekday()]}요일</div>{_chip_html}</div>',
    unsafe_allow_html=True)

render_pending_event()
render_pending_delete()

tab_chat, tab_dash, tab_meet, tab_mail = st.tabs(["🤖 에이블 비서", "📊 대시보드", "🎙️ 회의 분석", "📥 메일 비서"])

# ===== 대시보드 =====
with tab_dash:
    undone = [t for t in data["todos"] if not t["done"]]
    week_end = today + dt.timedelta(days=6 - today.weekday())
    due_this_week = [t for t in undone if (d := parse_date(t["due"])) and today <= d <= week_end]
    overdue = [t for t in undone if (d := parse_date(t["due"])) and d < today]
    today_events = [e for e in data["events"] if parse_date(e["date"]) == today]

    c1, c2, c3 = st.columns(3)
    c1.metric("오늘 일정", f"{len(today_events)}건")
    c2.metric("남은 할 일", f"{len(undone)}건",
              delta=f"기한 지남 {len(overdue)}건" if overdue else None, delta_color="inverse")
    c3.metric("이번 주 마감", f"{len(due_this_week)}건")

    with st.expander("⚙️ 일정 종류 · 할 일 분류 직접 정하기"):
        render_type_settings()

    left, right = st.columns([3, 2], gap="large")

    # --- 캘린더 ---
    with left:
        if "cal_ym" not in st.session_state:
            st.session_state.cal_ym = (today.year, today.month)
        y, mth = st.session_state.cal_ym
        n1, n2, n3, n4 = st.columns([1, 4, 1, 1])
        if n1.button("◀", key="prev_m"):
            st.session_state.cal_ym = (y - 1, 12) if mth == 1 else (y, mth - 1)
            st.rerun()
        n2.markdown(f"### {y}년 {mth}월")
        if n3.button("오늘", key="today_m"):
            st.session_state.cal_ym = (today.year, today.month)
            st.rerun()
        if n4.button("▶", key="next_m"):
            st.session_state.cal_ym = (y + 1, 1) if mth == 12 else (y, mth + 1)
            st.rerun()
        render_calendar(y, mth)

    # --- 오른쪽: 주요 일정 / 할 일 목록 (나란히 놓고 골라 보기) ---
    with right:
        n_up = sum(1 for e in data["events"] if (d := parse_date(e["date"])) and today <= d <= today + dt.timedelta(days=30))
        right_view = st.radio("보기 선택", ["주요 일정", "할 일 목록"], horizontal=True, key="dash_right",
                              label_visibility="collapsed",
                              format_func=lambda v: f"{v} ({n_up if v == '주요 일정' else len(undone)})")

        if right_view == "주요 일정":
            horizon = today + dt.timedelta(days=30)
            upcoming = sorted([e for e in data["events"] if (d := parse_date(e["date"])) and today <= d <= horizon],
                              key=lambda e: (e["date"], e["start"] or "00:00"))
            if not upcoming:
                st.info("앞으로 30일 안에 예정된 일정이 없습니다.")
            for e in upcoming:
                with st.container(border=True):
                    if st.session_state.get("edit_ev") == e["id"]:
                        st.caption(f"일정 수정 · 등록: {e['source'] or '직접 입력'}")
                        f = event_form(f"ev_edit_{e['id']}", e)
                        if f:
                            r = update_event(e["id"], **f)
                            st.session_state.edit_ev = None
                            if r["status"] == "success":
                                st.session_state.pending_toast = f"'{r['title']}' 일정을 수정했어요."
                            st.rerun()
                        if st.button("수정 취소", key=f"ev_edit_cancel_{e['id']}"):
                            st.session_state.edit_ev = None
                            st.rerun()
                    else:
                        a, b = st.columns([5, 1])
                        a.markdown(event_html(e), unsafe_allow_html=True)
                        if e["note"]:
                            a.caption(e["note"])
                        if e["source"] in ("에이블", "회의 분석") or e["source"].startswith("메일"):
                            a.caption(f"AI 등록 ({e['source']}) · 수정할 수 있어요")
                        ed, dl = action_buttons(b, f"ev_{e['id']}")
                        if ed:
                            st.session_state.edit_ev = e["id"]
                            st.rerun()
                        if dl:
                            delete_item("events", e["id"])
                            st.rerun()
            with st.expander("일정 추가"):
                f = event_form("add_event")
                if f:
                    r = add_event(f["title"], f["date"], f["note"], "직접 입력", f["start"], f["end"], f["category"])
                    if r["status"] == "success":
                        st.session_state.pending_toast = f"'{r['title']}' 일정을 {event_time_text(r)}에 등록했어요."
                    st.rerun()

        else:
            view = st.radio("보기", ["진행 중", "완료", "전체"], horizontal=True, key="td_view",
                            label_visibility="collapsed")

            def toggle(todo_id):
                set_todo_done(todo_id, st.session_state[f"chk_{todo_id}"])

            todos = data["todos"]
            if view == "진행 중":
                todos = [t for t in todos if not t["done"]]
            elif view == "완료":
                todos = [t for t in todos if t["done"]]
            todos = sorted(todos, key=lambda t: (t["done"], t["due"] or "9999", PRIORITY_ORDER[t["priority"]]))
            if not todos:
                st.info("표시할 할 일이 없습니다. '회의 분석'이나 '메일 비서'에서 업무를 가져오거나 직접 추가해 보세요.")
            for t in todos:
                with st.container(border=True):
                    if st.session_state.get("edit_td") == t["id"]:
                        st.caption(f"할 일 수정 · 등록: {t['source'] or '직접 입력'}")
                        f = todo_form(f"td_edit_{t['id']}", t)
                        if f:
                            update_todo(t["id"], **f)
                            st.session_state.edit_td = None
                            st.session_state.pending_toast = f"'{clean_text(f['title'])}' 할 일을 수정했어요."
                            st.rerun()
                        if st.button("수정 취소", key=f"td_edit_cancel_{t['id']}"):
                            st.session_state.edit_td = None
                            st.rerun()
                    else:
                        c0, c1, c2 = st.columns([0.5, 5, 1])
                        c0.checkbox("완료", value=t["done"], key=f"chk_{t['id']}", on_change=toggle,
                                    args=(t["id"],), label_visibility="collapsed")
                        c1.markdown(todo_html(t), unsafe_allow_html=True)
                        if t["detail"]:
                            c1.caption(t["detail"])
                        if t["source"] not in ("직접 입력", ""):
                            c1.caption(f"AI 등록 ({t['source']}) · 수정할 수 있어요")
                        ed, dl = action_buttons(c2, f"td_{t['id']}")
                        if ed:
                            st.session_state.edit_td = t["id"]
                            st.rerun()
                        if dl:
                            delete_item("todos", t["id"])
                            st.rerun()
            with st.expander("할 일 추가"):
                f = todo_form("add_todo")
                if f:
                    add_todo(f["title"], f["due"], f["priority"], f["detail"], "직접 입력", f["category"])
                    st.rerun()

# ===== 에이블 비서 도구 (이메일 발송 · 할 일/일정 관리) =====
TOOLS = [
    {
        "type": "function", "name": "send_email",
        "description": "이메일을 보냅니다. 사용자가 이메일/메일로 보내 달라고 명시적으로 요청할 때만 사용합니다. "
                       "보내기 전에 에이블의 확인 단계를 거칩니다.",
        "parameters": {
            "type": "object",
            "properties": {
                "to": {"type": "string",
                       "description": "받는 사람 이메일 주소 또는 연락처에 있는 이름(예: 박과장). 김 대리 본인이면 '나'"},
                "subject": {"type": "string", "description": "이메일 제목"},
                "body": {"type": "string", "description": "이메일 본문"},
            },
            "required": ["to", "subject", "body"], "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function", "name": "add_todo",
        "description": "대시보드의 할 일 목록에 새 할 일을 추가합니다.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "할 일 제목"},
                "due": {"type": "string", "description": "마감일 YYYY-MM-DD, 없으면 빈 문자열"},
                "priority": {"type": "string", "enum": PRIORITIES},
                "detail": {"type": "string", "description": "세부 내용, 없으면 빈 문자열"},
                "category": {"type": "string", "enum": list(TODO_CATS), "description": "할 일 분류"},
            },
            "required": ["title", "due", "priority", "detail", "category"], "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function", "name": "add_event",
        "description": "캘린더에 일정을 추가합니다. 기존 일정과 겹치거나 간격이 10분 미만이면 등록하지 않고 "
                       "status=needs_approval과 대체 시간(suggestions)을 돌려줍니다.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "일정 제목"},
                "date": {"type": "string", "description": "날짜 YYYY-MM-DD"},
                "start": {"type": "string",
                          "description": "시작 시간 HH:MM (24시간), 종일 일정이면 빈 문자열. 오전/오후 없는 1~7시는 오후(예: 2시 → 14:00)"},
                "end": {"type": "string",
                        "description": "끝나는 시간 HH:MM. 말하지 않았으면 빈 문자열 → 종류별 자동 "
                                       f"({type_guide()})"},
                "category": {"type": "string", "enum": EVENT_CATS, "description": "일정 종류"},
                "note": {"type": "string", "description": "메모, 없으면 빈 문자열"},
                "weekday": {"type": "string", "enum": ["", "월", "화", "수", "목", "금", "토", "일"],
                            "description": "김 대리가 요일을 말했으면 그 요일(예: 목), 날짜로만 말했으면 빈 문자열"},
            },
            "required": ["title", "date", "start", "end", "category", "note", "weekday"], "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function", "name": "move_event",
        "description": "기존 일정의 날짜·시간을 옮깁니다. id는 [현재 업무 데이터]의 일정 id를 사용합니다. "
                       "옮긴 시간이 충돌하면 needs_approval을 돌려줍니다.",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "start": {"type": "string", "description": "HH:MM, 종일이면 빈 문자열"},
                "end": {"type": "string", "description": "HH:MM, 모르면 빈 문자열(원래 길이 유지)"},
                "weekday": {"type": "string", "enum": ["", "월", "화", "수", "목", "금", "토", "일"],
                            "description": "김 대리가 요일을 말했으면 그 요일(예: 목), 날짜로만 말했으면 빈 문자열"},
            },
            "required": ["id", "date", "start", "end", "weekday"], "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function", "name": "decide_pending_event",
        "description": "충돌로 승인 대기 중인 일정을 김 대리의 선택대로 처리합니다. "
                       "1~3: 그 번호의 대체 시간으로 등록, original: 원래 시간으로 등록, cancel: 취소.",
        "parameters": {
            "type": "object",
            "properties": {"choice": {"type": "string", "enum": ["1", "2", "3", "original", "cancel"]}},
            "required": ["choice"], "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function", "name": "get_recent_mails",
        "description": "김 대리 받은편지함의 최근 메일을 가져옵니다(보낸 사람, 시각, 제목, 본문 일부, 캘린더 추가 제안 여부). "
                       "'최근 메일 요약해줘', '메일 온 거 있어?' 같은 요청에 사용합니다.",
        "parameters": {
            "type": "object",
            "properties": {"count": {"type": "integer", "description": "가져올 메일 수 (1~10, 말하지 않으면 5)"}},
            "required": ["count"], "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function", "name": "confirm_pending_email",
        "description": "보내기를 기다리는 메일(send_email 결과가 needs_confirmation)을 김 대리 말대로 처리합니다. "
                       "'보내줘', '그래도 보내' → send, '보내지 마', '취소' → cancel.",
        "parameters": {
            "type": "object",
            "properties": {"action": {"type": "string", "enum": ["send", "cancel"]}},
            "required": ["action"], "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function", "name": "delete_event",
        "description": "일정을 삭제(취소)해 달라고 할 때 사용합니다. 바로 지우지 않고 삭제 대상을 찾아 화면에 보여 준 뒤 "
                       "status=needs_approval과 candidates를 돌려줍니다. 김 대리가 승인해야 decide_pending_delete로 삭제됩니다.",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "일정 날짜 YYYY-MM-DD ([날짜표]에서 찾기), 날짜를 말하지 않았으면 빈 문자열"},
                "weekday": {"type": "string", "enum": ["", "월", "화", "수", "목", "금", "토", "일"],
                            "description": "김 대리가 요일을 말했으면 그 요일, 아니면 빈 문자열"},
                "keyword": {"type": "string",
                            "description": "일정을 가리키는 말 그대로(예: '점심 약속', '고객사 미팅'), 없으면 빈 문자열"},
            },
            "required": ["date", "weekday", "keyword"], "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function", "name": "decide_pending_delete",
        "description": "삭제 승인 대기 중인 일정을 김 대리의 대답대로 처리합니다. 김 대리가 승인한 뒤에만 사용합니다. "
                       "번호(1~5): 그 일정 삭제, all: 후보 전부 삭제, cancel: 삭제하지 않음.",
        "parameters": {
            "type": "object",
            "properties": {"choice": {"type": "string", "enum": ["1", "2", "3", "4", "5", "all", "cancel"]}},
            "required": ["choice"], "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function", "name": "complete_todo",
        "description": "할 일을 완료 처리합니다. id는 현재 할 일 목록에 있는 값을 사용합니다.",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "할 일 id"}},
            "required": ["id"], "additionalProperties": False,
        },
        "strict": True,
    },
]

TOOL_LABEL = {"send_email": "메일", "add_todo": "할 일 추가", "add_event": "일정 추가",
              "move_event": "일정 변경", "complete_todo": "완료 처리", "decide_pending_event": "일정 승인",
              "confirm_pending_email": "메일 발송", "get_recent_mails": "받은 메일",
              "delete_event": "일정 삭제", "decide_pending_delete": "삭제 승인"}


def recent_mails_for_chat(count=5):
    # 에이블 비서가 받은 메일을 읽을 수 있게: 본문은 일부만 넘기고 요약은 AI가 본문 내용만으로 작성
    if not gmail_ready():
        return {"status": "failed", "error": "Gmail 설정(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)이 없어 메일을 읽을 수 없습니다."}
    got = fetch_mails(max(1, min(int(count or 5), 10)))
    if "error" in got:
        return {"status": "failed", "error": f"메일을 불러오지 못했습니다: {got['error']}"}
    analyzed = {m["msg_id"]: m for m in data["mail_analysis"]["mails"]}
    out = []
    for m in got["mails"]:
        a = analyzed.get(m["msg_id"])
        out.append({"from": m["from"], "date": m["date"], "subject": m["subject"], "unread": m["unread"],
                    "body": m["body"][:800],
                    "calendar_proposal": ({"pending": "캘린더 추가 여부를 묻는 중", "added": "추가함",
                                           "declined": "추가 안 함"}.get(a["proposal"]["decision"], "없음") if a else "분석 전")})
    return {"status": "success", "count": len(out), "mails": out}


def run_tool(name, args):
    if name == "add_todo":
        return add_todo(args["title"], args["due"], args["priority"], args["detail"], "에이블", args["category"])
    if name in ("add_event", "move_event", "delete_event"):
        bad = check_weekday(args["date"], args.get("weekday", ""))
        if bad:
            return bad
    if name == "add_event":
        return add_event(args["title"], args["date"], args["note"], "에이블", args["start"], args["end"],
                         args["category"])
    if name == "move_event":
        return move_event(args["id"], args["date"], args["start"], args["end"])
    if name == "decide_pending_event":
        return decide_pending(args["choice"])
    if name == "delete_event":
        return request_delete(args["date"], args["keyword"].strip())
    if name == "decide_pending_delete":
        return decide_pending_delete(args["choice"])
    if name == "get_recent_mails":
        return recent_mails_for_chat(args["count"])
    if name == "complete_todo":
        return set_todo_done(args["id"], True)
    return {"status": "failed", "error": f"알 수 없는 도구: {name}"}


def action_of(name, args, result):
    label = TOOL_LABEL.get(name, name)
    if not isinstance(result, dict) or result.get("status") == "failed":
        return {"ok": False, "text": f"{label} 실패 ({(result or {}).get('error', '')})"}
    if name == "delete_event":
        return {"ok": True, "warn": True, "text": "🗑️ 삭제 승인 대기 → "
                + ", ".join(f"{c['no']}. {c['title']} ({c['when']})" for c in result["candidates"])}
    if name == "decide_pending_delete":
        if result["decision"] == "취소":
            return {"ok": True, "text": "🗑️ 일정 삭제를 취소했어요 (일정은 그대로 있어요)"}
        return {"ok": True, "text": "🗑️ 일정 삭제 완료 → " + ", ".join(result["deleted"])}
    if result.get("status") == "needs_approval":
        return {"ok": True, "warn": True,
                "text": f"일정 충돌 감지 → 승인 대기: {result['title']} ({result['requested']}) · "
                        f"겹치는 일정 {len(result['conflicts'])}건, 대체 시간 {len(result['suggestions'])}개"}
    if name in ("add_event", "move_event"):
        return {"ok": True, "text": f"{label} 완료 → {result['title']} "
                                    f"({fmt_date(parse_date(result['date']))} {event_time_text(result)}, {result['category']})"}
    if name == "decide_pending_event":
        if result["decision"] == "취소":
            return {"ok": True, "text": f"{label} → '{result['title']}' 등록 취소"}
        return {"ok": True, "text": f"{label} → {result['title']} {result['date']} {result['time']} 등록 ({result['decision']})"}
    if name == "add_todo":
        return {"ok": True, "text": f"{label} 완료 → {result['title']} ({result['category']})"}
    if name == "get_recent_mails":
        return {"ok": True, "text": f"{label} → 최근 메일 {result['count']}통 확인"}
    return {"ok": True, "text": f"{label} 완료 → {result.get('todo', '')}"}


def show_action(a):
    (st.error if not a["ok"] else st.warning if a.get("warn") else st.success)(a["text"])


def system_prompt():
    snapshot = {
        "todos": [{k: t[k] for k in ("id", "title", "due", "priority", "category", "done", "detail")}
                  for t in data["todos"]],
        "events": [{k: e[k] for k in ("id", "title", "date", "start", "end", "category", "note")}
                   for e in data["events"]],
        "contacts": [{"name": c["name"], "role": c["role"], "team": c["team"], "alias": c["alias"],
                      "email": c["email"]} for c in data["contacts"]],
    }
    lines = [
        "당신은 '김 대리'의 개인 비서 '에이블'입니다. 김 대리가 요청한 업무를 최선을 다해 처리합니다.",
        f"오늘 날짜: {today.isoformat()} ({WEEKDAYS[today.weekday()]}요일), 현재 시각: {now_kst()[11:]}",
        date_table(),
        f"김 대리 이메일: {my_email() or '(미설정)'}",
        "",
        "규칙",
        "- [현재 업무 데이터], [최근 회의], 첨부 파일, 이전 대화에 있는 내용만 근거로 답합니다.",
        "  근거가 없는 내용은 지어내지 말고 '확인되지 않는 내용입니다'라고 말합니다.",
        "- '이번 주 목요일', '모레 오후 3시' 같은 표현은 [날짜표]에서 날짜를 찾아 YYYY-MM-DD, HH:MM으로 도구에 넘깁니다.",
        "  요일을 말했으면 add_event·move_event의 weekday에 그 요일을 넣습니다. 요일이 맞지 않는다는 결과가 오면 날짜표를 다시 보고 고칩니다.",
        "- 할 일/일정 추가, 일정 변경, 완료 처리를 요청하면 해당 도구를 사용합니다. 제목에 이모티콘을 넣지 않습니다.",
        f"- 일정 종류: {' / '.join(EVENT_CATS)} 중 내용에 맞게 고릅니다. (김 대리가 직접 정한 종류 목록)",
        f"- 할 일 분류: {' / '.join(TODO_CATS)} 중 내용에 맞게 고릅니다.",
        "  끝나는 시간을 말하지 않았으면 end는 빈 문자열로 둡니다(종류별 소요시간 자동 추정).",
        "- add_event·move_event 결과가 needs_approval이면 아직 등록되지 않은 것입니다.",
        "  어떤 일정과 왜 충돌하는지(시간 겹침/간격 n분) 알리고, suggestions의 번호와 시간만 제시한 뒤",
        "  '대체 시간 번호 선택, 원래 시간으로 등록, 취소' 중 고르게 합니다. 다른 시간을 지어내지 않습니다.",
        "  김 대리가 고르면(예: '2번으로', '그냥 원래대로', '취소해') decide_pending_event를 사용합니다.",
        "- 일정을 취소·삭제해 달라고 하면(예: '금요일 점심 약속 취소해줘') delete_event를 사용합니다. 바로 지워지지 않습니다.",
        "  결과의 candidates를 번호·제목·시간과 함께 알려 주고 '이 일정을 삭제할까요?'라고 묻습니다.",
        "  같은 답변 안에서 decide_pending_delete를 호출하지 않고, 반드시 김 대리의 다음 대답을 기다립니다.",
        "  김 대리가 '응/지워줘/1번'이라고 하면 decide_pending_delete(번호, 후보가 하나면 1), '전부' → all,",
        "  '아니/하지 마/취소'라고 하면 cancel입니다. (삭제 승인 대기 중의 '취소'는 삭제를 그만두라는 뜻)",
        "  찾지 못했으면(failed) events_on_date를 알려 주고 어떤 일정인지 묻습니다. 삭제했다고 지어내지 않습니다.",
        "- 이메일로 보내 달라는 명시적 요청이 있을 때만 send_email을 사용합니다.",
        "  받는 사람을 말하지 않으면 to에 '나'를 넣습니다. 연락처에 있는 사람은 이름으로 넣을 수 있고,",
        "  연락처에 없는 사람의 이메일 주소는 절대 지어내지 않습니다.",
        "- 받은 메일을 물으면(예: '최근 받은 메일 5개 요약해줘') get_recent_mails로 가져와,",
        "  메일마다 보낸 사람·제목과 본문에 있는 내용만으로 한두 문장씩 요약합니다. 본문에 없는 내용은 지어내지 않습니다.",
        "  calendar_proposal이 '캘린더 추가 여부를 묻는 중'이면 메일 비서 탭에서 추가할지 고를 수 있다고 알려 줍니다.",
        "- 메일을 보내 달라고 하면 메일을 쓰고 send_email까지 반드시 호출합니다. 보낼지 다시 묻지 않습니다.",
        "- send_email 결과가 success면 '메일을 보냈습니다'라고 받는 사람과 제목을 알려 줍니다.",
        "- needs_confirmation이면 아직 보내지 않은 것입니다. AI 확인에서 나온 확인할 점(issues)을 짧게 말하고",
        "  '그래도 보낼까요?'라고 묻습니다. 김 대리가 '보내줘/그래도 보내'라고 하면 confirm_pending_email(send),",
        "  '보내지 마/취소'라고 하면 confirm_pending_email(cancel)을 사용합니다.",
        "- failed면 이유를 알리고 필요한 정보를 묻습니다.",
        "- 도구를 사용한 뒤에는 무엇을 했는지 짧게 보고합니다.",
        "- 공손한 한국어 존댓말로 간결하게 답합니다.",
        "",
        "[현재 업무 데이터]",
        json.dumps(snapshot, ensure_ascii=False),
        "",
    ]
    p = st.session_state.get("pending_event")
    if p:
        f = p["fields"]
        lines += ["[승인 대기 중인 일정]",
                  f"{f['title']} ({f['date']} {event_time_text(f)}, {f['category']}) — 대체 시간: "
                  + ", ".join(f"{i + 1}) {slot_text(x)}" for i, x in enumerate(p["suggestions"])), ""]
    pd_evs = pending_delete_events()
    if pd_evs:
        lines += ["[삭제 승인 대기 중인 일정] (아직 삭제하지 않음)"]
        lines += [f"{i + 1}) {e['title']} ({e['date']} {event_time_text(e)})" for i, e in enumerate(pd_evs)] + [""]
    note = latest_note()
    if note:
        lines += [f"[최근 회의] {note['title']} ({note['when']})", "요약:"]
        lines += [f"- {s}" for s in note["summary"]] or [note["minutes"][:3000]]
        lines += ["김 대리 업무:"] + [f"- {t['title']} (기한: {t['due_text'] or '언급 없음'})" for t in note["kim_tasks"]]
        lines += ["회의 원문:", note["text"][:6000]]
    else:
        lines.append("[최근 회의] (없음)")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# 에이블 비서 (과제 8: 음성 인식 AI 개인비서)
#   텍스트 파일 첨부 · 실시간 녹음(STT) · 실행 버튼 · 스피너 · LLM 호출(도구 사용)
#   · 응답 화면 출력 · 응답 음성 즉시 재생(TTS) · 멀티턴 상태 관리 · GPT처럼 대화 기록/새 채팅
# ─────────────────────────────────────────────
MAX_TURNS = 10   # 대화마다 기억할 질문+답변 개수
MAX_CHATS = 10   # 보관할 대화 개수
VOICES = {"alloy": "기본", "nova": "밝은", "shimmer": "부드러운", "echo": "차분한", "onyx": "낮은"}
QUICK_ASKS = ["📝 최근 회의 내용 요약해줘", "📥 최근 받은 메일 5개 요약해줘", "📧 오늘 할 일을 내 메일로 보내줘"]


def clean_for_tts(text):
    # 마크다운 기호를 빼고 읽기 좋은 문장으로 (TTS가 '#', '*' 등을 읽지 않게)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    t = re.sub(r"^\s*\|?[-:| ]+\|?\s*$", "", t, flags=re.M)
    t = re.sub(r"^\s*[-*+]\s+", "", t, flags=re.M)
    t = re.sub(r"[#*`>|_~]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def current_chat():
    cid = st.session_state.get("chat_id")
    return next((c for c in data["chats"] if c["id"] == cid), None)


def new_chat_cb(notify=True):
    st.session_state.chat_id = None
    st.session_state.chat_n = st.session_state.get("chat_n", 0) + 1     # 녹음기·입력칸 새로
    if notify:
        st.session_state.pending_toast = "새 채팅을 시작했어요. 에이블 비서 탭에서 마이크를 눌러 말로 요청해 보세요."


def open_chat_cb(cid):
    st.session_state.chat_id = cid
    st.session_state.chat_n = st.session_state.get("chat_n", 0) + 1


def delete_chat_cb(cid):
    data["chats"] = [c for c in data["chats"] if c["id"] != cid]
    save_data()
    if st.session_state.get("chat_id") == cid:
        new_chat_cb(notify=False)


def save_turn(chat, turn):
    # 상태 관리: 질문·답변을 대화에 저장 (대화마다 최근 10개, 대화는 최근 10개까지)
    if chat is None:
        title = turn["q"][:22] + ("…" if len(turn["q"]) > 22 else "")
        chat = {"id": uuid.uuid4().hex[:8], "title": title or "새 채팅", "created": now_kst(),
                "updated": now_kst(), "turns": []}
        data["chats"].insert(0, chat)
    chat["turns"].append(turn)
    chat["turns"] = chat["turns"][-MAX_TURNS:]
    chat["updated"] = now_kst()
    data["chats"].sort(key=lambda c: c["updated"], reverse=True)
    del data["chats"][MAX_CHATS:]
    save_data()
    st.session_state.chat_id = chat["id"]
    return chat


def prepare_email(args, question, via="text"):
    # send_email 도구: 받는 사람 확인 → AI 점검 → 문제가 없으면 발송
    #   음성 요청은 AI 확인에서 문제가 없으면 바로 발송, 글 요청은 설정에 따라 김 대리 확인 후 발송
    rcpt, err = resolve_recipient(args["to"])
    if err:
        return {"status": "failed", "error": err}, None, {"ok": False, "text": f"📧 메일 준비 실패 ({err})"}
    ctx = f"[김 대리 요청]\n{question}\n\n[참고 자료]\n{brief_context()}"
    rv = review_email(rcpt, args["subject"], args["body"], ctx)
    who = f"{rcpt['name']} <{rcpt['email']}>" if rcpt["name"] else rcpt["email"]
    if rv["ok"] and (via == "voice" or not data["settings"].get("confirm_before_send", True)):
        sr = send_email(rcpt["email"], args["subject"], args["body"])
        if sr["status"] == "success":
            return ({"status": "success", "to": rcpt["email"], "review": rv["summary"]}, None,
                    {"ok": True, "text": f"📧 AI 확인 후 발송 완료 → {who} · '{args['subject']}'"})
        return sr, None, {"ok": False, "text": f"📧 메일 발송 실패 ({sr['error']})"}
    mail = {"to": rcpt["email"], "name": rcpt["name"], "subject": args["subject"], "body": args["body"],
            "review": rv, "context": ctx, "status": "pending"}
    result = {"status": "needs_confirmation", "to": rcpt["email"], "name": rcpt["name"], "review": rv["summary"],
              "issues": [c["comment"] for c in rv["checks"] if not c["ok"]],
              "message": ("아직 보내지 않았습니다. 김 대리가 '보내줘'라고 하면 confirm_pending_email(send)로 보내고, "
                          "'보내지 마'라고 하면 cancel 합니다. 화면의 '보내기 전 확인' 카드에서 버튼으로 보낼 수도 있습니다.")}
    return result, mail, {"ok": True, "warn": True, "text": f"📧 보내기 전 확인 대기 → {who} · '{args['subject']}'"}


def confirm_pending_mail(chat, action, current=None):
    # 말로 "보내줘" / "보내지 마" → 보내기를 기다리는 메일을 발송하거나 취소
    target, turn = None, None
    if current and current["status"] == "pending":
        target = current
    else:
        for t in reversed(chat["turns"] if chat else []):
            if t.get("mail") and t["mail"]["status"] == "pending":
                target, turn = t["mail"], t
                break
    if target is None:
        return {"status": "failed", "error": "보내기를 기다리는 메일이 없습니다."}, {"ok": False, "text": "메일 발송 실패 (보낼 메일 없음)"}
    who = f"{target['name']} <{target['to']}>" if target["name"] else target["to"]
    if action == "cancel":
        target["status"] = "canceled"
        save_data()
        return {"status": "success", "canceled": True}, {"ok": True, "warn": True, "text": "📧 메일 발송을 취소했어요."}
    sr = send_email(target["to"], target["subject"], target["body"])
    if sr["status"] != "success":
        return sr, {"ok": False, "text": f"📧 메일 발송 실패 ({sr['error']})"}
    target["status"] = "sent"
    if turn is not None:
        turn["actions"].append({"ok": True, "text": f"📧 메일 발송 완료 → {who} · '{target['subject']}'"})
    save_data()
    return ({"status": "success", "to": target["to"], "subject": target["subject"]},
            {"ok": True, "text": f"📧 메일 발송 완료 → {who} · '{target['subject']}'"})


def ask_assistant(chat, question, file_name="", file_text="", via="text"):
    # 이 대화의 최근 질문·답변(멀티턴) + 첨부 파일 + 업무 데이터로 LLM 호출, 필요하면 도구 실행
    instructions = system_prompt() + "\n" + "\n".join([
        "",
        "[답변 방식]",
        "- 답변은 화면에 보여 주고 음성으로도 읽어 주므로, 핵심부터 간결하게 말합니다.",
        "- 표보다는 짧은 문단과 목록을 씁니다.",
        "- 이전 대화 내용을 기억하고 이어서 답합니다.",
        "",
        f"[첨부 파일: {file_name}]" if file_text else "[첨부 파일] (없음)",
        file_text[:12000] if file_text else "",
    ])
    inputs = []
    for t in (chat["turns"] if chat else [])[-MAX_TURNS:]:
        inputs.append({"role": "user", "content": t["q"]})
        inputs.append({"role": "assistant", "content": t["a"]})
    inputs.append({"role": "user", "content": question})

    client = get_client()
    actions, mail = [], None
    asked_delete = False                 # 이번 답변에서 삭제 대상을 보여 줬으면 같은 답변에서는 삭제 금지
    for _ in range(5):  # 도구 호출은 최대 5회 반복
        resp = client.responses.create(model=MODEL, instructions=instructions, input=inputs, tools=TOOLS)
        calls = [i for i in resp.output if i.type == "function_call"]
        if not calls:
            return resp.output_text, actions, mail
        inputs += resp.output
        for c in calls:
            args = json.loads(c.arguments)
            if c.name == "send_email":
                result, new_mail, act = prepare_email(args, question, via)
                mail = new_mail or mail
            elif c.name == "confirm_pending_email":
                result, act = confirm_pending_mail(chat, args["action"], mail)
            elif c.name == "decide_pending_delete" and asked_delete and args["choice"] != "cancel":
                result = {"status": "failed", "retry": True,
                          "error": "삭제 대상을 방금 보여 드렸습니다. 김 대리의 승인 대답을 받은 뒤에만 삭제할 수 있습니다. "
                                   "지금은 삭제 대상을 알려 주고 삭제할지 물어보세요."}
                act = None
            else:
                result = run_tool(c.name, args)
                act = action_of(c.name, args, result)
                if c.name == "delete_event" and result.get("status") == "needs_approval":
                    asked_delete = True
            if not (isinstance(result, dict) and result.get("retry")):
                actions.append(act)
            inputs.append({"type": "function_call_output", "call_id": c.call_id,
                           "output": json.dumps(result, ensure_ascii=False)})
    return resp.output_text or "요청을 처리했습니다.", actions, mail


def set_quick(q):
    st.session_state.chat_quick = q


def history_text(chat):
    lines = [f"# {chat['title']} ({chat['created']})", ""]
    for t in chat["turns"]:
        lines += [f"[{t['time']}] 김 대리: {t['q']}", f"[{t['time']}] 에이블: {t['a']}", ""]
    return "\n".join(lines)


def render_mail_confirm(t):
    # 에이블 비서가 쓴 메일: AI 확인 결과를 보여 주고 김 대리가 확인 후 발송
    m, tid = t["mail"], t["id"]
    key = f"chat_{tid}"
    with st.container(border=True):
        st.markdown("#### 📧 보내기 전 확인")
        st.caption("에이블이 메일을 한 번 더 점검했어요. 내용을 고칠 수 있고, 보내기를 눌러야 발송됩니다.")
        to_raw = st.text_input("받는 사람 (이메일 또는 연락처 이름)", value=m["to"], key=f"cm_to_{tid}")
        subj = st.text_input("제목", value=m["subject"], key=f"cm_sub_{tid}")
        body = st.text_area("본문", value=m["body"], height=220, key=f"cm_body_{tid}")
        if f"rv_{key}" not in st.session_state and m.get("review"):
            st.session_state[f"rv_{key}"] = dict(m["review"],
                                                 sig=json.dumps([m["to"], m["subject"], m["body"]], ensure_ascii=False))
        r = confirm_and_send(key, to_raw, subj, body, m.get("context", ""))
        if r == "sent":
            m.update(status="sent", to=to_raw, subject=subj, body=body)
            t["actions"].append({"ok": True, "text": f"📧 메일 발송 완료 → {to_raw} · '{subj}'"})
            save_data()
            st.session_state.pending_toast = "메일을 보냈어요."
            st.rerun()
        if st.button("✖ 보내지 않기", key=f"cm_cancel_{tid}"):
            m["status"] = "canceled"
            t["actions"].append({"ok": True, "warn": True, "text": "📧 메일 발송을 취소했어요."})
            save_data()
            st.rerun()


with tab_chat:
    st.session_state.setdefault("chat_n", 0)
    st.session_state.setdefault("audio_cache", {})
    n = st.session_state.chat_n
    chat = current_chat()
    turns = chat["turns"] if chat else []
    autoplay_id = st.session_state.pop("chat_autoplay", None)

    h1, h2, h3 = st.columns([5, 1.3, 1.3])
    h1.markdown(f"#### 💬 {chat['title'] if chat else '새 채팅'}")
    h1.caption(f"🧠 이 대화에서 기억 중인 질문·답변 {len(turns)}/{MAX_TURNS} · 대화 기록은 왼쪽 사이드바에 있어요")
    h2.button("➕ 새 채팅", key="new_chat_main", on_click=new_chat_cb, use_container_width=True)
    h3.download_button("⬇️ 대화 저장", history_text(chat) if chat else "", file_name="에이블_대화기록.txt",
                       mime="text/plain", use_container_width=True, disabled=not turns)

    voice_in = None
    has_mic = hasattr(st, "audio_input")
    if not turns:
        # 새 채팅: 바로 말로 요청할 수 있게 녹음기를 맨 위에 크게
        with st.container(border=True):
            st.markdown("### 🎙️ 말로 요청해 보세요")
            st.caption("마이크 버튼을 눌러 말하고 다시 누르면 바로 에이블에게 전달돼요. "
                       "예) 내일 오후 2시에 고객사 미팅 잡아줘 · 최근 회의 요약해서 박과장님께 메일 보내줘")
            if has_mic:
                voice_in = st.audio_input("음성으로 요청하기", key=f"chat_mic_{n}", label_visibility="collapsed")
                st.caption("녹음기에 'An error has occurred'가 뜨면 페이지를 새로고침한 뒤 다시 녹음하거나, 아래에 글로 입력해 주세요.")
            else:
                st.warning("실시간 녹음을 쓰려면 `!pip install -q -U streamlit`으로 업데이트하세요.")

    for t in turns:
        with st.chat_message("user", avatar="🧑‍💼"):
            if t["via"] == "voice":
                st.caption("🎙️ 음성 인식 결과 (STT) — 말씀하신 내용이 맞는지 확인하세요")
            st.markdown(t["q"])
            st.caption(t["time"] + (f" · 첨부 {t['file']}" if t["file"] else ""))
        with st.chat_message("assistant", avatar="🤖"):
            st.markdown(t["a"])
            for a in t["actions"]:
                show_action(a)
            audio = st.session_state.audio_cache.get(t["id"])
            if audio:
                st.audio(audio, format="audio/mp3", autoplay=(t["id"] == autoplay_id))

    for t in turns:
        if t.get("mail") and t["mail"]["status"] == "pending":
            render_mail_confirm(t)

    # --- 입력 카드 ---
    with st.container(border=True):
        if turns and has_mic:
            voice_in = st.audio_input("🎙️ 말로 이어서 요청하기", key=f"chat_mic_{n}")
        typed = st.text_input("⌨️ 글로 입력", key=f"chat_text_{n}",
                              placeholder="예) 박과장님께 회의 요약 메일 보내줘 / 금요일 오후 3시 온라인 회의 잡아줘")
        up_txt = st.file_uploader("📎 텍스트 파일 첨부 (회의록, 메모 등)", type=["txt", "md", "csv"], key="chat_file")
        file_name, file_text = "", ""
        if up_txt is not None:
            file_name = up_txt.name
            file_text = up_txt.getvalue().decode("utf-8", errors="ignore")
            st.caption(f"📎 **{file_name}** 첨부됨 · {len(file_text):,}자 — 이 내용을 참고해 답변합니다.")

        qc = st.columns(len(QUICK_ASKS))
        for col, qa in zip(qc, QUICK_ASKS):
            col.button(qa, key=f"quick_{qa}", on_click=set_quick, args=(qa,), use_container_width=True)

        b1, b2, b3, b4 = st.columns([1.1, 1.5, 1.6, 1.4])
        run_clicked = b1.button("▶ 실행", type="primary", use_container_width=True)
        auto_voice = b2.toggle("녹음 끝나면 바로 요청", value=True, key="chat_auto")
        tts_on = b3.toggle("🔊 답변 음성 재생", value=True, key="chat_tts")
        voice = b4.selectbox("목소리", list(VOICES), format_func=lambda v: f"{VOICES[v]} ({v})",
                             key="chat_voice", label_visibility="collapsed")

    quick = st.session_state.pop("chat_quick", None)
    voice_bytes = voice_in.getvalue() if voice_in is not None else None
    voice_hash = hashlib.md5(voice_bytes).hexdigest() if voice_bytes else None
    new_voice = bool(voice_hash) and voice_hash != st.session_state.get("voice_done")
    if run_clicked or quick or (auto_voice and new_voice):
        question = quick.split(" ", 1)[1] if quick else typed.strip()
        if not quick and not voice_bytes and not question:
            st.warning("음성을 녹음하거나 질문을 입력한 뒤 실행을 눌러 주세요.")
        else:
            done = False
            if voice_hash:
                st.session_state.voice_done = voice_hash          # 같은 녹음을 두 번 보내지 않게
            try:
                with st.spinner("답변을 준비하고 있습니다.."):
                    stt = None
                    if voice_bytes and not quick:
                        stt = speech_to_text(voice_bytes)                            # 음성 → 텍스트 (STT)
                        question = (stt + " " + question).strip()
                    answer, actions, mail = ask_assistant(chat, question, file_name, file_text,
                                                          "voice" if stt else "text")   # LLM 호출
                    reply_audio = text_to_speech(clean_for_tts(answer), voice) if tts_on and answer else None
                turn = {"id": uuid.uuid4().hex[:8], "q": question, "stt": stt, "via": "voice" if stt else "text",
                        "file": file_name, "a": answer, "actions": actions, "mail": mail, "time": now_kst()[11:]}
                save_turn(chat, turn)                                            # 상태 저장 (멀티턴)
                if reply_audio:
                    cache = st.session_state.audio_cache
                    cache[turn["id"]] = reply_audio
                    for k in list(cache)[:-30]:
                        del cache[k]
                st.session_state.chat_autoplay = turn["id"]                       # 답변 음성 즉시 재생
                st.session_state.chat_n += 1                                      # 녹음기·입력칸 비우기
                done = True
            except Exception as e:
                st.error(f"답변을 만들지 못했습니다: {e}")
            if done:
                st.rerun()   # try 밖에서 호출해야 화면 새로고침 신호가 막히지 않음


# ─────────────────────────────────────────────
# 🎙️ 회의 분석 (실시간 녹음 · 음성 파일 · 텍스트 파일)
# ─────────────────────────────────────────────
SCHEDULE_ITEM = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "when_text": {"type": "string", "description": "시점 표현 원문 그대로"},
        "date_type": {"type": "string", "enum": ["exact", "approximate", "none"]},
        "date": {"type": "string", "description": "exact일 때만 YYYY-MM-DD, 아니면 빈 문자열"},
        "evidence": {"type": "string", "description": "원문에서 그대로 복사한 근거 문장 1개"},
    },
    "required": ["title", "when_text", "date_type", "date", "evidence"],
    "additionalProperties": False,
}

MEETING_SCHEMA = {
    "type": "object",
    "properties": {
        "title_guess": {"type": "string", "description": "원문에 나온 회의 주제, 없으면 빈 문자열"},
        "summary": {"type": "array", "items": {"type": "string"}, "description": "안건별 핵심 3~6문장"},
        "decisions": {"type": "array", "items": {"type": "string"}, "description": "회의에서 확정된 사항"},
        "kim_tasks": {"type": "array", "items": TASK_ITEM},
        "schedules": {"type": "array", "items": SCHEDULE_ITEM,
                      "description": "김 대리 업무가 아닌, 회의에서 언급된 일정(출시, 광고 시작, 다음 회의 등)"},
    },
    "required": ["title_guess", "summary", "decisions", "kim_tasks", "schedules"],
    "additionalProperties": False,
}


def analyze_meeting(text, title="", attendees=""):
    instructions = "\n".join([
        "당신은 '김 대리'의 개인 비서 '에이블'입니다. 회의 원문을 분석합니다.",
        f"오늘 날짜: {today.isoformat()} ({WEEKDAYS[today.weekday()]}요일). 회의는 오늘 열렸다고 가정합니다.",
        date_table(),
        "원문이 음성 인식 결과일 수 있어 오탈자가 있을 수 있습니다. 문맥상 분명한 것만 바로잡습니다.",
        "",
        "[작성 규칙]",
        *TASK_RULES,
        "- summary: 안건별 핵심을 3~6개 문장으로. decisions: 회의에서 확정된 사항만.",
        "- schedules: 김 대리 업무가 아닌 일정만. when_text는 원문 표현, date_type/date는 위 기한 규칙과 같게.",
    ])
    resp = get_client().responses.create(
        model=MODEL, instructions=instructions,
        input=f"[회의 제목] {title or '(입력 없음)'}\n[참석자] {attendees or '(입력 없음)'}\n\n[회의 원문]\n{text}",
        text={"format": {"type": "json_schema", "name": "meeting_analysis",
                         "schema": MEETING_SCHEMA, "strict": True}},
    )
    return json.loads(resp.output_text)


def note_markdown(n):
    if not n["summary"] and n["minutes"]:      # 예전 버전 회의록
        return n["minutes"]
    lines = [f"# 회의록 - {n['title']}", "",
             f"- 일시: {n['when']}", f"- 참석자: {n['attendees'] or '(입력 없음)'}", f"- 분석 방법: {n['source']}", "",
             "## 주요 논의 내용"] + [f"- {s}" for s in n["summary"]]
    lines += ["", "## 결정 사항"] + ([f"- {s}" for s in n["decisions"]] or ["- (없음)"])
    lines += ["", "## 김 대리 업무"]
    for t in n["kim_tasks"]:
        lines.append(f"- {t['title']} (기한: {t['due_text'] or '언급 없음'}"
                     + (f", 요청: {t['requester']}" if t["requester"] else "") + ")")
        if t["detail"]:
            lines.append(f"  - {t['detail']}")
    if not n["kim_tasks"]:
        lines.append("- (없음)")
    lines += ["", "## 회의에서 언급된 일정 (참고)"]
    lines += [f"- {s['title']}: {s['when_text'] or '시점 언급 없음'}" for s in n["schedules"]] or ["- (없음)"]
    return "\n".join(lines)


with tab_meet:
    st.markdown("회의를 녹음하거나 녹음·텍스트 파일을 올리면 에이블이 요약하고 **김 대리 업무만** 할 일에 올립니다.")
    st.caption("📅 정확한 날짜가 있는 김 대리 업무만 캘린더에 들어갑니다. 대략적인 기한이나 다른 일정은 이 화면에서만 보여 드려요. "
               "원문에서 근거를 찾지 못한 내용은 추가하지 않습니다.")

    with st.container(border=True):
        mode = st.radio("분석 방법", ["🎙️ 실시간 녹음", "📁 음성 파일 분석", "📄 텍스트 파일 분석"],
                        horizontal=True, key="meet_mode")
        src_audio, src_name, src_text = None, "", ""
        if mode.startswith("🎙️"):
            if hasattr(st, "audio_input"):
                rec = st.audio_input("마이크 버튼을 눌러 회의 녹음을 시작·종료하세요", key="meet_mic")
                if rec is not None:
                    src_audio, src_name = rec.getvalue(), "recording.wav"
            else:
                st.warning("실시간 녹음을 쓰려면 `!pip install -q -U streamlit`으로 업데이트하세요.")
        elif mode.startswith("📁"):
            up_a = st.file_uploader("회의 녹음 파일 (m4a, mp3, wav 등)", type=AUDIO_TYPES, key="meet_audio")
            if up_a is not None:
                src_audio, src_name = up_a.getvalue(), up_a.name
        else:
            up_t = st.file_uploader("회의록 텍스트 파일 (.txt)", type=["txt", "md"], key="meet_txt")
            default = ""
            if up_t is not None:
                default = up_t.getvalue().decode("utf-8", errors="ignore")
            elif (BASE / "meeting.txt").exists():
                default = (BASE / "meeting.txt").read_text(encoding="utf-8")
            src_text = st.text_area("회의록 내용 (직접 고치거나 붙여 넣어도 됩니다)", value=default, height=200,
                                    key=f"meet_text_{up_t.name if up_t else 'default'}")
        if src_audio:
            st.audio(src_audio)
            st.caption(f"{src_name} · {len(src_audio) / 1024 / 1024:.1f}MB")

        c1, c2 = st.columns(2)
        m_title = c1.text_input("회의 제목 (선택)", placeholder="예: 신제품 출시 마케팅 전략 회의")
        m_people = c2.text_input("참석자 (선택)", placeholder="예: 팀장, 박과장, 이과장, 최대리")
        run_meet = st.button("🔍 회의 분석하기", type="primary",
                             disabled=not (src_audio or src_text.strip()))

    if run_meet:
        done = False
        with st.status("회의를 분석하는 중...", expanded=True) as status:
            try:
                if src_audio:
                    st.write("① 음성을 글로 받아 적는 중 (회의 길이에 따라 1~3분)")
                    a_bytes, a_name = shrink_audio(src_audio, src_name)
                    if len(a_bytes) > 25 * 1024 * 1024:
                        raise ValueError("파일이 너무 큽니다. 25MB 이하로 나눠서 올려 주세요.")
                    hint = "회사 회의 녹음입니다." + (f" 참석자: {m_people}" if m_people else "")
                    text = speech_to_text(a_bytes, a_name, hint)
                else:
                    text = src_text
                st.write("② 회의 내용 분석 중")
                res = analyze_meeting(text, m_title, m_people)
                st.write("③ 김 대리 업무를 할 일·캘린더에 반영 중")
                tasks = apply_kim_tasks(res["kim_tasks"], text, "회의 분석")
                schedules = [dict(s, verified=found_in(s["evidence"], text)) for s in res["schedules"]]
                note = {
                    "id": uuid.uuid4().hex[:8],
                    "title": m_title.strip() or res["title_guess"].strip() or f"{today.isoformat()} 회의",
                    "when": now_kst(), "attendees": m_people.strip(),
                    "source": ("실시간 녹음" if mode.startswith("🎙") else "음성 파일") if src_audio else "텍스트 파일",
                    "text": text, "summary": res["summary"], "decisions": res["decisions"],
                    "kim_tasks": tasks, "schedules": schedules,
                }
                data["meeting_notes"].insert(0, note)
                del data["meeting_notes"][20:]          # 최근 20개까지만 보관
                save_data()
                n_cal = sum(t["status"] == "calendar" for t in tasks)
                n_add = sum(t["status"] in ("calendar", "approximate", "nodue") for t in tasks)
                status.update(label="분석 완료!", state="complete")
                done = True
            except Exception as e:
                status.update(label="처리 중 오류가 발생했습니다", state="error")
                st.error(f"오류 내용: {e}")
        if done:
            st.session_state.meet_view = note["id"]
            st.session_state.pending_toast = f"김 대리 업무 {n_add}건을 할 일에 추가했어요 (캘린더 {n_cal}건)."
            st.rerun()

    notes = data["meeting_notes"]
    if not notes:
        st.info("아직 분석한 회의가 없습니다. 위에서 녹음하거나 파일을 올려 분석해 보세요.")
    else:
        ids = [n["id"] for n in notes]
        cur_id = st.session_state.get("meet_view") if st.session_state.get("meet_view") in ids else ids[0]
        if len(notes) > 1:
            label = {n["id"]: f"{n['when']} · {n['title']}" for n in notes}
            cur_id = st.selectbox("📂 회의록 선택", ids, index=ids.index(cur_id), format_func=lambda i: label[i])
        st.session_state.meet_view = cur_id
        n = next(x for x in notes if x["id"] == cur_id)

        st.markdown(f"### 📄 {n['title']}")
        st.caption(f"{n['when']} · {n['source']} · 참석자: {n['attendees'] or '입력 없음'}")
        with st.expander("회의록 이름 수정"):
            new_title = st.text_input("회의록 이름", value=n["title"], key=f"rename_{n['id']}")
            if st.button("이름 저장", key=f"rename_save_{n['id']}", type="primary"):
                if clean_text(new_title):
                    n["title"] = clean_text(new_title)[:60]
                    save_data()
                    st.session_state.pending_toast = f"회의록 이름을 '{n['title']}'(으)로 바꿨어요."
                    st.rerun()
                else:
                    st.warning("회의록 이름을 입력하세요.")

        l, r = st.columns([3, 2], gap="large")
        with l:
            st.markdown("#### 📋 회의 요약")
            if not n["summary"] and n["minutes"]:
                st.caption("이전 버전에서 만든 회의록입니다. 새 형식으로 보려면 같은 회의를 다시 분석하세요.")
                st.markdown(n["minutes"])
            else:
                st.markdown("\n".join(f"- {s}" for s in n["summary"]) or "_(요약할 내용이 없습니다)_")
            st.markdown("#### ✅ 결정 사항")
            st.markdown("\n".join(f"- {s}" for s in n["decisions"]) or "_(확정된 사항이 없습니다)_")
        with r:
            st.markdown("#### 🗓️ 회의에서 언급된 일정")
            st.caption("참고용입니다. 캘린더에는 넣지 않았어요.")
            if not n["schedules"]:
                st.caption("언급된 일정이 없습니다.")
            for s in n["schedules"]:
                when = s["when_text"] or "시점 언급 없음"
                if s["date_type"] == "exact" and parse_date(s["date"]):
                    when += f" ({fmt_date(parse_date(s['date']))})"
                st.markdown(f"- **{s['title']}** · {when}" + ("" if s.get("verified", True) else " ⚠️근거 확인 필요"))

        st.markdown(f"#### 🙋 김 대리 업무 ({len(n['kim_tasks'])}건)")
        if not n["kim_tasks"]:
            st.caption("이 회의에서 김 대리에게 맡겨진 업무는 없습니다.")
        for i, t in enumerate(n["kim_tasks"]):
            with st.container(border=True):
                st.markdown(badge(t["priority"], PRIORITY_COLOR.get(t["priority"], "#6b7280"))
                            + f"<b>{html.escape(t['title'])}</b>"
                            + (f" · 요청: {html.escape(t['requester'])}" if t["requester"] else ""),
                            unsafe_allow_html=True)
                if t["detail"]:
                    st.write(t["detail"])
                msg = task_status_line(t)
                {"calendar": st.success, "unverified": st.warning}.get(t["status"], st.info)(msg)
                if t["evidence"]:
                    st.caption(f"근거: “{t['evidence']}”")

                todo = next((x for x in data["todos"] if x["id"] == t.get("todo_id")), None)
                if t["status"] in ("approximate", "nodue") and todo and not todo["due"]:
                    a, b = st.columns([2, 2])
                    pick = a.date_input("마감일 정하기", value=None, key=f"pick_{n['id']}_{i}",
                                        label_visibility="collapsed")
                    if b.button("📅 이 날짜로 캘린더에 추가", key=f"setdue_{n['id']}_{i}", disabled=pick is None):
                        todo["due"] = pick.isoformat()
                        t.update(status="calendar", due_date=pick.isoformat())
                        save_data()
                        st.session_state.pending_toast = f"'{t['title']}' 마감일을 {fmt_date(pick)}로 정했어요."
                        st.rerun()
                if t["status"] == "unverified":
                    if st.button("그래도 할 일에 추가", key=f"force_{n['id']}_{i}"):
                        item = add_todo(t["title"], "", t["priority"], t["detail"], "회의 분석")
                        t.update(status="nodue", todo_id=item["id"])
                        save_data()
                        st.rerun()

        b1, b2, b3, b4 = st.columns(4)
        if b1.button("📧 내 메일로 보내기", use_container_width=True, disabled=not (my_email() and gmail_ready())):
            sr = send_email(my_email(), f"[에이블] 회의록 - {n['title']}", note_markdown(n))
            if sr["status"] == "success":
                st.success(f"{my_email()}로 보냈습니다.")
            else:
                st.error(f"발송 실패: {sr['error']}")
        b2.download_button("⬇️ 회의록 (.md)", note_markdown(n), file_name=f"회의록_{n['when'][:10]}.md",
                           mime="text/markdown", use_container_width=True)
        b3.download_button("⬇️ 원문 (.txt)", n["text"], file_name=f"회의원문_{n['when'][:10]}.txt",
                           mime="text/plain", use_container_width=True)
        if b4.button("🗑️ 이 회의록 삭제", use_container_width=True):
            st.session_state.meeting_delete_pending = n["id"]

        if st.session_state.get("meeting_delete_pending") == n["id"]:
            linked_ids = {t.get("todo_id") for t in n["kim_tasks"] if t.get("todo_id")}
            linked_count = sum(t["id"] in linked_ids for t in data["todos"])
            with st.container(border=True):
                st.markdown("#### 🗑️ 삭제할 범위를 선택해 주세요")
                scope = st.radio(
                    "삭제 범위",
                    ["회의록만 삭제", "회의록과 연결된 할 일도 삭제"],
                    index=0,
                    key=f"meeting_delete_scope_{n['id']}",
                )
                if scope == "회의록만 삭제":
                    st.info("회의 원문과 요약을 삭제합니다. 연결된 할 일과 캘린더 마감 표시는 유지됩니다.")
                else:
                    st.warning(f"회의록과 연결된 할 일 {linked_count}건을 함께 삭제합니다. "
                               "해당 할 일의 캘린더 마감 표시도 사라집니다. 별도로 등록한 일정은 유지됩니다.")
                cancel_col, confirm_col = st.columns(2)
                if cancel_col.button("취소", key=f"meeting_delete_cancel_{n['id']}", use_container_width=True):
                    st.session_state.pop("meeting_delete_pending", None)
                    st.rerun()
                if confirm_col.button("선택한 항목 삭제", key=f"meeting_delete_confirm_{n['id']}",
                                      type="primary", use_container_width=True):
                    delete_tasks = scope == "회의록과 연결된 할 일도 삭제"
                    if delete_tasks:
                        data["todos"] = [t for t in data["todos"] if t["id"] not in linked_ids]
                    data["meeting_notes"] = [item for item in data["meeting_notes"] if item["id"] != n["id"]]
                    save_data()
                    st.session_state.pop("meeting_delete_pending", None)
                    st.session_state.pop("meet_view", None)
                    st.session_state.pending_toast = (
                        f"회의록과 연결된 할 일 {linked_count}건을 삭제했어요."
                        if delete_tasks else "회의록만 삭제했어요. 할 일과 캘린더 마감 표시는 유지했어요."
                    )
                    st.rerun()
        with st.expander("🗣️ 회의 원문 / 녹취록 보기"):
            st.text(n["text"])


# ─────────────────────────────────────────────
# 📥 메일 비서 (메일 발송 · 받은 메일 요약)
# ─────────────────────────────────────────────
MAIL_CATEGORIES = ["업무 요청", "회의/일정", "공유/참고", "광고/알림"]
CATEGORY_ICON = {"업무 요청": "🔴", "회의/일정": "📅", "공유/참고": "📎", "광고/알림": "📢"}

MAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "mails": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "category": {"type": "string", "enum": MAIL_CATEGORIES},
                    "summary": {"type": "string", "description": "메일 핵심 1~2문장"},
                    "kim_tasks": {"type": "array", "items": TASK_ITEM},
                    "events": {"type": "array", "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "when_text": {"type": "string", "description": "날짜·시간 표현 원문 그대로"},
                            "date_type": {"type": "string", "enum": ["exact", "approximate", "none"]},
                            "date": {"type": "string", "description": "exact일 때만 YYYY-MM-DD"},
                            "start": {"type": "string", "description": "시간이 나오면 HH:MM, 없으면 빈 문자열"},
                            "category": {"type": "string", "enum": EVENT_CATS},
                            "evidence": {"type": "string", "description": "메일에서 그대로 복사한 근거 문장"},
                        },
                        "required": ["title", "when_text", "date_type", "date", "start", "category", "evidence"],
                        "additionalProperties": False}},
                    "needs_reply": {"type": "boolean"},
                    "reply_draft": {"type": "string"},
                },
                "required": ["index", "category", "summary", "kim_tasks", "events", "needs_reply", "reply_draft"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["mails"],
    "additionalProperties": False,
}

SHORT_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"index": {"type": "integer"}, "summary": {"type": "string"}},
        "required": ["index", "summary"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False,
}

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {"subject": {"type": "string"}, "body": {"type": "string"}},
    "required": ["subject", "body"], "additionalProperties": False,
}


def mail_payload(mails):
    return "\n\n".join(f"[메일 {i}]\n보낸 사람: {m['from']} <{m['addr']}>\n받은 시각: {m['date']}\n"
                       f"제목: {m['subject']}\n본문: {m['body']}" for i, m in enumerate(mails))


def analyze_mails(mails):
    instructions = "\n".join([
        "당신은 '김 대리'의 개인 비서 '에이블'입니다. 김 대리가 받은 메일을 분석합니다.",
        f"오늘 날짜: {today.isoformat()} ({WEEKDAYS[today.weekday()]}요일). 여기서 '원문'은 각 메일의 제목과 본문입니다.",
        date_table(),
        "메일마다 index(입력 번호 그대로), category, summary(1~2문장), kim_tasks, events, needs_reply, reply_draft를 씁니다.",
        *TASK_RULES,
        "- 메일을 받은 김 대리에게 요청된 일이 kim_tasks입니다. 광고·자동 알림·단순 공유면 빈 배열.",
        "- events: 김 대리가 참석·진행해야 하는 회의·미팅·행사 일정만 넣습니다(마감 기한은 kim_tasks).",
        "  date_type/date는 기한 규칙과 같고, 시간이 메일에 있으면 start(HH:MM), 없으면 빈 문자열.",
        f"  category는 내용에 맞게({'/'.join(EVENT_CATS)}). 근거 문장 evidence 필수.",
        "- needs_reply: 사람이 보낸 메일이고 회신이 필요하면 true, 광고·자동 알림이면 false.",
        "- reply_draft: needs_reply가 true일 때만 김 대리 입장의 공손한 답장. 메일에 없는 날짜·숫자·약속은 만들지 않고,",
        "  끝에 '김 대리 드림'. false면 빈 문자열.",
    ])
    resp = get_client().responses.create(
        model=MODEL, instructions=instructions, input=mail_payload(mails),
        text={"format": {"type": "json_schema", "name": "mail_analysis", "schema": MAIL_SCHEMA, "strict": True}},
    )
    return json.loads(resp.output_text)["mails"]


def ensure_mail_summaries(mails):
    # 사이드바용 한 줄 요약. 한 번 요약한 메일은 저장해 두고 다시 요약하지 않음
    cache = data["mail_summaries"]
    missing = [m for m in mails if m["msg_id"] not in cache]
    if missing:
        try:
            resp = get_client().responses.create(
                model=MODEL,
                instructions="각 메일을 본문에 있는 내용만으로 40자 이내 한 문장으로 요약합니다. "
                             "지어내지 않고, 광고·자동 알림이면 '광고/알림:'으로 시작합니다. index는 입력 번호 그대로.",
                input=mail_payload(missing),
                text={"format": {"type": "json_schema", "name": "mail_short", "schema": SHORT_SCHEMA, "strict": True}},
            )
            for it in json.loads(resp.output_text)["items"]:
                if 0 <= it["index"] < len(missing):
                    cache[missing[it["index"]]["msg_id"]] = it["summary"]
            for k in list(cache)[:-100]:      # 최근 100개까지만 보관
                del cache[k]
            save_data()
        except Exception:
            pass
    return {m["msg_id"]: cache.get(m["msg_id"]) or ("(본문 앞부분) " + m["body"][:60] if m["body"] else "(본문 없음)")
            for m in mails}


def draft_mail(points, situation):
    resp = get_client().responses.create(
        model=MODEL,
        instructions="\n".join([
            "당신은 '김 대리'의 개인 비서 '에이블'입니다. 김 대리 이름으로 보낼 업무 메일을 씁니다.",
            "- 김 대리가 준 요점에 있는 사실만 씁니다. 새로운 날짜·숫자·약속·이름을 만들지 않습니다.",
            "- 정중한 비즈니스 메일 형식(인사 → 본론 → 마무리), 끝에 '김 대리 드림'.",
            "- subject는 내용을 잘 나타내는 짧은 제목.",
        ]),
        input=f"[요점]\n{points}\n\n[받는 사람/상황]\n{situation or '(없음)'}",
        text={"format": {"type": "json_schema", "name": "mail_draft", "schema": DRAFT_SCHEMA, "strict": True}},
    )
    return json.loads(resp.output_text)


def build_proposal(a, m):
    # 메일에서 찾은 김 대리 할 일·일정을 '제안'으로만 만든다 (사용자가 고르기 전에는 추가하지 않음)
    src = m["subject"] + " " + m["body"]
    items = []
    for t in a["kim_tasks"]:
        exact = t["due_type"] == "exact" and parse_date(t["due_date"])
        detail = clean_text(t["detail"])
        if t["requester"]:
            detail += f" / 요청: {t['requester']}"
        if t["due_text"] and not exact:
            detail += f" / 기한(대략): {t['due_text']}"
        items.append({"id": uuid.uuid4().hex[:8], "kind": "todo", "title": clean_text(t["title"]),
                      "detail": detail.strip(" /"), "due": t["due_date"] if exact else "", "due_text": t["due_text"],
                      "priority": t["priority"], "evidence": t["evidence"],
                      "verified": found_in(t["evidence"], src), "added": ""})
    for e in a["events"]:
        exact = e["date_type"] == "exact" and parse_date(e["date"])
        items.append({"id": uuid.uuid4().hex[:8], "kind": "event", "title": clean_text(e["title"]),
                      "date": e["date"] if exact else "", "start": e["start"] if parse_time(e["start"]) is not None else "",
                      "category": e["category"], "when_text": e["when_text"], "evidence": e["evidence"],
                      "verified": found_in(e["evidence"], src), "added": ""})
    ask = any(i["verified"] for i in items)
    return {"items": items, "decision": "pending" if ask else "none"}


def proposal_label(it):
    if it["kind"] == "todo":
        due = (f"마감 {fmt_date(parse_date(it['due']))}" if it["due"]
               else (f"기한 '{it['due_text']}'(대략) — 마감일 없이 할 일 목록에 추가" if it["due_text"]
                     else "기한 없음 — 할 일 목록에 추가"))
        return f"[할 일] {it['title']} · {due}"
    if not it["date"]:
        return f"[일정·{it['category']}] {it['title']} · '{it['when_text'] or '날짜 미정'}' — 날짜가 정해지지 않아 캘린더에 넣을 수 없어요"
    return (f"[일정·{it['category']}] {it['title']} · {fmt_date(parse_date(it['date']))} "
            f"{it['start'] or '시간 미정(종일)'}")


def can_add(it):
    return it["verified"] and (it["kind"] == "todo" or bool(it["date"]))


def add_proposal_item(m, it):
    src = f"메일({m['from']})"
    if it["kind"] == "todo":
        t = add_todo(it["title"], it["due"], it["priority"], it["detail"], src)
        it["added"] = "todo"
        return "todo", t
    r = add_event(it["title"], it["date"], f"메일: {m['subject']}", src, it["start"], "", it["category"])
    it["added"] = {"success": "event", "needs_approval": "approval"}.get(r["status"], "failed")
    return it["added"], r


def apply_mail_proposal(m, selected):
    n_todo = n_ev = n_wait = 0
    for it in m["proposal"]["items"]:
        if it["id"] not in selected or it["added"] or not can_add(it):
            continue
        if it["kind"] == "event" and st.session_state.get("pending_event"):
            it["added"] = "deferred"                 # 다른 일정이 승인 대기 중이면 나중에 추가
            n_wait += 1
            continue
        kind, _ = add_proposal_item(m, it)
        n_todo += kind == "todo"
        n_ev += kind == "event"
        n_wait += kind == "approval"
    m["proposal"]["decision"] = "added"
    save_data()
    msg = f"할 일 {n_todo}건, 일정 {n_ev}건을 추가했어요."
    if n_wait:
        msg += f" 충돌 확인이 필요한 일정 {n_wait}건은 화면 위 카드에서 시간을 골라 주세요."
    if not st.session_state.get("pending_event"):
        st.session_state.pending_toast = msg
    return msg


def added_line(it):
    return {"todo": "할 일 목록에 추가됨" + (" (캘린더에 마감 표시)" if it.get("due") else ""),
            "event": "캘린더에 추가됨",
            "approval": "기존 일정과 겹쳐 화면 위 카드에서 시간 선택이 필요했어요",
            "deferred": "다른 일정 승인 대기 중이라 아직 추가하지 않았어요",
            "failed": "추가하지 못했어요"}.get(it["added"], "추가하지 않음")


def render_proposal(m, k):
    p = m["proposal"]
    vis = [i for i in p["items"] if i["verified"]]
    hidden = len(p["items"]) - len(vis)
    if p["decision"] == "pending" and vis:
        with st.container(border=True):
            st.markdown(f"**📅 이 메일에서 김 대리님의 일정·할 일 {len(vis)}건을 찾았어요. 캘린더에 추가할까요?**")
            for it in vis:
                st.checkbox(proposal_label(it), value=can_add(it), key=f"mp_{it['id']}", disabled=not can_add(it))
                st.caption(f"근거: “{it['evidence']}”")
            a, b, _ = st.columns([1.3, 1.3, 2])
            if a.button("네, 추가할게요", key=f"mp_yes_{k}", type="primary", use_container_width=True):
                apply_mail_proposal(m, [it["id"] for it in vis if st.session_state.get(f"mp_{it['id']}")])
                st.rerun()
            if b.button("아니요, 추가 안 함", key=f"mp_no_{k}", use_container_width=True):
                p["decision"] = "declined"
                save_data()
                st.session_state.pending_toast = "캘린더에 추가하지 않았어요. 메일 요약에서만 볼 수 있어요."
                st.rerun()
    elif p["decision"] == "added":
        for it in vis:
            st.markdown(f"- {proposal_label(it).split(' — ')[0]} → **{added_line(it)}**")
            if it["added"] == "deferred" and st.button("지금 추가", key=f"mp_retry_{it['id']}"):
                add_proposal_item(m, it)
                save_data()
                st.rerun()
    elif p["decision"] == "declined":
        st.caption("캘린더에 추가하지 않은 항목 (이 메일 요약에서만 표시)")
        for it in vis:
            st.markdown(f"- {proposal_label(it).split(' — ')[0]}")
        if st.button("다시 물어보기", key=f"mp_again_{k}"):
            p["decision"] = "pending"
            save_data()
            st.rerun()
    if hidden:
        st.caption(f"원문에서 근거를 찾지 못한 항목 {hidden}건은 제안하지 않았어요.")


def mail_key(m):
    return hashlib.md5(m["msg_id"].encode("utf-8")).hexdigest()[:10]


def run_mail_analysis(n=5, include_done=False):
    # 최근 받은 메일을 요약·분석해 '캘린더 추가 제안'을 만든다 (자동으로 추가하지 않음)
    fetch_mails.clear()
    got = fetch_mails(n)
    if "error" in got:
        raise RuntimeError(f"메일을 불러오지 못했습니다: {got['error']}")
    me = (os.getenv("GMAIL_ADDRESS") or "").lower()
    processed = data["processed_mails"]
    targets = [m for m in got["mails"] if m["addr"].lower() != me and (include_done or m["msg_id"] not in processed)]
    if not targets:
        data["mail_analysis"]["when"] = now_kst()
        save_data()
        return 0, 0
    results = []
    for a in analyze_mails(targets):
        if 0 <= a["index"] < len(targets):
            m = targets[a["index"]]
            results.append({**m, "category": a["category"], "summary": a["summary"], "needs_reply": a["needs_reply"],
                            "reply_draft": a["reply_draft"], "proposal": build_proposal(a, m), "replied": False})
    for m in targets:
        if m["msg_id"] not in processed:
            processed.append(m["msg_id"])
    del processed[:-500]
    ids = {r["msg_id"] for r in results}
    ma = data["mail_analysis"]
    ma["mails"] = results + [x for x in ma["mails"] if x["msg_id"] not in ids]
    del ma["mails"][20:]
    ma["when"] = now_kst()
    save_data()
    return len(results), sum(1 for r in results for i in r["proposal"]["items"] if i["verified"])


def _save_mail_auto():
    data["settings"]["mail_auto"] = st.session_state.set_mail_auto
    save_data()


# 앱을 열 때(세션당 한 번) 새 메일이 있으면 자동으로 요약·분석하고 캘린더 추가 여부를 물어봄
if (gmail_ready() and os.getenv("OPENAI_API_KEY") and data["settings"].get("mail_auto", True)
        and not st.session_state.get("mail_auto_checked")):
    st.session_state.mail_auto_checked = True
    try:
        _n_m, _n_items = run_mail_analysis(5)
        if _n_items:
            st.toast(f"새 메일에서 일정·할 일 {_n_items}건을 찾았어요. 메일 비서 탭에서 캘린더에 추가할지 골라 주세요.", icon="📥")
    except Exception:
        pass


def fill_mail(kind):
    if kind == "status":
        st.session_state.mail_subject = f"[에이블] {today.isoformat()} 업무 현황"
        st.session_state.mail_body = status_report_text()
        st.session_state.mail_context = "[참고 자료]\n" + brief_context()
    elif kind == "meeting" and latest_note():
        n = latest_note()
        st.session_state.mail_subject = f"[에이블] 회의록 - {n['title']}"
        st.session_state.mail_body = note_markdown(n)
        st.session_state.mail_context = "[참고 자료: 회의 원문]\n" + n["text"][:6000]
    elif kind == "clear":
        st.session_state.mail_subject = ""
        st.session_state.mail_body = ""
        st.session_state.mail_context = ""
    st.session_state.mail_to = my_email()


def _pick_contact():
    c = next((x for x in data["contacts"] if x["id"] == st.session_state.get("mail_pick")), None)
    if c:
        st.session_state.mail_to = c["email"]


def _write_to(email_addr):
    st.session_state.mail_to = email_addr
    st.session_state.pending_toast = "✉️ 메일 보내기 탭의 받는 사람에 넣었어요."


def _save_confirm():
    data["settings"]["confirm_before_send"] = st.session_state.set_confirm
    save_data()


def _add_sender(name, addr):
    upsert_contact(name or addr.split("@")[0], addr, memo="받은 메일에서 추가")
    st.session_state.pending_toast = f"📇 {name or addr}님을 연락처에 추가했어요."


with tab_mail:
    st.markdown("연락처로 메일을 보내고(보내기 전에 에이블이 한 번 더 확인), 받은 메일을 요약해 "
                "**김 대리에게 요청된 일**을 할 일에 올립니다.")

    with st.expander(f"⚙️ 메일 설정 · 내 이메일: {my_email() or '미설정'}"):
        new_addr = st.text_input("내 이메일 주소", value=my_email(), key="set_my_email")
        st.caption("한 번 저장하면 메일 보내기의 기본 받는 사람, 회의록 메일, 에이블 비서의 ‘내 메일로 보내줘’에 항상 적용됩니다.")
        if st.button("저장", key="save_my_email"):
            if EMAIL_RE.fullmatch(new_addr.strip()):
                data["settings"]["my_email"] = new_addr.strip()
                save_data()
                st.session_state.mail_to = new_addr.strip()
                st.session_state.pending_toast = "내 이메일 주소를 저장했어요."
                st.rerun()
            else:
                st.error("이메일 주소 형식을 확인해 주세요.")
        st.toggle("💬 에이블 비서가 메일을 보낼 때, AI 확인 후 내가 한 번 더 보고 보내기",
                  value=data["settings"].get("confirm_before_send", True), key="set_confirm", on_change=_save_confirm)
        st.caption("음성으로 요청한 메일은 AI 확인에서 문제가 없으면 바로 보내요. 이 설정은 글로 요청할 때 적용됩니다. "
                   "끄면 글로 요청해도 문제가 없을 때 바로 발송해요. 확인할 점이 있으면 \"보내줘\"라고 말해도 보낼 수 있어요.")

    if not gmail_ready():
        st.warning("api_key.txt에 GMAIL_ADDRESS, GMAIL_APP_PASSWORD가 있어야 메일을 보내고 받을 수 있습니다.")

    sub_send, sub_inbox, sub_contacts = st.tabs(["✉️ 메일 보내기", "📥 받은 메일 요약", "📇 연락처"])

    # --- 메일 보내기 ---
    with sub_send:
        st.session_state.setdefault("mail_to", my_email())
        if st.session_state.pop("mail_clear", False):
            fill_mail("clear")

        q1, q2, q3 = st.columns(3)
        q1.button("📊 오늘 업무 현황 불러오기", on_click=fill_mail, args=("status",), use_container_width=True)
        q2.button("📝 최근 회의록 불러오기", on_click=fill_mail, args=("meeting",), use_container_width=True,
                  disabled=latest_note() is None)
        q3.button("🧹 새로 쓰기", on_click=fill_mail, args=("clear",), use_container_width=True)

        with st.expander("🤖 에이블에게 메일 초안 부탁하기"):
            points = st.text_area("메일에 담을 요점", key="mail_points", height=100,
                                  placeholder="예) 경쟁사 분석 보고서 다음 주까지 공유 예정, 자료 요청 감사 인사")
            situation = st.text_input("받는 사람 / 상황 (선택)", key="mail_situation", placeholder="예) 팀장님께 보고")
            if st.button("✍️ 초안 작성", disabled=not points.strip()):
                ok_draft = False
                try:
                    with st.spinner("초안을 쓰는 중..."):
                        dr = draft_mail(points, situation)
                    st.session_state.mail_subject = dr["subject"]
                    st.session_state.mail_body = dr["body"]
                    st.session_state.mail_context = f"[김 대리가 준 요점]\n{points}\n[상황] {situation or '-'}"
                    st.session_state.pending_toast = "초안을 아래에 채워 넣었어요. 확인 후 보내세요."
                    ok_draft = True
                except Exception as e:
                    st.error(f"초안을 만들지 못했습니다: {e}")
                if ok_draft:
                    st.rerun()

        cmap = {c["id"]: c for c in data["contacts"]}
        if cmap:
            st.selectbox("📇 연락처에서 받는 사람 고르기", [""] + list(cmap), key="mail_pick", on_change=_pick_contact,
                         format_func=lambda i: "선택하세요" if not i else f"{contact_label(cmap[i])} <{cmap[i]['email']}>")
        to = st.text_input("받는 사람 (이메일 주소 또는 연락처 이름)", key="mail_to")
        subject = st.text_input("제목", key="mail_subject")
        body = st.text_area("본문", key="mail_body", height=260)
        if to.strip():
            sent = confirm_and_send("compose", to, subject, body,
                                    st.session_state.get("mail_context") or "(김 대리가 직접 작성한 메일)")
            if sent == "sent":
                st.session_state.mail_clear = True
                st.session_state.pending_toast = "메일을 보냈어요."
                st.rerun()

    # --- 받은 메일 요약 ---
    with sub_inbox:
        res = data["mail_analysis"]
        st.caption("최근 받은 메일을 에이블이 요약하고, 김 대리가 할 일이나 참석할 일정이 있으면 캘린더에 추가할지 물어봐요. "
                   "추가하지 않으면 이 화면에서만 볼 수 있어요.")
        o1, o2, o3 = st.columns(3)
        n_mail = o1.select_slider("분석할 최근 메일 수", options=[5, 10, 15, 20], value=5)
        include_done = o2.checkbox("이미 분석한 메일도 다시 분석", value=False)
        o3.toggle("앱을 열 때 새 메일 자동 분석", value=data["settings"].get("mail_auto", True), key="set_mail_auto",
                  on_change=_save_mail_auto)
        if st.button("받은 메일 요약·분석하기", type="primary", disabled=not gmail_ready()):
            done, msg = False, ""
            try:
                with st.spinner("메일을 불러와 요약·분석하는 중..."):
                    n_m, n_items = run_mail_analysis(n_mail, include_done)
                msg = (f"메일 {n_m}통 요약 · 캘린더 추가 제안 {n_items}건" if n_m else "새로 분석할 메일이 없습니다.")
                done = True
            except Exception as e:
                st.error(str(e))
            if done:
                st.session_state.pending_toast = msg
                st.rerun()

        mails = res["mails"]
        if res["when"]:
            st.caption(f"마지막 분석: {res['when']}")
        n_ask = sum(1 for m in mails if m["proposal"]["decision"] == "pending")
        if n_ask:
            st.info(f"메일 {n_ask}통에서 김 대리님의 일정·할 일을 찾았어요. 아래에서 캘린더에 추가할지 골라 주세요.")
        if not mails and res["when"]:
            st.caption("분석한 메일이 없습니다.")

        known = {c["email"].lower() for c in data["contacts"]}
        work = [m for m in mails if m["category"] != "광고/알림"]
        ads = [m for m in mails if m["category"] == "광고/알림"]
        work.sort(key=lambda m: m["proposal"]["decision"] != "pending")     # 물어볼 메일을 위로
        for m in work:
            k = mail_key(m)
            with st.container(border=True):
                a, b = st.columns([5, 1.4])
                a.markdown(f"{CATEGORY_ICON[m['category']]} **{m['subject']}**  \n"
                           f":gray[{m['from']} · {m['date']} · {m['category']}]")
                if m["addr"].lower() not in known:
                    b.button("📇 연락처에 추가", key=f"addc_{k}", on_click=_add_sender, args=(m["from"], m["addr"]),
                             use_container_width=True)
                st.write(m["summary"])
                render_proposal(m, k)
                if m["needs_reply"]:
                    if m.get("replied"):
                        st.success("답장을 보냈습니다.")
                    else:
                        with st.expander(f"✉️ {m['from']}님께 보낼 답장 초안 보기·수정"):
                            draft = st.text_area("답장", value=m["reply_draft"], height=200,
                                                 key=f"reply_{k}", label_visibility="collapsed")
                            subj = m["subject"] if m["subject"].lower().startswith("re:") else "Re: " + m["subject"]
                            sent = confirm_and_send(
                                f"reply_{k}", m["addr"], subj, draft,
                                f"[받은 메일]\n보낸 사람: {m['from']}\n제목: {m['subject']}\n본문: {m['body']}",
                                m["msg_id"])
                            if sent == "sent":
                                m["replied"] = True
                                save_data()
                                st.session_state.pending_toast = f"{m['from']}님께 답장을 보냈어요."
                                st.rerun()
        if ads:
            with st.expander(f"📢 광고/알림 메일 {len(ads)}통"):
                for m in ads:
                    st.markdown(f"- **{m['subject']}** · {m['from']} — {m['summary']}")

    # --- 연락처 ---
    with sub_contacts:
        st.caption("연락처에 저장하면 에이블 비서에게 \"박과장님께 회의 요약 메일 보내줘\"처럼 이름만 말해도 보낼 수 있어요. "
                   "보내기 전에는 에이블이 받는 사람과 내용을 한 번 더 확인합니다.")
        with st.expander("➕ 연락처 추가", expanded=not data["contacts"]):
            with st.form("ct_add", clear_on_submit=True):
                c1, c2, c3 = st.columns(3)
                nm = c1.text_input("이름 *", placeholder="박민수")
                rl = c2.text_input("직함", placeholder="과장")
                tm = c3.text_input("소속", placeholder="마케팅팀")
                c4, c5 = st.columns(2)
                em = c4.text_input("이메일 *", placeholder="park@company.com")
                al = c5.text_input("부르는 호칭 (선택)", placeholder="박과장")
                mm = st.text_input("메모", placeholder="예: 신제품 마케팅 담당")
                if st.form_submit_button("저장", type="primary"):
                    if not nm.strip() or not EMAIL_RE.fullmatch(em.strip()):
                        st.error("이름과 올바른 이메일 주소를 입력하세요.")
                    else:
                        upsert_contact(nm, em, rl, tm, al, mm)
                        st.session_state.pending_toast = f"📇 {nm.strip()}님을 연락처에 저장했어요."
                        st.rerun()

        cq = st.text_input("🔍 연락처 검색", key="ct_q", placeholder="이름, 직함, 소속, 이메일")
        shown = [c for c in data["contacts"]
                 if cq.strip().lower() in " ".join([c["name"], c["role"], c["team"], c["alias"], c["email"]]).lower()]
        shown.sort(key=lambda c: c["name"])
        if not shown:
            st.info("저장된 연락처가 없습니다." if not cq.strip() else "검색 결과가 없습니다.")
        for c in shown:
            with st.container(border=True):
                if st.session_state.get("ct_edit") == c["id"]:
                    e1, e2, e3 = st.columns(3)
                    nm = e1.text_input("이름", value=c["name"], key=f"ce_n_{c['id']}")
                    rl = e2.text_input("직함", value=c["role"], key=f"ce_r_{c['id']}")
                    tm = e3.text_input("소속", value=c["team"], key=f"ce_t_{c['id']}")
                    e4, e5 = st.columns(2)
                    em = e4.text_input("이메일", value=c["email"], key=f"ce_e_{c['id']}")
                    al = e5.text_input("호칭", value=c["alias"], key=f"ce_a_{c['id']}")
                    mm = st.text_input("메모", value=c["memo"], key=f"ce_m_{c['id']}")
                    s1, s2, _ = st.columns([1, 1, 4])
                    if s1.button("저장", key=f"ce_save_{c['id']}", type="primary"):
                        if nm.strip() and EMAIL_RE.fullmatch(em.strip()):
                            c.update(name=nm.strip(), role=rl.strip(), team=tm.strip(), email=em.strip(),
                                     alias=al.strip(), memo=mm.strip())
                            save_data()
                            st.session_state.ct_edit = None
                            st.rerun()
                        else:
                            st.error("이름과 올바른 이메일 주소를 입력하세요.")
                    if s2.button("취소", key=f"ce_cancel_{c['id']}"):
                        st.session_state.ct_edit = None
                        st.rerun()
                else:
                    a, b, c_, d_ = st.columns([5, 1.3, 0.7, 0.7])
                    a.markdown(f"**{contact_label(c)}**" + (f" · 호칭: {c['alias']}" if c["alias"] else "")
                               + f"  \n:gray[{c['email']}]")
                    if c["memo"]:
                        a.caption(c["memo"])
                    b.button("✉️ 메일 쓰기", key=f"ct_mail_{c['id']}", on_click=_write_to, args=(c["email"],),
                             use_container_width=True)
                    if c_.button("✏️", key=f"ct_edit_{c['id']}", help="수정"):
                        st.session_state.ct_edit = c["id"]
                        st.rerun()
                    if d_.button("🗑️", key=f"ct_del_{c['id']}", help="삭제"):
                        data["contacts"] = [x for x in data["contacts"] if x["id"] != c["id"]]
                        save_data()
                        st.rerun()


# ─────────────────────────────────────────────
# 사이드바 (챕터를 누르면 펼쳐짐)
# ─────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 🤖 에이블")
    st.caption("김 대리님의 AI 개인비서")

    # 0) GPT처럼 새 채팅 + 대화 기록
    st.button("➕ 새 채팅", key="new_chat_side", on_click=new_chat_cb, type="primary", use_container_width=True)
    chats = sorted(data["chats"], key=lambda c: c["updated"], reverse=True)
    with st.expander(f"💬 대화 기록 ({len(chats)}/{MAX_CHATS})", expanded=True):
        if not chats:
            st.caption("아직 대화가 없습니다. 🤖 에이블 비서 탭에서 대화를 시작해 보세요.")
        for c in chats:
            a, b = st.columns([4, 1], gap="small")
            cur = c["id"] == st.session_state.get("chat_id")
            a.button(("🟢 " if cur else "💬 ") + c["title"], key=f"open_{c['id']}", on_click=open_chat_cb,
                     args=(c["id"],), use_container_width=True, help=f"{c['updated']} · 질문 {len(c['turns'])}개")
            with b.container(key=f"chatdel_{c['id']}"):          # 휴지통 아이콘이 잘리지 않게 여백 없이
                st.button("🗑", key=f"delchat_{c['id']}", on_click=delete_chat_cb, args=(c["id"],),
                          help="대화 삭제", use_container_width=True)
        st.caption(f"최근 대화 {MAX_CHATS}개까지 보관하고, 대화마다 최근 질문·답변 {MAX_TURNS}개를 기억해요.")

    # 1) 앞으로 3일 일정·할 일 마감 (매 실행마다 data에서 다시 계산 → 등록·수정·삭제 즉시 반영)
    undone_all = [t for t in data["todos"] if not t["done"]]
    overdue = sorted([t for t in undone_all if parse_date(t["due"]) and parse_date(t["due"]) < today],
                     key=lambda t: t["due"])
    days3 = [today + dt.timedelta(days=i) for i in range(3)]
    ev3 = {d: sorted([e for e in data["events"] if parse_date(e["date"]) == d],
                     key=lambda e: e["start"] or "00:00") for d in days3}
    td3 = {d: sorted([t for t in undone_all if parse_date(t["due"]) == d],
                     key=lambda t: PRIORITY_ORDER.get(t["priority"], 1)) for d in days3}
    n_ev3 = sum(len(v) for v in ev3.values())
    n_td3 = sum(len(v) for v in td3.values())
    with st.expander(f"📅 앞으로 3일 (일정 {n_ev3} · 마감 {n_td3})", expanded=True):
        st.caption("일정은 종류별 색, 할 일은 분류별 색으로 구분해요.")
        if overdue:
            st.markdown("**⚠️ 기한 지난 할 일**")
            for t in overdue:
                st.markdown(todo_html(t), unsafe_allow_html=True)
        for d, label in zip(days3, ["오늘", "내일", "모레"]):
            st.markdown(f"**{label} · {fmt_date(d)}**")
            if not ev3[d] and not td3[d]:
                st.caption("일정·마감 없음")
                continue
            for e in ev3[d]:
                st.markdown(f'{badge(e["category"], ev_color(e))}<b>{html.escape(e["title"])}</b>'
                            f'<br><span class="able-meta">🕒 {event_time_text(e)}</span>',
                            unsafe_allow_html=True)
            for t in td3[d]:
                st.markdown(todo_html(t), unsafe_allow_html=True)
        no_due = sum(1 for t in undone_all if not t["due"])
        if no_due:
            st.caption(f"기한 없는 할 일 {no_due}건은 📊 대시보드에서 확인하세요.")

    # 2) 가장 최근 회의록 요약
    with st.expander("📝 최근 회의록 요약"):
        note = latest_note()
        if not note:
            st.caption("아직 분석한 회의가 없습니다.")
        else:
            st.markdown(f"**{note['title']}**")
            st.caption(f"{note['when']} · {note['source']}")
            if note["summary"]:
                st.markdown("\n".join(f"- {s}" for s in note["summary"][:5]))
            elif note["minutes"]:
                st.caption("이전 버전 회의록입니다. 🎙️ 회의 분석 탭에서 내용을 볼 수 있어요.")
            if note["kim_tasks"]:
                st.markdown("**김 대리 업무**")
                st.markdown("\n".join(f"- {t['title']} ({t['due_text'] or '기한 언급 없음'})"
                                      for t in note["kim_tasks"]))
            st.caption("자세한 내용은 🎙️ 회의 분석 탭에서 볼 수 있어요.")

    # 3) 최근 받은 메일 5개 요약
    _n_ask = sum(1 for m in data["mail_analysis"]["mails"] if m["proposal"]["decision"] == "pending")
    with st.expander("📥 최근 받은 메일 5개" + (f" · 캘린더 추가 제안 {_n_ask}" if _n_ask else "")):
        if _n_ask:
            st.info(f"메일 {_n_ask}통에서 일정·할 일을 찾았어요. 📥 메일 비서 → 받은 메일 요약에서 추가할지 골라 주세요.")
        if not gmail_ready():
            st.caption("api_key.txt에 GMAIL_ADDRESS, GMAIL_APP_PASSWORD가 있어야 볼 수 있습니다.")
        else:
            if st.button("🔄 새로고침", key="side_mail_refresh", use_container_width=True):
                fetch_mails.clear()
            got = fetch_mails(5)
            if "error" in got:
                st.caption(f"메일을 불러오지 못했습니다: {got['error']}")
            elif not got["mails"]:
                st.caption("받은편지함이 비어 있습니다.")
            else:
                sums = ensure_mail_summaries(got["mails"])
                for m in got["mails"]:
                    st.markdown(("🔵 " if m["unread"] else "") + f"**{m['subject'][:30]}**  \n"
                                f":gray[{m['from']} · {m['date']}]")
                    st.caption(sums[m["msg_id"]])
                st.caption("🔵 = 읽지 않은 메일")
