from types import NoneType

# AI 개인비서 '에이블' (Streamlit)
# 실행: streamlit run app.py
# 같은 폴더에 필요한 파일:
#   - api_key.txt : OPENAI_API_KEY=..., GMAIL_ADDRESS=..., GMAIL_APP_PASSWORD=..., (선택) MY_EMAIL=...
#   - meeting.txt : (선택) 회의 분석 탭에서 기본으로 불러올 회의록
#
# 화면 구성
#   메인 탭  : 🤖 에이블 비서 (입력창 맨 위·최근 대화가 위로, 에이블 로고를 눌러 녹음·생각 중·말하는 중 애니메이션, 음성/텍스트 대화, 파일 첨부, 이메일 발송 도구, 음성 답변, 멀티턴, 대화 기록/새 채팅)
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

import base64
import time
import streamlit as st
import streamlit.components.v1 as components
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
                     "verified": True, "kind": "기타", "end_date": "", "start": "", "end": "", "place": "",
                     "kim_attends": False, "cal_status": "reference", "event_ids": [], "conflicts": []}.items():
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


STT_CHAT_MODELS = ["gpt-4o-transcribe", "whisper-1"]   # 정확도가 높은 모델 먼저, 안 되면 whisper-1로


def speech_to_text(audio_bytes, name="voice.wav", hint=None, models=("whisper-1",)):
    last_err = None
    for model in models:
        kwargs = {"model": model, "file": (name, audio_bytes), "language": "ko"}
        if hint:
            kwargs["prompt"] = hint
        try:
            return get_client().audio.transcriptions.create(**kwargs).text
        except Exception as e:              # 모델을 쓸 수 없으면 다음 모델로
            last_err = e
    raise last_err


def stt_hint():
    # 음성 인식이 이름·고유명사를 잘 받아 적도록, 자주 나올 단어를 힌트로 줌 (연락처·일정 제목·종류)
    words = ["에이블", "김 대리", "일정", "할 일", "캘린더", "메일", "회의록", "추가해줘", "삭제해줘", "취소해줘",
             "이번 주", "다음 주", "오전", "오후"]
    for c in data["contacts"]:
        words += [c["name"] + c["role"], c["alias"], c["team"]]
    up = sorted([e for e in data["events"] if (d := parse_date(e["date"])) and d >= today],
                key=lambda e: e["date"])[:20]
    words += [e["title"] for e in up] + [t["title"] for t in data["todos"] if not t["done"]][:15]
    words += EVENT_CATS + list(TODO_CATS)
    seen, out = set(), []
    for w in words:
        w = clean_text(w)
        if w and w not in seen:
            seen.add(w)
            out.append(w)
    text = ", ".join(out)[:600]               # 힌트가 너무 길면 잘리므로 적당히
    return f"김 대리가 AI 비서 에이블에게 일정·할 일·메일을 부탁하는 한국어 음성입니다. 자주 나오는 말: {text}"


def text_to_speech(text, voice="alloy"):
    return get_client().audio.speech.create(model="gpt-4o-mini-tts", voice=voice, input=text[:4000]).content


# ─────────────────────────────────────────────
# 🎙️ 에이블 로고 녹음기 (Streamlit 커스텀 컴포넌트)
#   로고 클릭 → 녹음(목소리에 맞춰 파동) → 다시 클릭 → 파이썬으로 전달 → 생각 중 → 답변 음성 재생(말하는 중)
#   부품 html은 앱이 실행될 때 임시 폴더에 직접 만들어서, GitHub에는 app.py만 올리면 됨
# ─────────────────────────────────────────────
ABLE_LOGO_B64 = "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"

