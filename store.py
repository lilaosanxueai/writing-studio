"""会话与本地状态持久化

目录结构（均在 data/ 下，已 gitignore）：
  data/sessions/{session_id}.json   每个话题一个文件
  data/state.json                   全局状态（飞书文件夹 token、待写入队列等）

会话数据模型：
  {
    id, title, topic, mode, created_at, updated_at,
    messages: [{role, content, ts}],
    sparks:   [{id, text, note, ts, origin}],        # origin: ai/user/manual/summary
    drafts:   [{id, format, tone, length, instruction, content, created_at, updated_at, history: []}],
    summary:  {text, ts} | None,
    feishu:   {doc_id, url, turns_synced, sparks_synced, summarized: bool, drafts_synced: []}
  }
"""
import json
import os
import threading
import time
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
STATE_FILE = os.path.join(DATA_DIR, "state.json")

_lock = threading.Lock()

DISCUSSION_MODES = {
    "free": "自由聊",
    "brainstorm": "头脑风暴",
    "deepdive": "深挖追问",
    "challenge": "唱反调",
}


def now_ts() -> float:
    return time.time()


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _ensure_dirs():
    os.makedirs(SESSIONS_DIR, exist_ok=True)


def today() -> str:
    return time.strftime("%Y-%m-%d")


def today_label() -> str:
    return time.strftime("%Y年%m月%d日")


# ---------------------------------------------------------------------------
# 全局状态
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(st: dict):
    _ensure_dirs()
    with _lock:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------
def new_session(title: str = "", mode: str = "free") -> dict:
    s = {
        "id": new_id(),
        "title": (title or "").strip() or f"未命名话题 · {today_label()}",
        "topic": (title or "").strip(),
        "mode": mode if mode in DISCUSSION_MODES else "free",
        "created_at": now_ts(),
        "updated_at": now_ts(),
        "messages": [],
        "sparks": [],
        "drafts": [],
        "summary": None,
        "feishu": {"doc_id": "", "url": "", "turns_synced": 0,
                   "sparks_synced": 0, "summarized": False, "drafts_synced": []},
        "auto_titled": not bool((title or "").strip()),
    }
    save_session(s)
    return s


def session_path(sid: str) -> str:
    return os.path.join(SESSIONS_DIR, f"{sid}.json")


def load_session(sid: str) -> dict | None:
    p = session_path(sid)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_session(s: dict):
    _ensure_dirs()
    s["updated_at"] = now_ts()
    with _lock:
        with open(session_path(s["id"]), "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)


def delete_session(sid: str) -> bool:
    p = session_path(sid)
    if os.path.exists(p):
        os.remove(p)
        return True
    return False


def list_sessions() -> list:
    """按更新时间倒序列出会话摘要"""
    out = []
    if os.path.isdir(SESSIONS_DIR):
        for fn in os.listdir(SESSIONS_DIR):
            if not fn.endswith(".json"):
                continue
            s = load_session(fn[:-5])
            if s:
                out.append({
                    "id": s["id"],
                    "title": s["title"],
                    "mode": s.get("mode", "free"),
                    "updated_at": s.get("updated_at", 0),
                    "message_count": len(s.get("messages", [])),
                    "spark_count": len(s.get("sparks", [])),
                    "draft_count": len(s.get("drafts", [])),
                })
    out.sort(key=lambda x: x["updated_at"], reverse=True)
    return out