ABLE_MIC_HTML = r'''<!doctype html>
<html><head><meta charset="utf-8">
<style>
  :root { --teal:#0b9ea4; --rec:#ef4f5a; --text:#31333f; --sub:rgba(49,51,63,.6); --btn:#ffffff; --ring:rgba(11,158,164,.25); }
  html,body { margin:0; padding:0; background:transparent; overflow:hidden;
              font-family:"Source Sans Pro","Noto Sans KR",sans-serif; color:var(--text); }
  .wrap { display:flex; align-items:center; gap:14px; padding:4px 2px; }
  .wrap.big { flex-direction:column; gap:8px; }
  .stage { position:relative; flex:0 0 auto; }
  canvas { position:absolute; inset:0; pointer-events:none; }
  .btn { position:absolute; border-radius:50%; border:2px solid var(--ring); background:var(--btn);
         cursor:pointer; display:flex; align-items:center; justify-content:center; padding:0;
         box-shadow:0 6px 22px rgba(11,158,164,.22); transition:transform .15s, box-shadow .3s, border-color .3s; outline:none; }
  .btn:focus-visible { box-shadow:0 0 0 4px rgba(11,158,164,.45); }
  .btn:active { transform:scale(.96); }
  .logo { width:62%; height:62%; background:var(--teal); transition:background .3s;
          -webkit-mask:url("data:image/png;base64,__LOGO_B64__") center/contain no-repeat;
                  mask:url("data:image/png;base64,__LOGO_B64__") center/contain no-repeat; }
  /* 대기: 숨쉬기 */
  .idle .btn, .blocked .btn { animation:breathe 3.2s ease-in-out infinite; }
  @keyframes breathe { 0%,100% { transform:scale(1); box-shadow:0 6px 22px rgba(11,158,164,.18);} 50% { transform:scale(1.045); box-shadow:0 8px 30px rgba(11,158,164,.38);} }
  /* 녹음 중 */
  .recording .btn { border-color:rgba(239,79,90,.55); box-shadow:0 0 0 6px rgba(239,79,90,.12), 0 8px 26px rgba(239,79,90,.25); }
  /* 생각 중: 도는 링 */
  .spin { position:absolute; border-radius:50%; opacity:0; transition:opacity .3s; pointer-events:none;
          background:conic-gradient(from 0deg, transparent 0 55%, var(--teal) 85%, transparent 100%);
          -webkit-mask:radial-gradient(farthest-side, transparent calc(100% - 5px), #000 calc(100% - 4px));
                  mask:radial-gradient(farthest-side, transparent calc(100% - 5px), #000 calc(100% - 4px));
          animation:spin 1s linear infinite; }
  .thinking .spin { opacity:1; }
  .thinking .btn { cursor:progress; }
  .thinking .logo { animation:think 1.6s ease-in-out infinite; }
  @keyframes spin { to { transform:rotate(360deg); } }
  @keyframes think { 0%,100% { opacity:1; } 50% { opacity:.45; } }
  .info { display:flex; flex-direction:column; gap:2px; min-width:0; }
  .big .info { align-items:center; text-align:center; }
  .label { font-size:15px; font-weight:600; }
  .sub { font-size:13px; color:var(--sub); }
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%; background:var(--rec); margin-right:6px;
         vertical-align:1px; animation:blink 1s infinite; }
  @keyframes blink { 50% { opacity:.2; } }
  .err { color:var(--rec); }
</style></head>
<body>
<div class="wrap idle" id="wrap">
  <div class="stage" id="stage">
    <canvas id="cv"></canvas>
    <div class="spin" id="spin"></div>
    <button class="btn" id="btn" aria-label="에이블에게 말하기"><div class="logo" id="logo"></div></button>
  </div>
  <div class="info"><div class="label" id="label"></div><div class="sub" id="sub"></div></div>
</div>
<script>
// ── Streamlit 컴포넌트 통신 (라이브러리 없이 postMessage로 직접) ──
function toST(type, data) { window.parent.postMessage(Object.assign({isStreamlitMessage: true, type: type}, data || {}), "*"); }
function setHeight() { toST("streamlit:setFrameHeight", {height: document.getElementById("wrap").offsetHeight + 8}); }
function setValue(v) { toST("streamlit:setComponentValue", {value: v, dataType: "json"}); }

const $ = (id) => document.getElementById(id);
let state = "idle", size = "", sentId = "", lastPlayId = "", thinkTimer = null;
let recorder = null, chunks = [], stream = null, recStart = 0, timerInt = null;
let actx = null, analyser = null, player = null, levelBuf = null, fakeLevel = false;
const bars = new Array(72).fill(0);

const TEXT = {
  idle:      ["에이블을 눌러 말하기", "누르면 녹음이 시작되고, 다시 누르면 에이블에게 전달돼요"],
  recording: ["", "다 말했으면 로고를 한 번 더 눌러 주세요"],
  thinking:  ["에이블이 생각 중이에요…", "잠시만 기다려 주세요"],
  speaking:  ["에이블이 말하는 중", "로고를 누르면 멈춰요"],
  blocked:   ["🔊 답변이 준비됐어요", "로고를 누르면 들려 드려요"],
};

function setState(s, subText) {
  state = s;
  $("wrap").className = "wrap " + size + " " + s;
  const t = TEXT[s] || ["", ""];
  $("label").innerHTML = s === "recording" ? '<span class="dot"></span>듣고 있어요 · <span id="tm">0:00</span>' : t[0];
  $("sub").textContent = subText || t[1];
  $("sub").className = "sub" + (subText && s === "idle" ? " err" : "");
  if (s !== "thinking" && thinkTimer) { clearTimeout(thinkTimer); thinkTimer = null; }
  setHeight();
}

function layout(sz) {
  size = sz === "big" ? "big" : "compact";
  const B = size === "big" ? 120 : 76, S = size === "big" ? 210 : 132;
  const stage = $("stage"), cv = $("cv"), btn = $("btn"), sp = $("spin");
  stage.style.width = stage.style.height = S + "px";
  const dpr = window.devicePixelRatio || 1;
  cv.width = cv.height = S * dpr; cv.style.width = cv.style.height = S + "px";
  const off = (S - B) / 2;
  Object.assign(btn.style, {width: B + "px", height: B + "px", left: off + "px", top: off + "px"});
  const so = off - 7;
  Object.assign(sp.style, {width: (B + 14) + "px", height: (B + 14) + "px", left: so + "px", top: so + "px"});
  setState(state);
}

// ── 파동 그리기: 녹음(마이크 소리) · 말하기(답변 소리)에 맞춰 로고 주변 막대가 출렁임 ──
function level() {
  if (fakeLevel) { const t = performance.now() / 1000; return 0.35 + 0.25 * Math.sin(t * 9) * Math.sin(t * 2.3); }
  if (!analyser) return 0;
  analyser.getByteFrequencyData(levelBuf);
  let s = 0; for (let i = 2; i < levelBuf.length * 0.5; i++) s += levelBuf[i];
  return Math.min(1, s / (levelBuf.length * 0.5) / 110);
}
function draw() {
  const cv = $("cv"), g = cv.getContext("2d"), dpr = window.devicePixelRatio || 1;
  const S = cv.width / dpr, B = size === "big" ? 120 : 76, r0 = B / 2 + 8, maxL = (S / 2 - r0) - 4;
  g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, S, S);
  const active = state === "recording" || state === "speaking";
  const lv = active ? level() : 0;
  const color = state === "recording" ? "239,79,90" : "11,158,164";
  const t = performance.now() / 1000;
  for (let i = 0; i < bars.length; i++) {
    const wobble = 0.55 + 0.45 * Math.sin(i * 0.9 + t * 6) * Math.cos(i * 0.37 - t * 3);
    const target = active ? lv * (0.35 + 0.65 * Math.abs(wobble)) : 0;
    bars[i] += (target - bars[i]) * 0.25;
    const L = 2 + bars[i] * maxL;
    if (bars[i] < 0.01 && !active) continue;
    const a = (i / bars.length) * Math.PI * 2 - Math.PI / 2;
    g.strokeStyle = "rgba(" + color + "," + (0.35 + bars[i] * 0.65) + ")";
    g.lineWidth = size === "big" ? 3 : 2.2; g.lineCap = "round";
    g.beginPath();
    g.moveTo(S / 2 + Math.cos(a) * r0, S / 2 + Math.sin(a) * r0);
    g.lineTo(S / 2 + Math.cos(a) * (r0 + L), S / 2 + Math.sin(a) * (r0 + L));
    g.stroke();
  }
  $("logo").style.transform = active ? "scale(" + (1 + lv * 0.14) + ")" : "";
  requestAnimationFrame(draw);
}

function ensureCtx() {
  if (!actx) { const C = window.AudioContext || window.webkitAudioContext; if (C) actx = new C(); }
  if (actx && actx.state === "suspended") actx.resume().catch(() => {});
  return actx;
}
function useAnalyser(node) {
  analyser = actx.createAnalyser(); analyser.fftSize = 256; analyser.smoothingTimeConstant = 0.7;
  levelBuf = new Uint8Array(analyser.frequencyBinCount); node.connect(analyser); return analyser;
}

// ── 녹음 ──
async function startRec() {
  stopPlayer();
  try {
    stream = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true, noiseSuppression: true}});
  } catch (e) {
    setState("idle", "마이크를 쓸 수 없어요. 브라우저 주소창의 마이크 권한을 허용해 주세요."); return;
  }
  const types = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
  const mime = types.find((t) => window.MediaRecorder && MediaRecorder.isTypeSupported(t)) || "";
  try { recorder = new MediaRecorder(stream, mime ? {mimeType: mime} : undefined); }
  catch (e) { setState("idle", "이 브라우저는 녹음을 지원하지 않아요. 크롬에서 열어 주세요."); stopStream(); return; }
  chunks = [];
  recorder.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
  recorder.onstop = onRecStop;
  if (ensureCtx()) { fakeLevel = false; useAnalyser(actx.createMediaStreamSource(stream)); } else { fakeLevel = true; }
  recorder.start(250);
  recStart = performance.now();
  setState("recording");
  timerInt = setInterval(() => {
    const s = Math.floor((performance.now() - recStart) / 1000), el = $("tm");
    if (el) el.textContent = Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
    if (s >= 120) stopRec();                    // 너무 길면 2분에서 자동 종료
  }, 250);
}
function stopStream() { if (stream) stream.getTracks().forEach((t) => t.stop()); stream = null; }
function stopRec() {
  clearInterval(timerInt);
  if (recorder && recorder.state !== "inactive") recorder.stop();
}
function onRecStop() {
  stopStream(); analyser = null;
  const dur = performance.now() - recStart;
  const type = (recorder && recorder.mimeType) || "audio/webm";
  const blob = new Blob(chunks, {type: type});
  if (dur < 700 || blob.size < 1500) { setState("idle", "너무 짧아요. 로고를 누르고 말한 뒤 다시 눌러 주세요."); return; }
  const ext = type.includes("mp4") ? "mp4" : type.includes("ogg") ? "ogg" : "webm";
  const fr = new FileReader();
  fr.onload = () => {
    sentId = Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
    setValue({id: sentId, b64: String(fr.result).split(",")[1], ext: ext});
    setState("thinking");
    thinkTimer = setTimeout(() => { if (state === "thinking") setState("idle"); }, 120000);
  };
  fr.readAsDataURL(blob);
}

// ── 답변 음성 재생 (말하는 중 애니메이션) ──
function stopPlayer() { if (player) { player.pause(); player = null; } analyser = null; fakeLevel = false; }
async function speak(b64) {
  stopPlayer();
  const a = new Audio("data:audio/mpeg;base64," + b64);
  player = a;
  const ctx = ensureCtx();
  if (ctx) { try { await Promise.race([ctx.resume(), new Promise((r) => setTimeout(r, 300))]); } catch (e) {} }
  if (ctx && ctx.state === "running") {
    try { const src = ctx.createMediaElementSource(a); useAnalyser(src); analyser.connect(ctx.destination); fakeLevel = false; }
    catch (e) { fakeLevel = true; }
  } else { fakeLevel = true; }               // 소리 분석이 안 되면 부드러운 가짜 파동
  a.onended = () => { if (player === a) { stopPlayer(); setState("idle"); } };
  try { await a.play(); setState("speaking"); }
  catch (e) { player = null; pendingB64 = b64; setState("blocked"); }   // 자동 재생이 막히면 눌러서 듣기
}
let pendingB64 = "";

function played(id) { try { return sessionStorage.getItem("able_played_" + id) === "1"; } catch (e) { return false; } }
function markPlayed(id) { try { sessionStorage.setItem("able_played_" + id, "1"); } catch (e) {} }

$("btn").addEventListener("click", () => {
  if (state === "idle") startRec();
  else if (state === "recording") stopRec();
  else if (state === "speaking") { stopPlayer(); setState("idle"); }
  else if (state === "blocked") { const b = pendingB64; pendingB64 = ""; speak(b); }
});

// ── 파이썬에서 받은 값으로 상태 바꾸기 ──
window.addEventListener("message", (ev) => {
  const d = ev.data || {};
  if (d.type !== "streamlit:render") return;
  const args = d.args || {};
  const th = d.theme || {};
  if (th.textColor) document.documentElement.style.setProperty("--text", th.textColor);
  if (th.base === "dark") {
    document.documentElement.style.setProperty("--btn", "#1d2531");
    document.documentElement.style.setProperty("--sub", "rgba(250,250,250,.6)");
  }
  if (args.size && (args.size === "big" ? "big" : "compact") !== size) layout(args.size);
  // 내가 보낸 녹음 처리가 끝났는데 들려줄 답변이 없으면 대기로
  if (state === "thinking" && sentId && args.done_id === sentId && !(args.play_id && !played(args.play_id))) setState("idle");
  // 새 답변 음성이 오면 말하기
  if (args.play_id && args.play_b64 && args.play_id !== lastPlayId && !played(args.play_id) && state !== "recording") {
    lastPlayId = args.play_id; markPlayed(args.play_id); speak(args.play_b64);
  }
});

layout("compact");
requestAnimationFrame(draw);
toST("streamlit:componentReady", {apiVersion: 1});
window.addEventListener("resize", setHeight);
</script>
</body></html>
'''


def _declare_able_mic():
    page = ABLE_MIC_HTML.replace("__LOGO_B64__", ABLE_LOGO_B64)
    ver = hashlib.md5(page.encode("utf-8")).hexdigest()[:8]           # 내용이 바뀌면 새 폴더 → 브라우저 캐시 문제 없음
    folder = Path(tempfile.gettempdir()) / f"able_mic_{ver}"
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / "index.html"
    if not f.exists():
        f.write_text(page, encoding="utf-8")
    return components.declare_component("able_mic", path=str(folder))


_able_mic = _declare_able_mic()


def able_mic(size, key):
    # size: "big"(새 채팅 첫 화면) / "compact"(말로 이어서 요청하기). 녹음하면 {"id","b64","ext"}를 돌려줌
    play = st.session_state.get("mic_play")
    if play and time.time() - play["ts"] > 90:     # 오래된 답변 음성은 다시 보내지 않음
        st.session_state.pop("mic_play", None)
        play = None
    return _able_mic(size=size, play_id=play["id"] if play else "", play_b64=play["b64"] if play else "",
                     done_id=st.session_state.get("voice_done", ""), key=key, default=None)


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
            elif t["due_text"] and re.search(r"\d+\s*시|오전|오후", t["due_text"]):
                detail += f" / 마감: {t['due_text']}"
            item = add_todo(title, t["due_date"] if exact else "", t["priority"], detail.strip(" /"), source_label)
            undone_titles[title] = item["id"]
            t.update(status="calendar" if exact else ("approximate" if t["due_type"] == "approximate" else "nodue"),
                     todo_id=item["id"])
        out.append(t)
    return out


MEETING_WORDS = re.compile(r"회의|미팅|상담|면담|방문|워크숍|세미나|간담회|참석")
WORK_WORDS = re.compile(r"자료|보고서|문서|작성|준비|정리|초안|조사|분석|제출|검토|확인|공유|전달")


def split_meeting_tasks(tasks):
    # AI가 '회의 참석'을 김 대리 업무로 넣은 경우 → 할 일이 아니라 일정으로 옮김
    work, meetings = [], []
    for t in tasks:
        if MEETING_WORDS.search(t["title"]) and not WORK_WORDS.search(t["title"]):
            meetings.append({"title": re.sub(r"\s*참석$", "", t["title"]).strip(), "kind": "회의·미팅",
                             "when_text": t["due_text"], "date_type": t["due_type"], "date": t["due_date"],
                             "end_date": "", "start": "", "end": "", "place": "", "kim_attends": True,
                             "evidence": t["evidence"]})
        else:
            work.append(t)
    return work, meetings


def same_event(e, title, date, start):
    # 이미 캘린더에 있는 같은 일정인지 (제목이 비슷하고 날짜가 같으면, 시간이 있으면 시간도 같으면)
    if e["date"] != date or (start and e["start"] and e["start"] != start):
        return False
    a, b = _squash(e["title"]), _squash(title)
    return a == b or (min(len(a), len(b)) >= 3 and (a in b or b in a))


def apply_meeting_schedules(schedules, source_text, note_title):
    # 회의에서 언급된 일정을 캘린더에 반영
    #   · 회의·미팅(김 대리 참석)과 회사 행사 → 캘린더 일정으로 자동 등록 (기간 행사는 시작일·종료일)
    #   · 다른 사람 마감, 날짜가 정확하지 않은 일정, 근거가 원문에 없는 일정 → 회의록에서 참고로만 보여 줌
    #   · 기존 일정과 겹쳐도 회의 내용 그대로 등록하고, 겹친다는 표시만 남김 (임의로 옮기거나 지우지 않음)
    out = []
    for s in schedules:
        s = dict(s, verified=found_in(s["evidence"], source_text), event_ids=[], conflicts=[])
        d = parse_date(s["date"]) if s["date_type"] == "exact" else None
        wanted = (s["kind"] == "회의·미팅" and s["kim_attends"]) or s["kind"] == "회사 행사"
        if not s["verified"]:
            s["cal_status"] = "unverified"
        elif not d or not wanted:
            s["cal_status"] = "reference"
        else:
            days = [(d.isoformat(), s["title"], s["start"], s["end"])]
            end_d = parse_date(s["end_date"])
            if s["kind"] == "회사 행사" and end_d and end_d > d:
                days = [(d.isoformat(), f"{s['title']} 시작 (~{fmt_date(end_d)})", "", ""),
                        (end_d.isoformat(), f"{s['title']} 종료", "", "")]
            status = "calendar"
            for date_iso, title, start, end in days:
                dup = next((e for e in data["events"] if same_event(e, title, date_iso, start)), None)
                if dup:
                    s["event_ids"].append(dup["id"])
                    status = "duplicate" if status == "calendar" and len(days) == 1 else status
                    continue
                note = f"[회의: {note_title}]" + (f" 장소: {s['place']}" if s["place"] else "")
                f = build_event(title, date_iso, start, end, "", note)
                s["conflicts"] += [conflict_text(c) for c in find_conflicts(f["date"], f["start"], f["end"])]
                s["event_ids"].append(commit_event(f, "add", None, "회의 분석")["id"])
            s["cal_status"] = "conflict" if s["conflicts"] else status
        out.append(s)
    return out


def schedule_status_line(s):
    return {
        "calendar": "📅 캘린더에 일정으로 추가했어요",
        "conflict": "⚠️ 캘린더에 추가했어요 · 겹치는 일정: " + ", ".join(s.get("conflicts", [])),
        "duplicate": "↔️ 이미 캘린더에 있는 일정이에요",
        "reference": "참고용 · 캘린더에는 넣지 않았어요",
        "unverified": "⚠️ 원문에서 근거 문장을 찾지 못해 캘린더에 넣지 않았어요",
    }.get(s.get("cal_status"), "")


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
                "당신은 '김 대리'의 개인 비서 '에이블'입니다. 김 대리가 메일을 보내기 전에 마지막으로 한 번 점검합니다.",
                f"오늘 날짜: {today.isoformat()} ({WEEKDAYS[today.weekday()]}요일)",
                date_table(),
                "아래 다섯 항목을 모두 점검해 항목마다 ok(true/false)와 짧은 comment를 씁니다.",
                "실제로 잘못 보낼 위험이 있을 때만 false로 하고, 사소하거나 애매하면 ok=true로 둡니다.",
                "- 받는 사람: [요청/상황]에서 의도한 사람과 [받는 사람] 정보가 맞는지. 요청에 받는 사람 언급이 없으면 ok.",
                "- 내용 정확성: 본문의 날짜·숫자·약속이 [요청/상황]·[이전 대화]·[참고 자료]와 '어긋날' 때만 false.",
                "  '내일', '금요일', '다음 주 화요일'처럼 요청한 표현을 [날짜표]에 맞는 실제 날짜(예: 11월 7일)로 바꿔 쓴 것은 정상(ok)입니다.",
                "  요일과 날짜가 날짜표와 맞지 않거나, 요청과 다른 날짜·금액이 들어간 경우만 문제입니다.",
                "  메일에 흔히 들어가는 인사·마무리 문장, 요청 내용을 정리한 표현은 지어낸 내용이 아닙니다.",
                "- 빠진 내용: 요청한 내용이 빠졌는지, '첨부'를 언급했는데 실제 첨부가 없는지(이 메일은 첨부를 보낼 수 없음).",
                "- 말투·맞춤법: 받는 사람에게 맞는 정중한 말투인지, 오탈자가 있는지.",
                "- 민감 정보: 비밀번호·주민번호·계좌번호 같은 민감 정보가 들어 있는지.",
                "비교할 자료가 없어 판단할 수 없으면 추측하지 말고 ok=true, comment에 '비교할 자료 없음'이라고 씁니다.",
                "summary: 김 대리에게 하는 한 문장 결론. 문제가 없으면 '보내도 괜찮아 보여요.',",
                "  문제가 있으면 무엇이 왜 문제인지 구체적으로 (예: '요청은 금요일인데 본문에 목요일(11/6)로 적혀 있어요.').",
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
ABLE_TEAL, ABLE_TEAL_DARK = "#0b9ea4", "#087e83"
_LOGO_MASK = f"url('data:image/png;base64,{ABLE_LOGO_B64}') center/contain no-repeat"
APP_CSS = [
    # 브랜드 색 (Streamlit 기본 빨강 → 에이블 청록)
    f":root {{--able:{ABLE_TEAL}; --able-dark:{ABLE_TEAL_DARK}; --able-soft:rgba(11,158,164,.10);}}",
    "button[kind='primary'], [data-testid='stBaseButton-primary'] {background:var(--able) !important;"
    " border-color:var(--able) !important; color:#fff !important;}",
    "button[kind='primary']:hover, [data-testid='stBaseButton-primary']:hover {background:var(--able-dark) !important;"
    " border-color:var(--able-dark) !important;}",
    "button[kind='secondary']:hover, [data-testid='stBaseButton-secondary']:hover {border-color:var(--able) !important;"
    " color:var(--able-dark) !important;}",
    "button:focus-visible {box-shadow:0 0 0 3px rgba(11,158,164,.35) !important;}",
    "[data-baseweb='tab-highlight'] {background-color:var(--able) !important;}",
    "[data-baseweb='tab'][aria-selected='true'] p {color:var(--able-dark) !important; font-weight:600;}",
    "[data-baseweb='tab']:hover p {color:var(--able-dark) !important;}",
    "label[data-baseweb='radio']:has(input:checked) > div:first-child {background-color:var(--able) !important;"
    " border-color:var(--able) !important;}",
    "label[data-baseweb='checkbox']:has(input:checked) > span {background-color:var(--able) !important;"
    " border-color:var(--able) !important;}",
    "label[data-baseweb='checkbox']:has(input[role='switch']:checked) > div {background-color:var(--able) !important;}",
    # (새 버전 Streamlit 구조) 라디오·체크박스·토글
    "[data-testid='stRadioOption'][data-selected='true'] > div > div:first-child {background-color:var(--able) !important;"
    " border-color:var(--able) !important;}",
    "label[data-selected='true']:not([data-testid='stRadioOption']) > span + div {background-color:var(--able) !important;"
    " border-color:var(--able) !important;}",
    "input:focus, textarea:focus, [data-baseweb='select'] > div:focus-within {border-color:var(--able) !important;}",
    "a {color:var(--able-dark);}",
    # 로고 마크 (로고 색을 그대로 씀)
    f".able-mark {{display:inline-block; width:28px; height:28px; flex:0 0 auto; background:var(--able);"
    f" -webkit-mask:{_LOGO_MASK}; mask:{_LOGO_MASK};}}",
    ".able-mark.big {width:40px; height:40px;}",
    ".able-side-brand {display:flex; align-items:center; gap:10px; margin:0 0 12px;}",
    ".able-side-brand b {font-size:1.25rem;}",
    # 헤더: 한 줄 (로고 · 제목/날짜 · 오늘 요약 칩)
    ".able-hero {display:flex; align-items:center; gap:14px; flex-wrap:wrap; padding:12px 18px; border-radius:14px;"
    " margin-bottom:6px; background:var(--able-soft); border:1px solid rgba(11,158,164,.22);}",
    ".able-hero-text {flex:1 1 220px; min-width:0;}",
    ".able-chips {display:flex; flex-wrap:wrap; gap:6px;}",
    ".able-title {font-size:1.3rem; font-weight:700; line-height:1.25;}",
    ".able-sub {opacity:.72; font-size:.85rem; margin-top:1px;}",
    ".able-chip {display:inline-block; font-size:.78rem; padding:3px 10px; border-radius:999px;"
    " background:rgba(128,128,128,.10); border:1px solid rgba(11,158,164,.25); white-space:nowrap;}",
    ".able-chip.warn {background:rgba(229,72,77,.14); border-color:rgba(229,72,77,.35);}",
    # 대시보드 오늘 요약 카드
    ".able-cards {display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:10px; margin:4px 0 12px;}",
    "@media (max-width:900px) {.able-cards {grid-template-columns:repeat(2, minmax(0,1fr));}}",
    ".able-card {border:1px solid rgba(128,128,128,.22); border-left:4px solid var(--c, var(--able)); border-radius:10px;"
    " padding:10px 12px; min-width:0; background:rgba(128,128,128,.04);}",
    ".able-card .k {font-size:.76rem; opacity:.7; letter-spacing:.02em;}",
    ".able-card .v {font-size:1.05rem; font-weight:700; margin-top:2px; overflow:hidden; text-overflow:ellipsis;"
    " white-space:nowrap;}",
    ".able-card .m {font-size:.8rem; opacity:.8; margin-top:1px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;}",
    ".able-card .big {font-size:1.6rem; font-weight:700; line-height:1.15; font-variant-numeric:tabular-nums;}",
    ".able-badge {display:inline-block; padding:1px 8px; border-radius:999px; font-size:.72rem;"
    " font-weight:600; margin-right:4px; vertical-align:1px;}",
    ".able-meta {opacity:.75; font-size:.83rem;}",
    ".able-warn {color:#e5484d; font-size:.8rem;}",
    ".able-cal .pill {display:block; font-size:.7rem; line-height:1.3; margin-top:3px; padding:2px 4px;"
    " border-radius:4px;}",
    ".able-cal .pill .tm {display:block; font-size:.64rem; opacity:.75; font-variant-numeric:tabular-nums;}",
    ".able-cal .pill .t {display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden;"
    " word-break:keep-all; overflow-wrap:anywhere;}",
    ".able-cal .pill.done {text-decoration:line-through; opacity:.45;}",
    ".able-cal .more {font-size:.68rem; opacity:.6; margin-top:2px;}",
    ".legend {margin-top:4px;} .legend span {font-size:.74rem; margin-right:10px; white-space:nowrap;}",
    ".cal-head {text-align:center; font-size:.8rem; font-weight:600; opacity:.8; padding-bottom:2px;}",
    ".cal-cell {min-height:70px; overflow:hidden; margin-top:-12px; padding-bottom:4px;"
    " border-bottom:1px solid rgba(128,128,128,.14);}",
    ".cal-cell.other {opacity:.35;}",
    ".st-key-able_cal button {padding:0 4px; min-height:30px; justify-content:flex-start;}",
    ".st-key-able_cal button[kind='secondary'], .st-key-able_cal [data-testid='stBaseButton-secondary'] {"
    " background:transparent !important; border-color:transparent !important; box-shadow:none !important;}",
    ".st-key-able_cal button[kind='secondary']:hover, .st-key-able_cal [data-testid='stBaseButton-secondary']:hover {"
    " background:var(--able-soft) !important;}",
    ".st-key-able_cal button p {font-weight:600; font-variant-numeric:tabular-nums;}",
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
    # 시간(있으면)은 작은 글씨 한 줄, 제목은 두 줄까지 보여 줌 · 마우스를 올리면 전체 제목
    tm, rest = "", label
    m = re.match(r"^(\d{1,2}:\d{2})\s+(.*)$", label)
    if m:
        tm, rest = m.group(1), m.group(2)
    full = html.escape(label)
    line = "solid" if kind == "event" else "dashed"            # 할 일 마감은 점선
    return (f'<span class="pill {kind}" title="{full}" '
            f'style="border-left:3px {line} {color}; background:{color}24">'
            + (f'<span class="tm">{tm}</span>' if tm else "") + f'<span class="t">{html.escape(rest)}</span></span>')


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


def dashboard_cards_html(undone, overdue, due_this_week, today_events):
    # 대시보드 맨 위 '오늘 한눈에' 카드: 다음 일정 · 가장 급한 마감 · 남은 할 일 · 오늘 일정/충돌
    now_hm = dt.datetime.now(KST).strftime("%H:%M")
    upcoming = sorted([e for e in data["events"] if (d := parse_date(e["date"]))
                       and (d > today or (d == today and (e["end"] or e["start"] or "23:59") >= now_hm))],
                      key=lambda e: (e["date"], e["start"] or "00:00"))
    dues = sorted([t for t in undone if parse_date(t["due"])], key=lambda t: (t["due"], PRIORITY_ORDER[t["priority"]]))
    esc = html.escape

    def card(key, value, meta, color, big=False):
        v = f'<div class="big">{value}</div>' if big else f'<div class="v" title="{esc(value)}">{esc(value)}</div>'
        return (f'<div class="able-card" style="--c:{color}"><div class="k">{key}</div>{v}'
                f'<div class="m" title="{esc(meta)}">{esc(meta)}</div></div>')

    if upcoming:
        e = upcoming[0]
        d = parse_date(e["date"])
        when = ("오늘" if d == today else "내일" if (d - today).days == 1 else fmt_date(d)) + " " + event_time_text(e)
        c1 = card("다음 일정", e["title"], when, ev_color(e))
    else:
        c1 = card("다음 일정", "예정된 일정 없음", "캘린더에서 날짜를 눌러 추가해요", "#94a3b8")
    if dues:
        t = dues[0]
        d = parse_date(t["due"])
        color = "#e5484d" if d <= today + dt.timedelta(days=1) else "#d97706" if (d - today).days <= 3 else "#2563eb"
        c2 = card("가장 급한 마감", t["title"], f"{d_day(d)} · {fmt_date(d)} · {t['priority']}", color)
    else:
        c2 = card("가장 급한 마감", "마감 있는 할 일 없음", f"기한 없는 할 일 {len(undone)}건", "#94a3b8")
    c3 = card("남은 할 일", f"{len(undone)}건",
              (f"기한 지남 {len(overdue)}건 · " if overdue else "") + f"이번 주 마감 {len(due_this_week)}건",
              "#e5484d" if overdue else ABLE_TEAL, big=True)
    ov = count_overlaps()
    c4 = card("오늘 일정", f"{len(today_events)}건", f"⚠️ 겹치는 일정 {ov}건" if ov else "겹치는 일정 없음",
              "#e5484d" if ov else ABLE_TEAL, big=True)
    return f'<div class="able-cards">{c1}{c2}{c3}{c4}</div>'


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
DELETE_STOP = {"일정", "스케줄", "거", "것", "그거", "약속잡은거", "할일", "투두", "todo", "업무", "목록"}
KIND_NAME = {"events": "일정", "todos": "할 일"}


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


def todo_due_text(t):
    d = parse_date(t["due"])
    return (f"마감 {fmt_date(d)}" if d else "기한 없음") + (" · 완료" if t["done"] else "")


def find_todo_delete_candidates(keyword, status):
    # status: 미완료 / 완료 / 전체. 키워드가 제목·세부 내용·분류에 가장 많이 들어간 할 일만 후보로 (최대 5개)
    words = [_nospace(w) for w in re.split(r"[\s,]+", keyword or "") if w and _nospace(w) not in DELETE_STOP]
    pool = [t for t in data["todos"]
            if status == "전체" or (status == "완료") == bool(t["done"])]
    if not words and status != "완료":          # 키워드 없이 '전부'는 완료한 할 일 정리일 때만 허용
        return [], len(pool)
    scored = []
    for t in pool:
        text = _nospace(t["title"] + " " + t.get("detail", ""))      # 분류 이름(예: 조사·분석)은 비교하지 않음
        score = sum(w in text for w in words)
        if score or not words:
            scored.append((-score, PRIORITY_ORDER.get(t["priority"], 1), t["due"] or "9999", t))
    scored.sort(key=lambda x: x[:3])
    if scored and words:
        scored = [x for x in scored if x[0] == scored[0][0]]
    return [x[3] for x in scored[:DELETE_MAX]], len(scored)


def request_delete_todo(keyword, status):
    # 바로 지우지 않고 삭제할 할 일 후보를 승인 대기(pending_delete)로 저장
    status = status if status in ("미완료", "완료", "전체") else "미완료"
    cands, total = find_todo_delete_candidates(keyword, status)
    if not cands:
        pool = [t for t in data["todos"] if status == "전체" or (status == "완료") == bool(t["done"])]
        return {"status": "failed",
                "error": (f"'{keyword}'에 해당하는 할 일을 찾지 못했습니다." if keyword else
                          "어떤 할 일을 지울지 알 수 없습니다. 할 일 이름을 알려 달라고 하세요.")
                         + " 아무것도 삭제하지 않았습니다.",
                "todos": [f"{t['title']} ({todo_due_text(t)})" for t in pool[:15]]}
    st.session_state.pending_delete = {"kind": "todos", "ids": [t["id"] for t in cands],
                                       "query": keyword or f"{status} 할 일"}
    st.session_state.pending_toast = "⚠️ 삭제할 할 일을 화면 위 카드에서 확인해 주세요. 승인하기 전에는 지우지 않아요."
    out = {"status": "needs_approval", "kind": "delete_todo",
           "candidates": [{"no": i + 1, "title": t["title"], "category": t["category"], "when": todo_due_text(t)}
                          for i, t in enumerate(cands)],
           "message": "아직 삭제하지 않았습니다. 삭제 대상을 보여 주고 김 대리의 승인(번호/전부/취소)을 기다립니다."}
    if total > len(cands):
        out["note"] = f"조건에 맞는 할 일이 {total}건이라 앞의 {len(cands)}건만 보여 줍니다."
    return out


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
    st.session_state.pending_delete = {"kind": "events", "ids": [e["id"] for e in cands], "query": keyword or date_iso}
    st.session_state.pending_toast = "⚠️ 삭제할 일정을 화면 위 카드에서 확인해 주세요. 승인하기 전에는 지우지 않아요."
    return {"status": "needs_approval", "kind": "delete",
            "candidates": [{"no": i + 1, "title": e["title"], "category": e["category"],
                            "when": f"{fmt_date(parse_date(e['date']))} {event_time_text(e)}"}
                           for i, e in enumerate(cands)],
            "message": "아직 삭제하지 않았습니다. 삭제 대상을 보여 주고 김 대리의 승인(번호/전부/취소)을 기다립니다."}


def pending_delete_kind():
    p = st.session_state.get("pending_delete")
    return p.get("kind", "events") if p else "events"


def pending_delete_events():
    # 삭제 승인 대기 중인 항목 (일정 또는 할 일)
    p = st.session_state.get("pending_delete")
    if not p:
        return []
    by_id = {x["id"]: x for x in data[pending_delete_kind()]}
    return [by_id[i] for i in p["ids"] if i in by_id]


def delete_item_text(kind, x):
    if kind == "todos":
        return f"{x['title']} ({todo_due_text(x)})"
    return f"{x['title']} ({fmt_date(parse_date(x['date']))} {event_time_text(x)})"


def decide_pending_delete(choice):
    # 김 대리 승인: 번호(1~5) / all(후보 전부) / cancel(삭제하지 않음)
    evs = pending_delete_events()
    kind = pending_delete_kind()
    if not st.session_state.get("pending_delete"):
        return {"status": "failed", "error": "삭제 승인을 기다리는 일정·할 일이 없습니다."}
    if choice == "cancel" or not evs:
        st.session_state.pop("pending_delete", None)
        return {"status": "success", "decision": "취소", "deleted": [], "kind": KIND_NAME[kind]}
    if choice == "all":
        targets = evs
    elif choice.isdigit() and 1 <= int(choice) <= len(evs):
        targets = [evs[int(choice) - 1]]
    else:
        return {"status": "failed", "error": f"{choice}번 {KIND_NAME[kind]}이 없습니다. 1~{len(evs)}, all, cancel 중에서 골라 주세요."}
    for e in targets:
        delete_item(kind, e["id"])
    st.session_state.pop("pending_delete", None)
    return {"status": "success", "decision": "삭제", "kind": KIND_NAME[kind],
            "deleted": [delete_item_text(kind, e) for e in targets]}


def _delete_cb(choice):
    r = decide_pending_delete(choice)
    if r["status"] != "success":
        st.session_state.pending_toast = f"⚠️ {r['error']}"
    elif r["decision"] == "취소":
        st.session_state.pending_toast = f"{r['kind']} 삭제를 취소했어요. 그대로 있어요."
    else:
        st.session_state.pending_toast = f"{r['kind']}을 삭제했어요: " + ", ".join(r["deleted"])


def render_pending_delete():
    if not st.session_state.get("pending_delete"):
        return
    evs = pending_delete_events()
    if not evs:                                  # 그 사이 다른 곳에서 지워졌으면 카드 닫기
        st.session_state.pop("pending_delete", None)
        return
    kind = pending_delete_kind()
    what = KIND_NAME[kind]
    with st.container(border=True):
        st.error(f"🗑️ {what} 삭제 확인 — '{st.session_state.pending_delete['query']}'에 해당하는 {what} {len(evs)}건")
        st.caption(f"승인하기 전에는 삭제하지 않아요. 지울 {what}을 골라 주세요. 에이블 비서에게 '1번 지워줘', '취소'라고 말해도 돼요.")
        for i, e in enumerate(evs):
            a, b = st.columns([5, 1.3])
            a.markdown(f"**{i + 1}.** " + (todo_html(e) if kind == "todos" else event_html(e)), unsafe_allow_html=True)
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
    from datetime import datetime, date, time

    # 날짜·시간 비교용 변환 함수
    def to_date(value):
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value

        return date.fromisoformat(str(value).strip()[:10])

    def to_time(value):
        if isinstance(value, datetime):
            return value.time()
        if isinstance(value, time):
            return value

        # "16:10", "16:10:00" 형식 지원
        return time.fromisoformat(str(value).strip())

    p = st.session_state.get("pending_event")
    if not p:
        return

    f = p["fields"]
    event_id = p.get("event_id")
    what = "수정" if p.get("mode") == "edit" else "추가"

    conflicts = find_conflicts(
        f["date"],
        f["start"],
        f["end"],
        event_id,
    )

    original_date = to_date(f["date"])
    original_start = datetime.combine(
        original_date,
        to_time(f["start"]),
    )

    suggestions = []

    for original_index, slot in enumerate(p.get("suggestions") or []):
        slot_date = to_date(slot["date"])
        slot_start = datetime.combine(
            slot_date,
            to_time(slot["start"]),
        )

        # 기존 충돌 검사 함수의 입력 방식 유지
        available = is_free(
            slot["date"],
            parse_time(slot["start"]),
            parse_time(slot["end"]),
            event_id,
        )

        suggestions.append({
            "slot": slot,
            # 화면 정렬 후에도 원래 추천 번호를 콜백에 전달
            "original_index": original_index,
            "available": available,
            "same_day": slot_date == original_date,
            "difference_minutes": int(
                (slot_start - original_start).total_seconds() / 60
            ),
        })

    # 선택 가능 → 같은 날 → 원래 시작 시간과 가까운 순
    suggestions.sort(
        key=lambda item: (
            not item["available"],
            not item["same_day"],
            abs(item["difference_minutes"]),
            item["original_index"],
        )
    )

    with st.container(border=True):
        event_description = (
            f"{what}하려는 '{f['title']}' "
            f"({fmt_date(original_date)} "
            f"{event_time_text(f)}, {f['category']})"
        )

        if conflicts:
            st.warning(
                f"⚠️ 일정 충돌 감지 — {event_description}"
            )

            st.markdown("**충돌하는 기존 일정**")

            conflict_lines = []

            for conflict in conflicts:
                conflict_lines.append(
                    f"{badge(conflict['category'], ev_color(conflict))}"
                    f"{html.escape(str(conflict['title']))} · "
                    f"{html.escape(str(event_time_text(conflict)))}"
                    f" — <b>"
                    f"{html.escape(str(conflict.get('reason', '일정 충돌')))}"
                    f"</b>"
                )

            st.markdown(
                "<br>".join(conflict_lines),
                unsafe_allow_html=True,
            )

            st.caption(
                "기존 일정과 겹치거나 앞뒤 간격이 10분 미만이에요. "
                "대체 시간을 선택하거나 원래 시간으로 등록할 수 있어요."
            )

            st.markdown("**대체 시간 추천**")

            with st.expander("추천 기준 보기", expanded=False):
                st.write(
                    "업무 시간 09:00–18:00 · "
                    "점심시간 12:00–13:00 제외 · "
                    "일정 앞뒤 10분 여유"
                )

                st.caption(
                    "선택 가능한 시간을 우선하고, 같은 날의 시간부터 "
                    "원래 시작 시간과 가까운 순서로 표시해요."
                )

            if suggestions:
                available_count = sum(
                    bool(item["available"])
                    for item in suggestions
                )

                if available_count == 0:
                    st.info(
                        "기존 추천 시간에도 충돌이 생겼어요. "
                        "취소 후 날짜나 시간을 바꿔 다시 시도해 주세요."
                    )

                # 한 줄에 최대 3개의 추천 표시
                for row_start in range(0, len(suggestions), 3):
                    row = suggestions[row_start:row_start + 3]
                    cols = st.columns(len(row))

                    for offset, item in enumerate(row):
                        display_index = row_start + offset
                        slot = item["slot"]
                        available = item["available"]

                        with cols[offset]:
                            with st.container(border=True):
                                if available and display_index == 0:
                                    st.markdown("**⭐ 우선 추천**")
                                else:
                                    st.markdown(
                                        f"**대체 시간 {display_index + 1}**"
                                    )

                                st.text(slot_text(slot))

                                if available:
                                    if item["same_day"]:
                                        delta = item["difference_minutes"]

                                        if delta == 0:
                                            reason = (
                                                "원래 시작 시간과 같아요."
                                            )
                                        else:
                                            direction = (
                                                "늦춘"
                                                if delta > 0
                                                else "앞당긴"
                                            )

                                            hours, minutes = divmod(
                                                abs(delta), 60
                                            )

                                            duration_parts = []

                                            if hours:
                                                duration_parts.append(
                                                    f"{hours}시간"
                                                )

                                            if minutes:
                                                duration_parts.append(
                                                    f"{minutes}분"
                                                )

                                            duration_text = " ".join(
                                                duration_parts
                                            )

                                            reason = (
                                                "원래 일정과 같은 날이에요. "
                                                f"시작을 {duration_text} "
                                                f"{direction} 시간이에요."
                                            )
                                    else:
                                        reason = (
                                            "다른 날짜에 확보할 수 있는 "
                                            "대체 시간이에요."
                                        )

                                    st.caption(reason)
                                    st.caption("✅ 현재 충돌 없음")

                                else:
                                    st.caption(
                                        "⛔ 다른 일정과 충돌해 "
                                        "선택할 수 없어요."
                                    )

                                st.button(
                                    "이 시간으로 등록",
                                    key=(
                                        f"pe_s{item['original_index']}"
                                    ),
                                    on_click=_decide_cb,
                                    args=(
                                        str(
                                            item["original_index"] + 1
                                        ),
                                    ),
                                    use_container_width=True,
                                    disabled=not available,
                                    type=(
                                        "primary"
                                        if available and display_index == 0
                                        else "secondary"
                                    ),
                                )

            else:
                st.info(
                    "2주 안에서 조건에 맞는 업무 시간을 "
                    "찾지 못했어요. "
                    "취소 후 날짜나 시간을 바꿔 다시 시도해 주세요."
                )

        else:
            st.success("✅ 일정 충돌이 해소됐어요.")
            st.write(event_description)
            st.caption("원래 시간으로 등록할 수 있어요.")

        st.divider()

        a, b = st.columns([2, 1])

        a.button(
            (
                "충돌을 감수하고 원래 시간으로 등록"
                if conflicts
                else "원래 시간으로 등록"
            ),
            key="pe_orig",
            on_click=_decide_cb,
            args=("original",),
            use_container_width=True,
            type="secondary" if conflicts else "primary",
        )

        b.button(
            "취소",
            key="pe_cancel",
            on_click=_decide_cb,
            args=("cancel",),
            use_container_width=True,
        )


# ─────────────────────────────────────────────
# 퇴근 10분 전 내일 브리핑
# 기존 "# 화면" 바로 위에 이 블록 전체 추가
# ─────────────────────────────────────────────

def render_leave_briefing_settings():
    # 사이드바 ⚙️ 설정: 퇴근 브리핑 켜기·시간·미리보기·초기화 + 테스트 상태 (본문 첫 화면을 가리지 않게)
    import datetime as briefing_dt
    from zoneinfo import ZoneInfo

    settings = st.session_state.data.setdefault("settings", {})
    seoul = ZoneInfo("Asia/Seoul")
    now = briefing_dt.datetime.now(seoul)
    current_iso = now.date().isoformat()
    settings.setdefault("leave_briefing_enabled", True)
    settings.setdefault("leave_briefing_time", "18:00")
    settings.setdefault("leave_briefing_ack_date", "")
    try:
        saved_time = briefing_dt.time.fromisoformat(settings["leave_briefing_time"])
    except (TypeError, ValueError):
        saved_time = briefing_dt.time(18, 0)
    st.session_state.setdefault("lb_enabled_widget", settings["leave_briefing_enabled"])
    st.session_state.setdefault("lb_time_widget", saved_time)

    with st.expander("⚙️ 설정 · 퇴근 브리핑", expanded=False):
        enabled = st.checkbox("퇴근 10분 전 내일 브리핑 받기", key="lb_enabled_widget")
        leave_time = st.time_input("퇴근 시간 · 24시간 기준", key="lb_time_widget", step=60)
        st.caption("오후 6시는 18:00이에요. 앱이 열려 있는 동안 30초마다 확인해요.")
        left, right = st.columns(2)
        preview_clicked = left.button("미리보기", key="lb_preview_button", use_container_width=True)
        reset_clicked = right.button("초기화", key="lb_reset_button", use_container_width=True,
                                     help="오늘 알림을 다시 받을 수 있게 해요")
        show_test = st.toggle("🛠️ 테스트 상태 보기", key="lb_test_toggle")

        leave_text = leave_time.strftime("%H:%M")
        if (settings["leave_briefing_enabled"] != enabled or settings["leave_briefing_time"] != leave_text
                or reset_clicked):
            settings["leave_briefing_enabled"] = enabled
            settings["leave_briefing_time"] = leave_text
            settings["leave_briefing_ack_date"] = ""
            st.session_state.pop("lb_toast_date", None)
            save_data()
        if preview_clicked:
            st.session_state["lb_preview_date"] = current_iso

        if show_test:
            leave_at = briefing_dt.datetime.combine(now.date(), leave_time, tzinfo=seoul)
            notification_at = leave_at - briefing_dt.timedelta(minutes=10)
            acknowledged = settings["leave_briefing_ack_date"] == current_iso
            auto_show = enabled and now >= notification_at and not acknowledged
            st.text(f"현재 한국 시간: {now:%Y-%m-%d %H:%M:%S}")
            st.text(f"설정한 퇴근 시간: {leave_at:%Y-%m-%d %H:%M}")
            st.text(f"알림 시작 시간: {notification_at:%Y-%m-%d %H:%M}")
            st.text(f"알림 켜짐: {'예' if enabled else '아니요'}")
            st.text(f"오늘 확인 완료: {'예' if acknowledged else '아니요'}")
            st.text(f"자동 표시 조건 충족: {'예' if auto_show else '아니요'}")
            if not enabled:
                st.info("브리핑 받기를 켜주세요.")
            elif acknowledged:
                st.info("오늘 브리핑을 확인했어요. 다시 테스트하려면 '초기화'를 누르세요.")
            elif now < notification_at:
                minutes, seconds = divmod(int((notification_at - now).total_seconds()), 60)
                st.info(f"알림 시작까지 {minutes}분 {seconds}초 남았어요.")
            else:
                st.success("자동 브리핑 표시 조건이 충족됐어요.")


@st.fragment(run_every="30s")
def render_leave_briefing():
    import datetime as briefing_dt
    from zoneinfo import ZoneInfo

    app_data = st.session_state.data
    settings = app_data.setdefault("settings", {})
    seoul = ZoneInfo("Asia/Seoul")

    now = briefing_dt.datetime.now(seoul)
    current_date = now.date()
    next_date = current_date + briefing_dt.timedelta(days=1)

    current_iso = current_date.isoformat()
    next_iso = next_date.isoformat()
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]

    settings.setdefault("leave_briefing_enabled", True)
    settings.setdefault("leave_briefing_time", "18:00")
    settings.setdefault("leave_briefing_ack_date", "")

    try:
        saved_time = briefing_dt.time.fromisoformat(
            settings["leave_briefing_time"]
        )
    except (TypeError, ValueError):
        saved_time = briefing_dt.time(18, 0)

    st.session_state.setdefault(
        "lb_enabled_widget",
        settings["leave_briefing_enabled"],
    )
    st.session_state.setdefault("lb_time_widget", saved_time)

    # 설정 값은 사이드바 ⚙️ 설정(render_leave_briefing_settings)에서 바꾸고, 여기서는 읽기만 함
    enabled = bool(settings["leave_briefing_enabled"])
    leave_time = saved_time
    leave_text = leave_time.strftime("%H:%M")
    leave_at = briefing_dt.datetime.combine(current_date, leave_time, tzinfo=seoul)
    notification_at = leave_at - briefing_dt.timedelta(minutes=10)
    acknowledged = settings["leave_briefing_ack_date"] == current_iso
    auto_show = enabled and now >= notification_at and not acknowledged
    preview_show = st.session_state.get("lb_preview_date") == current_iso

    if not (auto_show or preview_show):
        return

    if (
        auto_show
        and st.session_state.get("lb_toast_date") != current_iso
    ):
        st.toast(
            "김 대리님, 퇴근 전 내일 일정을 확인하세요.",
            icon="🌙",
        )
        st.session_state["lb_toast_date"] = current_iso

    # 기존 일정 데이터 사용
    next_events = sorted(
        [
            event
            for event in app_data.get("events", [])
            if event.get("date") == next_iso
        ],
        key=lambda event: event.get("start") or "00:00",
    )

    # 기존 할 일 데이터 사용: 마감일 due, 완료 여부 done
    next_todos = sorted(
        [
            todo
            for todo in app_data.get("todos", [])
            if todo.get("due") == next_iso
            and not todo.get("done", False)
        ],
        key=lambda todo: (
            PRIORITY_ORDER.get(todo.get("priority", "보통"), 1),
            todo.get("title", ""),
        ),
    )

    # 기존 앱의 시간 겹침·간격 부족 판단 로직 사용
    conflict_pairs = []

    for index, first in enumerate(next_events):
        first_span = event_span(first)
        if first_span is None:
            continue

        for second in next_events[index + 1:]:
            second_span = event_span(second)
            if second_span is None:
                continue

            reason = conflict_reason(first_span, second_span)
            if reason:
                conflict_pairs.append((first, second, reason))

    next_label = (
        f"{next_date.month}/{next_date.day}"
        f"({weekdays[next_date.weekday()]})"
    )

    with st.container(border=True):
        st.subheader("🌙 김 대리님의 내일 브리핑")
        st.caption(
            f"내일 {next_label} · "
            f"퇴근 시간 {leave_text} · 한국 시간 기준"
        )

        c1, c2, c3 = st.columns(3)
        c1.metric("내일 일정", f"{len(next_events)}건")
        c2.metric("내일 마감", f"{len(next_todos)}건")
        c3.metric("일정 충돌·간격 부족", f"{len(conflict_pairs)}건")

        st.markdown("**📅 내일 일정**")

        if next_events:
            for event in next_events:
                st.text(
                    f"• {event_time_text(event)} | "
                    f"{event.get('title', '제목 없음')}"
                )

                category = event.get("category", "")
                if category:
                    st.caption(f"분류: {category}")

                note = event.get("note", "")
                if note:
                    with st.expander(
                        f"📝 {event.get('title', '일정')} 메모"
                    ):
                        st.text(note)
        else:
            st.caption("내일 등록된 일정이 없어요.")

        st.markdown("**⏰ 내일 마감인 미완료 업무**")

        if next_todos:
            for todo in next_todos:
                priority = todo.get("priority", "보통")

                st.text(
                    f"• [{priority}] "
                    f"{todo.get('title', '제목 없음')}"
                )

                detail = todo.get("detail", "")
                if detail:
                    with st.expander(
                        f"📋 {todo.get('title', '업무')} 상세"
                    ):
                        st.text(detail)

            st.caption(
                "할 일에는 마감 날짜만 저장되어 있어요. "
                "구체적인 마감 시간은 업무 상세 내용을 확인해 주세요."
            )
        else:
            st.caption("내일 마감인 미완료 업무가 없어요.")

        if conflict_pairs:
            st.warning(
                "내일 일정이 겹치거나 일정 사이 여유가 부족해요."
            )

            for first, second, reason in conflict_pairs:
                st.text(
                    f"• {first.get('title', '제목 없음')} "
                    f"({event_time_text(first)})"
                    f" ↔ {second.get('title', '제목 없음')} "
                    f"({event_time_text(second)})"
                    f" — {reason}"
                )

        if not next_events and not next_todos:
            st.success(
                "내일 등록된 일정과 마감 업무가 없어요. "
                "편안하게 하루를 마무리하세요."
            )

        st.divider()

        def acknowledge_briefing():
            ack_date = (
                briefing_dt.datetime.now(seoul).date().isoformat()
            )

            st.session_state.data["settings"][
                "leave_briefing_ack_date"
            ] = ack_date

            st.session_state.pop("lb_preview_date", None)
            save_data()

        st.button(
            "✅ 확인 완료 · 오늘 브리핑 닫기",
            key="lb_ack_button",
            on_click=acknowledge_briefing,
            use_container_width=True,
            type="primary",
        )


SIDE_MAIN = st.sidebar.container()          # 사이드바 위쪽: 새 채팅·대화 기록·요약
SIDE_SETTINGS = st.sidebar.container()      # 사이드바 맨 아래: ⚙️ 설정
with SIDE_SETTINGS:
    render_leave_briefing_settings()

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
    f'<div class="able-hero"><span class="able-mark big"></span>'
    f'<div class="able-hero-text"><div class="able-title">AI 개인비서 에이블</div>'
    f'<div class="able-sub">{today.month}월 {today.day}일 {WEEKDAYS[today.weekday()]}요일 · 김 대리님의 업무 비서</div></div>'
    f'<div class="able-chips">{_chip_html}</div></div>',
    unsafe_allow_html=True)

render_leave_briefing()      # 퇴근 10분 전(또는 미리보기)에만 헤더 아래에 내일 브리핑 카드가 나타남

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

    st.markdown(dashboard_cards_html(undone, overdue, due_this_week, today_events), unsafe_allow_html=True)

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
        "type": "function", "name": "delete_todo",
        "description": "할 일을 삭제해 달라고 할 때 사용합니다(완료 처리와 다름). 바로 지우지 않고 삭제 대상을 찾아 화면에 보여 준 뒤 "
                       "status=needs_approval과 candidates를 돌려줍니다. 김 대리가 승인해야 decide_pending_delete로 삭제됩니다.",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string",
                            "description": "할 일을 가리키는 말 그대로(예: '경쟁사 분석', '보고서'). "
                                           "'완료한 할 일 다 지워줘'처럼 이름 없이 정리할 때만 빈 문자열"},
                "status": {"type": "string", "enum": ["미완료", "완료", "전체"],
                           "description": "어떤 할 일에서 찾을지. 보통 미완료, '완료한 할 일 지워줘'면 완료, 모르면 전체"},
            },
            "required": ["keyword", "status"], "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function", "name": "decide_pending_delete",
        "description": "삭제 승인 대기 중인 일정·할 일을 김 대리의 대답대로 처리합니다. 김 대리가 승인한 뒤에만 사용합니다. "
                       "번호(1~5): 그 항목 삭제, all: 후보 전부 삭제, cancel: 삭제하지 않음.",
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
              "delete_event": "일정 삭제", "delete_todo": "할 일 삭제", "decide_pending_delete": "삭제 승인"}


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
    if name == "delete_todo":
        return request_delete_todo(args["keyword"].strip(), args["status"])
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
    if name in ("delete_event", "delete_todo"):
        return {"ok": True, "warn": True, "text": f"🗑️ {label} 승인 대기 → "
                + ", ".join(f"{c['no']}. {c['title']} ({c['when']})" for c in result["candidates"])}
    if name == "decide_pending_delete":
        if result["decision"] == "취소":
            return {"ok": True, "text": f"🗑️ {result['kind']} 삭제를 취소했어요 (그대로 있어요)"}
        return {"ok": True, "text": f"🗑️ {result['kind']} 삭제 완료 → " + ", ".join(result["deleted"])}
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
        "- 할 일을 지워 달라고 하면(예: '경쟁사 분석 할 일 지워줘', '완료한 할 일 다 지워줘') delete_todo를 사용합니다.",
        "  '다 했어/끝냈어'는 삭제가 아니라 complete_todo(완료 처리)입니다. 지워 달라고 할 때만 delete_todo를 씁니다.",
        "  승인 방법은 일정 삭제와 같습니다: 후보를 보여 주고 묻고, 다음 대답에서 decide_pending_delete를 사용합니다.",
        "  찾지 못했으면(failed) todos 목록을 알려 주고 어떤 할 일인지 묻습니다.",
        "- 이메일로 보내 달라는 명시적 요청이 있을 때만 send_email을 사용합니다.",
        "  받는 사람을 말하지 않으면 to에 '나'를 넣습니다. 연락처에 있는 사람은 이름으로 넣을 수 있고,",
        "  연락처에 없는 사람의 이메일 주소는 절대 지어내지 않습니다.",
        "- 받은 메일을 물으면(예: '최근 받은 메일 5개 요약해줘') get_recent_mails로 가져와,",
        "  메일마다 보낸 사람·제목과 본문에 있는 내용만으로 한두 문장씩 요약합니다. 본문에 없는 내용은 지어내지 않습니다.",
        "  calendar_proposal이 '캘린더 추가 여부를 묻는 중'이면 메일 비서 탭에서 추가할지 고를 수 있다고 알려 줍니다.",
        "- 메일을 보내 달라고 하면 메일을 쓰고 send_email까지 반드시 호출합니다. 보낼지 다시 묻지 않습니다.",
        "- send_email 결과가 success면 '메일을 보냈습니다'라고 받는 사람과 제목을 알려 줍니다.",
        "- needs_confirmation이면 아직 보내지 않은 것입니다. AI 확인에서 나온 확인할 점(issues)을 짧게 말하고",
        "  '그래도 보낼까요?'라고 한 번만 묻습니다. 같은 답변 안에서 send_email을 다시 부르지 않습니다.",
        "  김 대리가 '보내줘/그래도 보내'라고 하면 confirm_pending_email(send)로 바로 보냅니다(다시 확인하지 않음).",
        "  내용을 고쳐 달라고 하면 고친 내용으로 send_email을 부르면 확인 없이 바로 발송됩니다.",
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
        kind = pending_delete_kind()
        lines += [f"[삭제 승인 대기 중인 {KIND_NAME[kind]}] (아직 삭제하지 않음)"]
        lines += [f"{i + 1}) {delete_item_text(kind, e)}" for i, e in enumerate(pd_evs)] + [""]
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


def pending_mail_of(chat):
    # 이 대화에서 '보내기 전 확인'을 이미 마치고 김 대리 대답을 기다리는 메일
    for t in reversed(chat["turns"] if chat else []):
        if t.get("mail") and t["mail"]["status"] == "pending":
            return t["mail"], t
    return None, None


def prepare_email(args, question, via="text", chat=None, reviewed=None):
    # send_email 도구: 받는 사람 확인 → AI 점검(메일 한 통에 딱 한 번) → 발송
    #   · 음성 요청은 AI 확인에서 문제가 없으면 바로 발송, 글 요청은 설정에 따라 김 대리 확인 후 발송
    #   · 이미 한 번 확인한 메일(이번 답변 안에서 다시 호출 / 확인 대기 중에 '보내줘')은 다시 확인하지 않음
    rcpt, err = resolve_recipient(args["to"])
    if err:
        return {"status": "failed", "error": err}, None, {"ok": False, "text": f"📧 메일 준비 실패 ({err})"}
    who = f"{rcpt['name']} <{rcpt['email']}>" if rcpt["name"] else rcpt["email"]

    if reviewed is not None:
        # 같은 답변 안에서 send_email을 또 부른 경우: 다시 확인하지 않고, 김 대리 대답을 기다리게 함
        reviewed.update(to=rcpt["email"], name=rcpt["name"], subject=args["subject"], body=args["body"])
        return ({"status": "needs_confirmation", "already_reviewed": True, "to": rcpt["email"],
                 "message": "이 메일은 방금 확인을 마쳤습니다. 다시 확인하거나 send_email을 또 부르지 말고, "
                            "확인할 점을 한 번만 말한 뒤 '그래도 보낼까요?'라고 물어보세요."}, None, None)

    prior, prior_turn = pending_mail_of(chat)
    if prior is not None and prior["to"].lower() == rcpt["email"].lower():
        # 앞 답변에서 이미 확인을 마친 메일(같은 받는 사람): 김 대리가 보내라고 했으므로 다시 확인하지 않고 바로 발송
        sr = send_email(rcpt["email"], args["subject"], args["body"])
        if sr["status"] != "success":
            return sr, None, {"ok": False, "text": f"📧 메일 발송 실패 ({sr['error']})"}
        prior.update(status="sent", to=rcpt["email"], name=rcpt["name"], subject=args["subject"], body=args["body"])
        save_data()
        return ({"status": "success", "to": rcpt["email"], "subject": args["subject"],
                 "note": "발송 전 확인은 앞에서 한 번 마쳤습니다."}, None,
                {"ok": True, "text": f"📧 메일 발송 완료 → {who} · '{args['subject']}'"})

    history = ""
    if chat:
        history = "\n".join(f"김 대리: {t['q']}\n에이블: {t['a'][:300]}" for t in chat["turns"][-4:])
    ctx = (f"[김 대리 요청]\n{question}\n\n[이전 대화]\n{history or '(없음)'}\n\n[참고 자료]\n{brief_context()}")
    rv = review_email(rcpt, args["subject"], args["body"], ctx)
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
    sent_mail = None                     # 이번 답변에서 이미 보낸 메일 (같은 메일을 두 번 보내지 않게)
    asked_delete = False                 # 이번 답변에서 삭제 대상을 보여 줬으면 같은 답변에서는 삭제 금지
    for _ in range(5):  # 도구 호출은 최대 5회 반복
        resp = client.responses.create(model=MODEL, instructions=instructions, input=inputs, tools=TOOLS)
        calls = [i for i in resp.output if i.type == "function_call"]
        if not calls:
            return resp.output_text, actions, mail
        inputs += resp.output
        for c in calls:
            args = json.loads(c.arguments)
            if c.name == "send_email" and sent_mail:
                inputs.append({"type": "function_call_output", "call_id": c.call_id,
                               "output": json.dumps({"status": "success", "already_sent": True, **sent_mail},
                                                    ensure_ascii=False)})
                continue
            if c.name == "send_email":
                result, new_mail, act = prepare_email(args, question, via, chat, mail)
                mail = new_mail or mail
                if result.get("status") == "success":
                    sent_mail = {"to": result["to"], "subject": args["subject"]}
                if act is None:                  # 이미 확인한 메일을 다시 부른 경우: 화면에 확인 알림을 또 띄우지 않음
                    inputs.append({"type": "function_call_output", "call_id": c.call_id,
                                   "output": json.dumps(result, ensure_ascii=False)})
                    continue
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
                if c.name in ("delete_event", "delete_todo") and result.get("status") == "needs_approval":
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

    h1, h3 = st.columns([6, 1.3])
    h1.markdown(f"#### 💬 {chat['title'] if chat else '새 채팅'}")
    h1.caption(f"🧠 이 대화에서 기억 중인 질문·답변 {len(turns)}/{MAX_TURNS} · 새 채팅과 대화 기록은 왼쪽 사이드바에 있어요")
    h3.download_button("⬇️ 대화 저장", history_text(chat) if chat else "", file_name="에이블_대화기록.txt",
                       mime="text/plain", use_container_width=True, disabled=not turns)

    voice_val = None
    if not turns:
        # 새 채팅: 에이블 로고를 크게 → 누르면 녹음, 다시 누르면 전달
        with st.container(border=True):
            st.markdown("<h3 style='text-align:center; margin-bottom:0'>🎙️ 에이블에게 말로 요청해 보세요</h3>",
                        unsafe_allow_html=True)
            voice_val = able_mic("big", "able_mic_big")
            st.caption("예) 내일 오후 2시에 고객사 미팅 잡아줘 · 최근 회의 요약해서 박과장님께 메일 보내줘 · "
                       "마이크 권한을 물으면 '허용'을 눌러 주세요.")

    # --- 입력 카드 (맨 위: 말로 지시해도 최근 대화를 바로 볼 수 있게) ---
    with st.container(border=True):
        err = st.session_state.pop("chat_error", None)
        if err:
            st.error(f"답변을 만들지 못했습니다: {err}")
        if turns:
            st.markdown("**🎙️ 말로 이어서 요청하기**")
            voice_val = able_mic("compact", "able_mic_compact")
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

        b1, b3, b4 = st.columns([1.1, 1.6, 1.4])
        run_clicked = b1.button("▶ 실행", type="primary", use_container_width=True)
        tts_on = b3.toggle("🔊 답변 음성 재생", value=True, key="chat_tts")
        voice = b4.selectbox("목소리", list(VOICES), format_func=lambda v: f"{VOICES[v]} ({v})",
                             key="chat_voice", label_visibility="collapsed")

    quick = st.session_state.pop("chat_quick", None)
    # 로고 녹음기에서 온 새 녹음 (같은 녹음을 두 번 보내지 않게 id로 확인)
    voice_bytes, voice_name, voice_hash = None, "voice.webm", None
    if isinstance(voice_val, dict) and voice_val.get("b64") and voice_val.get("id") != st.session_state.get("voice_done"):
        try:
            voice_bytes = base64.b64decode(voice_val["b64"])
            voice_name = "voice." + str(voice_val.get("ext") or "webm")
            voice_hash = voice_val["id"]
        except Exception:
            voice_bytes = None
    new_voice = voice_bytes is not None
    if run_clicked or quick or new_voice:
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
                        stt = speech_to_text(voice_bytes, voice_name, stt_hint(), STT_CHAT_MODELS)  # 음성 → 텍스트 (STT)
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
                if reply_audio:                                                   # 로고 녹음기가 '말하는 중'으로 재생
                    st.session_state.mic_play = {"id": turn["id"], "ts": time.time(),
                                                 "b64": base64.b64encode(reply_audio).decode()}
                st.session_state.chat_n += 1                                      # 녹음기·입력칸 비우기
                done = True
            except Exception as e:
                st.session_state.chat_error = str(e)        # 새로고침 후에도 보이게 저장 (로고도 '생각 중'에서 풀림)
                done = True
            if done:
                st.rerun()   # try 밖에서 호출해야 화면 새로고침 신호가 막히지 않음

    # --- 보내기 전 확인 메일 (기다리는 메일이 있으면 대화 기록보다 먼저) ---
    for t in reversed(turns):
        if t.get("mail") and t["mail"]["status"] == "pending":
            render_mail_confirm(t)

    # --- 대화 기록: 최근 대화가 맨 위, 아래로 갈수록 이전 대화 ---
    def render_turn(t):
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
                st.audio(audio, format="audio/mp3")          # 처음 재생은 로고 녹음기가 '말하는 중'으로 해 줌

    if turns:
        latest, older = turns[-1], list(reversed(turns[:-1]))
        with st.container(border=True):
            st.markdown("**🆕 최근 대화**")
            render_turn(latest)
        if older:
            st.markdown(f"**🕘 이전 대화 ({len(older)}개)** · 최근 순")
            for t in older:
                render_turn(t)


# ─────────────────────────────────────────────
# 🎙️ 회의 분석 (실시간 녹음 · 음성 파일 · 텍스트 파일)
# ─────────────────────────────────────────────
SCHEDULE_KINDS = ["회의·미팅", "회사 행사", "다른 사람 마감", "기타"]
SCHEDULE_ITEM = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "일정 이름 (예: B사 제휴 미팅, 중간 점검 회의, 연말 프로모션)"},
        "kind": {"type": "string", "enum": SCHEDULE_KINDS,
                 "description": "회의·미팅: 회의·미팅·상담·방문·면담 / 회사 행사: 프로모션·출시·행사 기간 / "
                                "다른 사람 마감: 김 대리가 아닌 사람의 업무 마감 / 기타"},
        "when_text": {"type": "string", "description": "시점 표현 원문 그대로"},
        "date_type": {"type": "string", "enum": ["exact", "approximate", "none"]},
        "date": {"type": "string", "description": "exact일 때만 YYYY-MM-DD(기간이면 시작일), 아니면 빈 문자열"},
        "end_date": {"type": "string", "description": "기간 일정의 마지막 날 YYYY-MM-DD, 하루짜리면 빈 문자열"},
        "start": {"type": "string", "description": "시작 시간 HH:MM(24시간), 언급 없으면 빈 문자열"},
        "end": {"type": "string", "description": "끝나는 시간 HH:MM, 언급 없으면 빈 문자열"},
        "place": {"type": "string", "description": "장소, 언급 없으면 빈 문자열"},
        "kim_attends": {"type": "boolean", "description": "김 대리가 참석자로 언급되었으면 true"},
        "evidence": {"type": "string", "description": "원문에서 그대로 복사한 근거 문장 1개"},
    },
    "required": ["title", "kind", "when_text", "date_type", "date", "end_date", "start", "end", "place",
                 "kim_attends", "evidence"],
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
                      "description": "회의에서 언급된 일정(회의·미팅, 프로모션·출시 기간, 다른 사람 마감 등)"},
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
        "- kim_tasks에는 김 대리가 해야 할 '작업'(문서·보고서 작성, 자료 준비, 조사, 메일 확인 등)만 넣습니다.",
        "  회의·미팅·상담 '참석'은 kim_tasks가 아니라 schedules에 넣습니다.",
        "- schedules: 회의에서 언급된 일정. 회의·미팅·상담·방문은 kind='회의·미팅', 프로모션·출시·행사 기간은 '회사 행사',",
        "  김 대리가 아닌 사람의 업무 마감은 '다른 사람 마감'. 김 대리 업무의 마감은 kim_tasks에 있으므로 넣지 않습니다.",
        "  when_text는 원문 표현, date_type/date는 위 기한 규칙과 같게. 기간이면 date=시작일, end_date=마지막 날.",
        "  start/end는 원문에 시간이 있을 때만 HH:MM. kim_attends는 김 대리가 참석자로 언급됐을 때만 true.",
        "  이미 잡혀 있는 기존 일정이 언급되어도(예: 김 대리 캘린더에 있는 상담) 그대로 schedules에 넣습니다.",
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
    lines += ["", "## 회의에서 언급된 일정"]
    lines += [f"- {s['title']}: {s['when_text'] or '시점 언급 없음'}" for s in n["schedules"]] or ["- (없음)"]
    return "\n".join(lines)


with tab_meet:
    st.markdown("회의를 녹음하거나 녹음·텍스트 파일을 올리면 에이블이 요약하고, 분석이 끝나면 **자동으로** "
                "문서 작성·보고서 제출 같은 **김 대리 업무는 할 일**로, **회의·미팅은 캘린더 일정**으로 등록합니다.")
    st.caption("📅 정확한 날짜가 있는 할 일은 마감일로 캘린더에 표시되고, 김 대리가 참석하는 회의·미팅과 회사 행사(프로모션 기간 등)는 "
               "일정으로 들어갑니다. 다른 사람의 마감, 날짜가 정확하지 않은 일정, 원문에서 근거를 찾지 못한 내용은 참고로만 보여 드려요.")

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
                st.write("③ 할 일(문서·보고서 등)은 대시보드에, 회의·미팅은 캘린더 일정으로 반영 중")
                note_title = m_title.strip() or res["title_guess"].strip() or f"{today.isoformat()} 회의"
                work, meet_like = split_meeting_tasks(res["kim_tasks"])
                tasks = apply_kim_tasks(work, text, "회의 분석")
                schedules = apply_meeting_schedules(res["schedules"] + meet_like, text, note_title)
                note = {
                    "id": uuid.uuid4().hex[:8],
                    "title": note_title,
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
                n_ev = sum(s["cal_status"] in ("calendar", "conflict") for s in schedules)
                n_cf = sum(s["cal_status"] == "conflict" for s in schedules)
                status.update(label="분석 완료!", state="complete")
                done = True
            except Exception as e:
                status.update(label="처리 중 오류가 발생했습니다", state="error")
                st.error(f"오류 내용: {e}")
        if done:
            st.session_state.meet_view = note["id"]
            st.session_state.pending_toast = (f"할 일 {n_add}건(마감 표시 {n_cal}건)과 일정 {n_ev}건을 캘린더에 추가했어요."
                                              + (f" ⚠️ 겹치는 일정 {n_cf}건은 회의록에서 확인하세요." if n_cf else ""))
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
            if not n["schedules"]:
                st.caption("언급된 일정이 없습니다.")
            order = {"conflict": 0, "calendar": 1, "duplicate": 2, "reference": 3, "unverified": 4}
            for s in sorted(n["schedules"], key=lambda x: (order.get(x.get("cal_status"), 3), x["date"] or "9999")):
                when = s["when_text"] or "시점 언급 없음"
                if s["date_type"] == "exact" and parse_date(s["date"]):
                    when = fmt_date(parse_date(s["date"]))
                    if parse_date(s.get("end_date")):
                        when += f" ~ {fmt_date(parse_date(s['end_date']))}"
                    if s.get("start"):
                        when += f" {s['start']}" + (f"–{s['end']}" if s.get("end") else "")
                st.markdown(f"**{s['title']}** · {when}" + (f" · {s['place']}" if s.get("place") else "")
                            + f"  \n:gray[{s.get('kind', '기타')}]")
                line = schedule_status_line(s)
                {"calendar": st.success, "conflict": st.warning, "unverified": st.warning}.get(
                    s.get("cal_status"), st.caption)(line)

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
            linked_ev = {i for s in n["schedules"] if s.get("cal_status") in ("calendar", "conflict")
                         for i in s.get("event_ids", [])}
            linked_ev_count = sum(e["id"] in linked_ev for e in data["events"])
            with st.container(border=True):
                st.markdown("#### 🗑️ 삭제할 범위를 선택해 주세요")
                scope = st.radio(
                    "삭제 범위",
                    ["회의록만 삭제", "회의록과 연결된 할 일·일정도 삭제"],
                    index=0,
                    key=f"meeting_delete_scope_{n['id']}",
                )
                if scope == "회의록만 삭제":
                    st.info("회의 원문과 요약을 삭제합니다. 연결된 할 일과 캘린더 마감 표시는 유지됩니다.")
                else:
                    st.warning(f"회의록과 연결된 할 일 {linked_count}건, 이 회의 분석으로 추가한 일정 {linked_ev_count}건을 "
                               "함께 삭제합니다. 원래 캘린더에 있던 일정과 직접 등록한 일정은 유지됩니다.")
                cancel_col, confirm_col = st.columns(2)
                if cancel_col.button("취소", key=f"meeting_delete_cancel_{n['id']}", use_container_width=True):
                    st.session_state.pop("meeting_delete_pending", None)
                    st.rerun()
                if confirm_col.button("선택한 항목 삭제", key=f"meeting_delete_confirm_{n['id']}",
                                      type="primary", use_container_width=True):
                    delete_tasks = scope == "회의록과 연결된 할 일·일정도 삭제"
                    if delete_tasks:
                        data["todos"] = [t for t in data["todos"] if t["id"] not in linked_ids]
                        data["events"] = [e for e in data["events"]
                                          if not (e["id"] in linked_ev and e.get("source") == "회의 분석")]
                    data["meeting_notes"] = [item for item in data["meeting_notes"] if item["id"] != n["id"]]
                    save_data()
                    st.session_state.pop("meeting_delete_pending", None)
                    st.session_state.pop("meet_view", None)
                    st.session_state.pending_toast = (
                        f"회의록과 연결된 할 일 {linked_count}건, 일정 {linked_ev_count}건을 삭제했어요."
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
with SIDE_MAIN:
    st.markdown(f'<div class="able-side-brand"><span class="able-mark"></span><div><b>에이블</b>'
                f'<div class="able-meta">김 대리님의 AI 개인비서</div></div></div>', unsafe_allow_html=True)

    # 0) GPT처럼 새 채팅 + 대화 기록
    st.button("새 채팅", key="new_chat_side", icon=":material/add:", on_click=new_chat_cb, type="primary",
              use_container_width=True)
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
