"""✍️ 写作共创坊 —— 后端服务

一句话：和 AI 搭档一起聊话题、撞灵感；讨论实时记录进飞书云文档；
聊完的素材一键整理成可直接用的文案。

启动：python app.py          （默认 http://127.0.0.1:8320）
自检：python app.py --check  （LLM + 飞书连通性）
联调：WS_MOCK=1 python app.py --check   （不真正调飞书/大模型）
"""
import asyncio
import json
import logging
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台中文
    sys.stderr.reconfigure(encoding="utf-8")

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import feishu
import prompts
import store
from feishu import Feishu, FeishuWriter
from llm import LLM

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("studio")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
PORT = 8320

MOCK = feishu.MOCK

DEFAULT_CONFIG = {
    "llm": {
        "api_key": "",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",        # 聊天：快
        "fast_model": "deepseek-flash",   # 起标题等轻任务
        "draft_model": "deepseek-v4-pro", # 写文案：质量优先
        "backup_api_key": "",
        "backup_base_url": "",
        "backup_model": "",
    },
    "feishu": {
        "app_id": "",
        "app_secret": "",
        "folder_token": "",       # 留空 = 自动建「✍️ 写作共创坊」文件夹
        "auto_share_tenant": True,
        "auto_record": True,      # 讨论实时写入飞书
    },
    "server": {
        "lan": False,
        "access_token": "",
    },
    "image": {
        "api_key": "",                       # APIMart GPT-Image-2；留空=配图只出提示词
        "base_url": "https://api.apimart.ai/v1",
    },
}


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def load_config() -> dict:
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
    merged = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    for k, v in cfg.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k].update(v)
        else:
            merged[k] = v
    return merged


def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


CONFIG = load_config()
llm = LLM(CONFIG["llm"])
fs = Feishu(CONFIG["feishu"])
writer = FeishuWriter(fs)

_session_locks: dict[str, asyncio.Lock] = {}


def _lock(sid: str) -> asyncio.Lock:
    if sid not in _session_locks:
        _session_locks[sid] = asyncio.Lock()
    return _session_locks[sid]


# ---------------------------------------------------------------------------
# 会话工具
# ---------------------------------------------------------------------------
def _get_session(sid: str) -> dict:
    s = store.load_session(sid)
    if not s:
        raise HTTPException(404, "会话不存在")
    return s


def _transcript(s: dict, char_budget: int = 16000) -> str:
    """把讨论记录拼成给大模型看的文稿（超预算从最旧开始截断）"""
    lines = [f"【话题】{s.get('topic') or s['title']}（模式：{store.DISCUSSION_MODES.get(s.get('mode'), '自由聊')}）", ""]
    for m in s.get("messages", []):
        who = "用户" if m["role"] == "user" else "搭档"
        lines.append(f"{who}：{m['content']}")
    text = "\n".join(lines)
    if len(text) > char_budget:
        text = "（更早的讨论已省略）\n" + text[-char_budget:]
    return text


def _sparks_text(s: dict) -> str:
    items = [sp["text"] for sp in s.get("sparks", []) if sp.get("text")]
    return "\n".join(f"- {t}" for t in items) or "（暂无）"


def _chat_messages(s: dict) -> list:
    sparks = [sp["text"] for sp in s.get("sparks", []) if sp.get("text")]
    profile = store.get_profile().get("text", "")
    sys_prompt = prompts.partner_system_prompt(s.get("topic") or s["title"], s.get("mode", "free"),
                                               sparks, profile, s.get("persona", "buddy"))
    history = s.get("messages", [])
    # 近 40 轮、总字数 12000 以内
    msgs, budget = [], 12000
    for m in reversed(history):
        if len(msgs) >= 40 or budget <= 0:
            break
        msgs.insert(0, {"role": m["role"], "content": m["content"]})
        budget -= len(m["content"])
    return [{"role": "system", "content": sys_prompt}] + msgs


def _sse(chunk: dict) -> str:
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


async def _auto_title(s: dict) -> str | None:
    """首轮流后自动起标题（快模型、关思考）；失败静默"""
    msgs = s.get("messages", [])
    if len(msgs) < 2:
        return None
    first = f"用户：{msgs[0]['content']}\n搭档：{msgs[1]['content'][:300]}"
    try:
        title = await llm.chat_once(
            [{"role": "user", "content": prompts.title_prompt(first)}],
            model=llm.fast_model, max_tokens=64, thinking=False,
        )
        title = title.strip().strip('"「」《》').splitlines()[0][:16]
        if title:
            return title
    except Exception as e:
        log.warning("自动起标题失败: %s", e)
    return None


def _extract_json(text: str) -> dict:
    """从模型输出里抠出第一个 JSON 对象（容忍代码块围栏/前后废话）"""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


async def _analyze_topic(sid: str) -> dict | None:
    """话题自动分析：分类/关键词/定位/推荐格式，写回会话；失败静默返回 None"""
    async with _lock(sid):
        s = _get_session(sid)
        if not s.get("messages"):
            return None
        try:
            raw = await llm.chat_once(
                [{"role": "user", "content": prompts.topic_analysis_prompt(
                    store.TOPIC_CATEGORIES, prompts.DRAFT_FORMATS) + _transcript(s, 6000)}],
                model=llm.fast_model, max_tokens=300, temperature=0.3, thinking=False,
            )
            data = _extract_json(raw)
            if not data:
                raise RuntimeError(f"无法解析: {raw[:80]}")
            cat = data.get("category")
            if cat not in store.TOPIC_CATEGORIES:
                cat = "other"
            try:
                maturity = max(0, min(100, int(data.get("maturity", 0))))
            except (TypeError, ValueError):
                maturity = 0
            analysis = {
                "category": cat,
                "tags": [str(t).strip()[:12] for t in (data.get("tags") or []) if str(t).strip()][:5],
                "summary": str(data.get("summary", "")).strip()[:60],
                "maturity": maturity,
                "maturity_hint": str(data.get("maturity_hint", "")).strip()[:40],
                "recommended_formats": [f for f in (data.get("recommended_formats") or [])
                                        if f in prompts.DRAFT_FORMATS][:2],
                "analyzed_turns": sum(1 for m in s["messages"] if m["role"] == "user"),
                "ts": store.now_ts(),
            }
            s["topic_analysis"] = analysis
            store.save_session(s)
            log.info("话题分析完成(sid=%s): %s %s", sid, cat, analysis["tags"])
            return analysis
        except Exception as e:
            log.warning("话题分析失败: %s", e)
            return None


async def _refresh_profile() -> str | None:
    """写作画像：用最近活跃会话的讨论材料更新跨话题记忆；失败静默"""
    try:
        materials = []
        for meta in store.list_sessions()[:2]:
            s = store.load_session(meta["id"])
            if s and s.get("messages"):
                materials.append(f"【话题：{s['title']}】\n{_transcript(s, 4000)}")
        if not materials:
            return None
        raw = await llm.chat_once(
            [{"role": "user", "content": prompts.profile_prompt() + "\n\n".join(materials)}],
            model=llm.fast_model, max_tokens=400, temperature=0.4, thinking=False,
        )
        text = raw.strip()
        if len(text) < 20:
            raise RuntimeError("画像过短")
        store.save_profile(text, store.get_total_user_turns())
        log.info("写作画像已更新（%d 字）", len(text))
        return text
    except Exception as e:
        log.warning("画像更新失败: %s", e)
        return None


# ---------------------------------------------------------------------------
# 灵感库 & 灵感碰撞
# ---------------------------------------------------------------------------
def _all_sparks() -> list:
    """跨话题汇总所有灵感卡片（新→旧）"""
    out = []
    for meta in store.list_sessions():
        s = store.load_session(meta["id"])
        if not s:
            continue
        for sp in s.get("sparks", []):
            out.append({
                **sp,
                "session_id": s["id"],
                "session_title": s["title"],
                "category": (s.get("topic_analysis") or {}).get("category", ""),
            })
    return out


# ---------------------------------------------------------------------------
# 飞书同步（后台任务，不阻塞聊天）
# ---------------------------------------------------------------------------
_bg_tasks: set = set()


async def _sync_turn_to_feishu(sid: str):
    """对外入口：拿锁后执行同步（聊天流程里以后台 task 调用）"""
    async with _lock(sid):
        await _sync_turn_locked(sid)


async def _sync_turn_locked(sid: str):
    """确保文档存在并补齐落后的轮次/灵感。调用方需持有该会话的锁。"""
    s = _get_session(sid)
    f = s.setdefault("feishu", {})
    if not (fs.enabled and fs.auto_record) and not MOCK:
        return
    try:
        if not f.get("doc_id"):
            doc = await asyncio.to_thread(
                fs.create_doc, f"{s['title']} · {store.today()}"
            )
            f["doc_id"], f["url"] = doc["token"], doc["url"]
            blocks = feishu.session_head_blocks(
                s["title"], store.DISCUSSION_MODES.get(s.get("mode"), "自由聊"), store.today_label()
            )
            await writer.enqueue(f["doc_id"], blocks, label="文档头")
            f["turns_synced"] = 0
            f["sparks_synced"] = 0
        # 成对补写（user + assistant 才算一轮；孤儿消息跳过）
        msgs = s.get("messages", [])
        turns = []
        i = 0
        while i + 1 < len(msgs):
            if msgs[i]["role"] == "user" and msgs[i + 1]["role"] == "assistant":
                turns.append((msgs[i]["content"], msgs[i + 1]["content"]))
                i += 2
            else:
                i += 1
        n = f.get("turns_synced", 0)
        for u_text, a_text in turns[n:]:
            await writer.enqueue(
                f["doc_id"], feishu.turn_blocks(n + 1, u_text, a_text), label=f"第{n + 1}轮",
            )
            n += 1
        f["turns_synced"] = n
        sparks = s.get("sparks", [])
        m = f.get("sparks_synced", 0)
        if len(sparks) > m:
            if m == 0:
                await writer.enqueue(f["doc_id"], feishu.sparks_head_blocks(), label="灵感头")
            for sp in sparks[m:]:
                await writer.enqueue(f["doc_id"], [feishu.spark_block(sp["text"])], label="灵感")
            f["sparks_synced"] = len(sparks)
        f.pop("error", None)
        store.save_session(s)
    except Exception as e:
        log.error("飞书同步失败(sid=%s): %s", sid, e)
        f["error"] = str(e)[:200]
        store.save_session(s)


# ---------------------------------------------------------------------------
# 鉴权（默认关；server.access_token 配置后 /api/* 需带 X-Access-Token）
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    writer.start()
    asyncio.create_task(_auto_weekly_loop())
    yield
    await llm.close()
    fs.http.close()


async def _auto_weekly_loop():
    """自动周报：新一周开始后首次运行时，自动生成上周周报并推送飞书（开关可关）"""
    while True:
        try:
            cfg = load_config().get("feishu", {})
            if cfg.get("auto_weekly") and (fs.enabled or feishu.MOCK):
                st = store.load_state()
                pushed = (st.get("auto_weekly") or {}).get("week", "")
                this_week = store.today()[:7] + "-w" + _isoweek()
                if pushed != this_week:
                    # 上周有成稿才推
                    import time as _t
                    import datetime
                    today = datetime.date.today()
                    last_monday = today - datetime.timedelta(days=today.weekday() + 7)
                    ws = _t.mktime(last_monday.timetuple())
                    we = ws + 7 * 86400
                    drafts = 0
                    for meta in store.list_sessions():
                        s2 = store.load_session(meta["id"])
                        if s2:
                            drafts += sum(1 for dd in s2.get("drafts", [])
                                          if ws <= dd.get("created_at", 0) < we)
                    if drafts > 0:
                        log.info("自动周报：生成上周（%d 篇成稿）并推送飞书", drafts)
                        text = await llm.chat_once(
                            [{"role": "user", "content": prompts.weekly_report_prompt(_week_stats_text())}],
                            model=llm.draft_model, max_tokens=1500, temperature=0.7, thinking=False,
                        )
                        st = store.load_state()
                        st["weekly_report"] = {"week": this_week, "text": text.strip(), "ts": store.now_ts()}
                        doc = st.get("weekly_doc") or {}
                        if not doc.get("token"):
                            created = await asyncio.to_thread(fs.create_doc, "📊 写作共创坊 · 周报")
                            doc = {"token": created["token"], "url": created["url"]}
                            st["weekly_doc"] = doc
                        await writer.enqueue(doc["token"],
                                             feishu.summary_blocks(f"📈 写作周报 · {store.today_label()}", text.strip()),
                                             label="自动周报")
                        st["auto_weekly"] = {"week": this_week, "ts": store.now_ts()}
                        store.save_state(st)
                    else:
                        st["auto_weekly"] = {"week": this_week, "ts": store.now_ts()}
                        store.save_state(st)
        except Exception as e:
            log.warning("自动周报任务失败: %s", e)
        await asyncio.sleep(3600)


app = FastAPI(title="写作共创坊", lifespan=lifespan)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    token = (CONFIG.get("server", {}).get("access_token") or "").strip()
    if token and request.url.path.startswith("/api"):
        if request.headers.get("X-Access-Token") != token:
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "需要访问令牌"}, status_code=401)
    return await call_next(request)


# ---------------------------------------------------------------------------
# 页面 & 配置
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/config")
def get_config():
    llm_cfg = CONFIG["llm"]
    fs_cfg = CONFIG["feishu"]
    return {
        "llm": {
            "base_url": llm_cfg.get("base_url", ""),
            "model": llm_cfg.get("model", ""),
            "draft_model": llm_cfg.get("draft_model", ""),
            "fast_model": llm_cfg.get("fast_model", ""),
            "backup_base_url": llm_cfg.get("backup_base_url", ""),
            "backup_model": llm_cfg.get("backup_model", ""),
            "has_key": bool(llm_cfg.get("api_key")),
            "has_backup_key": bool(llm_cfg.get("backup_api_key")),
        },
        "feishu": {
            "app_id": fs_cfg.get("app_id", ""),
            "folder_token": fs_cfg.get("folder_token", ""),
            "auto_share_tenant": bool(fs_cfg.get("auto_share_tenant")),
            "auto_record": bool(fs_cfg.get("auto_record", True)),
            "auto_weekly": bool(fs_cfg.get("auto_weekly", True)),
            "enabled": fs.enabled,
        },
        "has_user_style": bool(store.load_state().get("user_style_card", {}).get("text")),
        "image": {
            "has_key": bool(CONFIG.get("image", {}).get("api_key")),
            "base_url": CONFIG.get("image", {}).get("base_url", ""),
        },
        "server": {"lan": bool(CONFIG.get("server", {}).get("lan"))},
        "modes": store.DISCUSSION_MODES,
        "personas": prompts.PARTNER_PERSONAS,
        "topic_categories": store.TOPIC_CATEGORIES,
        "draft_formats": prompts.DRAFT_FORMATS,
        "draft_tones": prompts.DRAFT_TONES,
        "draft_lengths": prompts.DRAFT_LENGTHS,
        "draft_styles": prompts.DRAFT_STYLES,
        "draft_style_groups": prompts.DRAFT_STYLE_GROUPS,
    }


class ConfigUpdate(BaseModel):
    llm: Optional[dict] = None
    feishu: Optional[dict] = None
    image: Optional[dict] = None


@app.post("/api/config")
async def update_config(body: ConfigUpdate):
    global CONFIG, llm, fs
    if body.llm:
        CONFIG["llm"].update({k: v for k, v in body.llm.items() if v is not None})
    if body.feishu:
        old_app = CONFIG["feishu"].get("app_id")
        CONFIG["feishu"].update({k: v for k, v in body.feishu.items() if v is not None})
        if CONFIG["feishu"].get("app_id") != old_app:  # 换应用后旧文件夹缓存作废
            st = store.load_state()
            st.pop("folder_token", None)
            st.pop("domain", None)
            store.save_state(st)
    if body.image:
        CONFIG.setdefault("image", {}).update({k: v for k, v in body.image.items() if v is not None})
    save_config(CONFIG)
    llm.update(CONFIG["llm"])
    fs.update(CONFIG["feishu"])
    return {"ok": True}


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------
@app.get("/api/sessions")
def sessions_list():
    return store.list_sessions()


class SessionCreate(BaseModel):
    title: str = ""
    mode: str = "free"
    persona: str = "buddy"
    seed: str = ""   # 搭档开场白（灵感碰撞「开聊」用），作为首条 assistant 消息注入


@app.post("/api/sessions")
def session_create(body: SessionCreate):
    s = store.new_session(body.title, body.mode,
                          body.persona if body.persona in prompts.PARTNER_PERSONAS else "buddy")
    if body.seed.strip():
        s["messages"].append({"role": "assistant", "content": body.seed.strip()[:600],
                              "ts": store.now_ts()})
        store.save_session(s)
    return s


@app.get("/api/sessions/{sid}")
def session_get(sid: str):
    return _get_session(sid)


class SessionPatch(BaseModel):
    title: Optional[str] = None
    mode: Optional[str] = None
    persona: Optional[str] = None


@app.patch("/api/sessions/{sid}")
async def session_patch(sid: str, body: SessionPatch):
    async with _lock(sid):
        s = _get_session(sid)
        if body.title is not None and body.title.strip():
            s["title"] = body.title.strip()[:40]
            s["auto_titled"] = False
            if s.get("feishu", {}).get("doc_id"):
                try:
                    await asyncio.to_thread(fs.rename_doc, s["feishu"]["doc_id"], s["title"])
                except Exception as e:
                    log.warning("飞书文档改名失败: %s", e)
        if body.mode is not None and body.mode in store.DISCUSSION_MODES:
            s["mode"] = body.mode
        if body.persona is not None and body.persona in prompts.PARTNER_PERSONAS:
            s["persona"] = body.persona
        store.save_session(s)
        return s


@app.delete("/api/sessions/{sid}")
def session_delete(sid: str):
    if not store.delete_session(sid):
        raise HTTPException(404, "会话不存在")
    return {"ok": True}


@app.post("/api/sessions/{sid}/analyze")
async def session_analyze(sid: str):
    """手动重跑话题分析"""
    s = store.load_session(sid)
    if not s:
        raise HTTPException(404, "会话不存在")
    if not s.get("messages"):
        raise HTTPException(400, "还没有讨论内容")
    r = await _analyze_topic(sid)
    if not r:
        raise HTTPException(502, "分析失败，稍后再试")
    return {"analysis": r}


# ---------------------------------------------------------------------------
# 聊天（SSE 流式）
# ---------------------------------------------------------------------------
class ChatBody(BaseModel):
    message: str


@app.post("/api/sessions/{sid}/chat")
async def chat(sid: str, body: ChatBody):
    text = body.message.strip()
    if not text:
        raise HTTPException(400, "消息为空")

    async def gen():
        async with _lock(sid):
            s = _get_session(sid)
            s["messages"].append({"role": "user", "content": text, "ts": store.now_ts()})
            first_turn = len(s["messages"]) == 1
            store.save_session(s)

            full, ok = "", False
            try:
                async for piece in llm.chat_stream(_chat_messages(s), model=llm.model):
                    full += piece
                    yield _sse({"t": "delta", "v": piece})
                ok = bool(full.strip())
                if not ok:
                    raise RuntimeError("空回复")
            except Exception as e:
                log.error("聊天生成失败: %s", e)
                yield _sse({"t": "error", "v": str(e)[:300]})
                return

            s = _get_session(sid)  # 期间灵感可能被加进来，重读
            s["messages"].append({"role": "assistant", "content": full, "ts": store.now_ts()})
            if not s.get("topic"):
                s["topic"] = text[:30]

            title_changed = False
            if first_turn and s.get("auto_titled"):
                t = await _auto_title(s)
                if t:
                    s["title"] = t
                    title_changed = True
            store.save_session(s)

            # 飞书后台同步（不阻塞响应）
            task = asyncio.create_task(_sync_turn_to_feishu(sid))
            _bg_tasks.add(task)
            task.add_done_callback(_bg_tasks.discard)

            # 话题自动分析：首轮后跑一次，之后每 8 轮自动刷新
            user_turns = sum(1 for m in s["messages"] if m["role"] == "user")
            analyzed = (s.get("topic_analysis") or {}).get("analyzed_turns", 0)
            if user_turns == 1 or user_turns - analyzed >= 8:
                t2 = asyncio.create_task(_analyze_topic(sid))
                _bg_tasks.add(t2)
                t2.add_done_callback(_bg_tasks.discard)

            # 写作画像：全局累计每 +6 轮用户发言，后台静默更新
            total = store.bump_total_user_turns()
            last = store.get_profile().get("total_turns", 0)
            if total - last >= 6:
                t3 = asyncio.create_task(_refresh_profile())
                _bg_tasks.add(t3)
                t3.add_done_callback(_bg_tasks.discard)

            yield _sse({"t": "done", "title": s["title"] if title_changed else None,
                        "syncing": True})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# 小结
# ---------------------------------------------------------------------------
@app.post("/api/sessions/{sid}/summary")
async def summary(sid: str):
    async with _lock(sid):
        s = _get_session(sid)
        if not s.get("messages"):
            raise HTTPException(400, "还没有讨论内容")
        try:
            text = await llm.chat_once(
                [
                    {"role": "system", "content": prompts.summary_prompt()},
                    {"role": "user", "content": _transcript(s)},
                ],
                model=llm.draft_model, max_tokens=2048, thinking=False,
            )
        except Exception as e:
            raise HTTPException(502, f"小结生成失败: {e}")
        s["summary"] = {"text": text, "ts": store.now_ts()}
        f = s.setdefault("feishu", {})
        if f.get("doc_id") and (fs.enabled and fs.auto_record or MOCK):
            await writer.enqueue(f["doc_id"], feishu.summary_blocks(text, store.today_label()),
                                 label="小结")
            f["summarized"] = True
        store.save_session(s)
        return {"summary": s["summary"]}


# ---------------------------------------------------------------------------
# 灵感
# ---------------------------------------------------------------------------
class SparkBody(BaseModel):
    text: str
    origin: str = "manual"   # ai / user / manual
    note: str = ""


@app.post("/api/sessions/{sid}/sparks")
async def spark_add(sid: str, body: SparkBody):
    async with _lock(sid):
        s = _get_session(sid)
        sp = {"id": store.new_id(), "text": body.text.strip()[:500],
              "note": body.note.strip(), "origin": body.origin, "ts": store.now_ts()}
        if not sp["text"]:
            raise HTTPException(400, "灵感内容为空")
        # 查重：同话题内前 16 字相同视为重复
        head = sp["text"][:16]
        for old in s.get("sparks", []):
            if old["text"][:16] == head:
                raise HTTPException(409, f"已有相似灵感：「{old['text'][:24]}…」")
        s.setdefault("sparks", []).append(sp)
        f = s.setdefault("feishu", {})
        if f.get("doc_id") and (fs.enabled and fs.auto_record or MOCK):
            if not f.get("sparks_synced"):
                await writer.enqueue(f["doc_id"], feishu.sparks_head_blocks(), label="灵感头")
            await writer.enqueue(f["doc_id"], [feishu.spark_block(sp["text"])], label="灵感")
            f["sparks_synced"] = f.get("sparks_synced", 0) + 1
        store.save_session(s)
        return sp


class SparkPatch(BaseModel):
    text: Optional[str] = None
    note: Optional[str] = None


@app.patch("/api/sessions/{sid}/sparks/{spid}")
def spark_patch(sid: str, spid: str, body: SparkPatch):
    s = _get_session(sid)
    for sp in s.get("sparks", []):
        if sp["id"] == spid:
            if body.text is not None:
                sp["text"] = body.text.strip()[:500]
            if body.note is not None:
                sp["note"] = body.note.strip()
            store.save_session(s)
            return sp
    raise HTTPException(404, "灵感不存在")


@app.delete("/api/sessions/{sid}/sparks/{spid}")
def spark_delete(sid: str, spid: str):
    s = _get_session(sid)
    before = len(s.get("sparks", []))
    s["sparks"] = [sp for sp in s.get("sparks", []) if sp["id"] != spid]
    if len(s["sparks"]) == before:
        raise HTTPException(404, "灵感不存在")
    store.save_session(s)
    return {"ok": True}


# ---------------------------------------------------------------------------
# 灵感库（全局） & 灵感碰撞器 & 写作画像
# ---------------------------------------------------------------------------
@app.get("/api/sparks/all")
def sparks_library(q: str = "", category: str = ""):
    """跨话题灵感库，支持关键词过滤与分类过滤"""
    q = q.strip().lower()
    out = []
    for sp in _all_sparks():
        if q and q not in sp["text"].lower() and q not in sp.get("session_title", "").lower():
            continue
        if category and sp.get("category") != category:
            continue
        out.append(sp)
    return {"sparks": out, "total": len(_all_sparks())}


class CollideBody(BaseModel):
    count: int = 3   # 抽几条灵感参与碰撞


@app.post("/api/sparks/collide")
async def sparks_collide(body: CollideBody):
    """灵感碰撞器：随机抽旧灵感 → 找隐秘关联 → 3 个新话题方向"""
    import random
    pool = _all_sparks()
    if len(pool) < 2:
        raise HTTPException(400, "灵感还太少（至少 2 条），先去攒点灵感再来碰撞")
    n = max(2, min(body.count or 3, min(5, len(pool))))
    picked = random.sample(pool, n)
    sparks_text = "\n".join(f"- {sp['text']}（来自话题「{sp['session_title']}」）" for sp in picked)
    try:
        raw = await llm.chat_once(
            [{"role": "user", "content": prompts.collide_prompt(sparks_text)}],
            model=llm.draft_model, max_tokens=800, temperature=0.9, thinking=False,
        )
        data = _extract_json(raw)
        dirs = data.get("directions") or []
        if not dirs:
            raise RuntimeError(f"无法解析: {raw[:80]}")
        out_dirs = []
        for d in dirs[:3]:
            if not isinstance(d, dict) or not d.get("title"):
                continue
            out_dirs.append({
                "title": str(d["title"]).strip()[:20],
                "why": str(d.get("why", "")).strip()[:60],
                "hook": str(d.get("hook", "")).strip()[:120],
            })
        if not out_dirs:
            raise RuntimeError("方向解析为空")
        return {"connections": str(data.get("connections", "")).strip()[:120],
                "directions": out_dirs,
                "picked": [sp["text"] for sp in picked]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"碰撞失败: {e}")


@app.get("/api/profile")
def profile_get():
    p = store.get_profile()
    return {"profile": p.get("text", ""), "updated_at": p.get("ts", 0),
            "total_turns": store.get_total_user_turns()}


@app.post("/api/profile/refresh")
async def profile_refresh():
    text = await _refresh_profile()
    if not text:
        raise HTTPException(502, "画像更新失败（先聊几轮再来）")
    return {"profile": text}


# ---------------------------------------------------------------------------
# 今日写作提示 / 话题导出 / 灵感库飞书同步
# ---------------------------------------------------------------------------
async def _gen_daily_prompt() -> dict:
    profile = store.get_profile().get("text", "")
    sparks = "\n".join(f"- {sp['text']}" for sp in _all_sparks()[:30])
    recent = "\n".join(f"- {m['title']}" for m in store.list_sessions()[:5])
    backlog_items = store.load_state().get("topic_backlog", [])
    backlog = "\n".join(f"- {x['title']}" + (f"（{x['note']}）" if x.get("note") else "")
                        for x in backlog_items[:10])
    raw = await llm.chat_once(
        [{"role": "user", "content": prompts.daily_prompt_prompt(profile, sparks, recent, backlog)}],
        model=llm.draft_model, max_tokens=300, temperature=0.9, thinking=False,
    )
    data = _extract_json(raw)
    if not data.get("opening"):
        raise RuntimeError(f"无法解析: {raw[:80]}")
    out = {
        "opening": str(data["opening"]).strip()[:80],
        "angle": str(data.get("angle", "")).strip()[:40],
        "dare": str(data.get("dare", "")).strip()[:40],
    }
    st = store.load_state()
    st["daily_prompt"] = {"date": store.today(), "data": out, "ts": store.now_ts()}
    store.save_state(st)
    return out


@app.get("/api/daily_prompt")
async def daily_prompt(refresh: str = ""):
    """今日写作提示（当天缓存；?refresh=1 换一个）"""
    if refresh != "1":
        cached = store.load_state().get("daily_prompt")
        if cached and cached.get("date") == store.today() and cached.get("data"):
            return cached["data"]
    try:
        return await _gen_daily_prompt()
    except Exception as e:
        cached = store.load_state().get("daily_prompt")
        if cached and cached.get("data"):
            return cached["data"]
        raise HTTPException(502, f"生成失败: {e}")


@app.get("/api/sessions/{sid}/export.md")
def session_export(sid: str):
    """整个话题导出为 Markdown（讨论+灵感+小结+文案）"""
    s = _get_session(sid)
    cats = store.DISCUSSION_MODES
    lines = [f"# {s['title']}", ""]
    ta = s.get("topic_analysis") or {}
    meta = [f"- 模式：{cats.get(s.get('mode'), s.get('mode'))}"]
    if ta.get("category"):
        meta.append(f"- 分类：{store.TOPIC_CATEGORIES.get(ta['category'], ta['category'])}")
    if ta.get("tags"):
        meta.append(f"- 关键词：{'、'.join(ta['tags'])}")
    if isinstance(ta.get("maturity"), int):
        meta.append(f"- 素材成熟度：{ta['maturity']}/100（{ta.get('maturity_hint', '')}）")
    lines += meta + ["", "---", ""]
    if s.get("sparks"):
        lines += ["## 💡 灵感火花", ""]
        lines += [f"- {sp['text']}" + (f"（{sp['note']}）" if sp.get("note") else "")
                  for sp in s["sparks"]]
        lines += ["", "---", ""]
    lines += ["## 💬 讨论记录", ""]
    for m in s.get("messages", []):
        who = "🙋 我" if m["role"] == "user" else "✍️ 搭档"
        lines += [f"**{who}**：", "", m["content"], ""]
    lines += ["---", ""]
    if s.get("summary"):
        lines += ["## 📌 讨论小结", "", s["summary"]["text"], "", "---", ""]
    fmts = prompts.DRAFT_FORMATS
    styles = prompts.DRAFT_STYLES
    for d in s.get("drafts", []):
        label = fmts.get(d["format"], d["format"])
        stl = styles.get(d.get("style", "none"), "")
        suffix = f"（{stl}）" if stl and stl != "自然文风" else ""
        lines += [f"## ✍️ 文案 · {label}{suffix}", "", d["content"], "", "---", ""]
    md = "\n".join(lines)
    from urllib.parse import quote as urlquote
    from fastapi.responses import Response
    return Response(content=md, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition":
                             f"attachment; filename*=UTF-8''{urlquote(s['title'])}.md"})


@app.post("/api/sparks/sync_feishu")
async def sparks_sync_feishu():
    """把灵感库（跨话题）同步到飞书独立文档「✨ 灵感卡片库」，增量追加"""
    if not fs.enabled and not feishu.MOCK:
        raise HTTPException(400, "飞书未配置")
    st = store.load_state()
    lib_doc = st.get("sparks_lib_doc") or {}
    synced_ids = set(st.get("sparks_lib_synced", []))
    try:
        if not lib_doc.get("token"):
            doc = await asyncio.to_thread(fs.create_doc, "✨ 写作共创坊 · 灵感卡片库")
            lib_doc = {"token": doc["token"], "url": doc["url"]}
            st["sparks_lib_doc"] = lib_doc
            await writer.enqueue(lib_doc["token"],
                                 [feishu.text_block([feishu.run("跨话题沉淀的灵感卡片，按收录顺序排列。", italic=True)]),
                                  feishu.divider_block()], label="灵感库文档头")
        all_sp = _all_sparks()
        fresh = [sp for sp in all_sp if sp["id"] not in synced_ids]
        blocks = []
        for sp in fresh:
            blocks.append(feishu.bullet_block(f"{sp['text']} ——《{sp['session_title']}》"))
        # 分批入队（每块一条 bullet，append_blocks 内部再按 20 一组）
        if blocks:
            await writer.enqueue(lib_doc["token"], blocks, label=f"灵感库×{len(blocks)}")
        synced_ids.update(sp["id"] for sp in fresh)
        st["sparks_lib_synced"] = sorted(synced_ids)
        store.save_state(st)
        return {"ok": True, "url": lib_doc.get("url", ""), "synced": len(fresh),
                "total": len(all_sp)}
    except Exception as e:
        store.save_state(st)
        raise HTTPException(502, f"同步失败: {e}")


# ---------------------------------------------------------------------------
# 文案工坊
# ---------------------------------------------------------------------------
class DraftBody(BaseModel):
    format: str = "wechat"
    tone: str = "casual"
    length: str = "medium"
    style: str = "none"
    style_custom: str = ""
    extra: str = ""
    spark_ids: Optional[list] = None   # None=全部灵感；[]=不用；[...]=只勾选的
    ext_sparks: Optional[list] = None  # 从灵感库引入的跨话题灵感文本


def _selected_sparks_text(s: dict, spark_ids, ext_sparks=None) -> str:
    """按勾选过滤灵感（素材精选）；spark_ids 为 None 时用全部；ext_sparks 为跨话题引入"""
    items = [sp["text"] for sp in s.get("sparks", []) if sp.get("text")]
    if spark_ids is not None:
        idset = set(spark_ids)
        items = [sp["text"] for sp in s.get("sparks", [])
                 if sp.get("id") in idset and sp.get("text")]
    for t in (ext_sparks or []):
        t = str(t).strip()[:300]
        if t:
            items.append(f"{t}（引自其它话题）")
    return "\n".join(f"- {t}" for t in items) or "（暂无）"


def _draft_material(s: dict, fmt: str, tone: str, length: str, extra: str,
                    spark_ids=None, style: str = "none", style_custom: str = "",
                    ext_sparks=None) -> list:
    fmt_label = prompts.DRAFT_FORMATS.get(fmt, fmt)
    profile = store.get_profile().get("text", "")
    sparks_part = _selected_sparks_text(s, spark_ids, ext_sparks)
    # 「我的文风」：用户蒸馏的专属文风卡，走 custom 通道注入
    if style == "mine":
        card = store.load_state().get("user_style_card", {}).get("text", "")
        if card:
            style, style_custom = "custom", card
    return [
        {"role": "system", "content": prompts.draft_system_prompt(
            fmt_label,
            prompts.DRAFT_TONES.get(tone, "口语随和"),
            prompts.DRAFT_LENGTHS.get(length, "中等长度"),
            extra,
            profile,
            fmt,
            style,
            style_custom,
        )},
        {"role": "user", "content": (
            f"【讨论记录】\n{_transcript(s)}\n\n【灵感卡片】\n{sparks_part}\n\n"
            + ("【要求】灵感卡片是本次成稿的精选素材，尽量都用上。\n\n" if sparks_part != "（暂无）" else "")
            + "请基于以上材料开始整理成稿。"
        )},
    ]


@app.post("/api/sessions/{sid}/drafts")
async def draft_create(sid: str, body: DraftBody):
    async def gen():
        async with _lock(sid):
            s = _get_session(sid)
            if not s.get("messages"):
                yield _sse({"t": "error", "v": "先聊出一些素材，再来生成文案"})
                return
            fmt_label = prompts.DRAFT_FORMATS.get(body.format, body.format)
            draft = {
                "id": store.new_id(), "format": body.format, "tone": body.tone,
                "length": body.length, "style": body.style, "instruction": body.extra,
                "content": "", "spark_ids": body.spark_ids,
                "created_at": store.now_ts(), "updated_at": store.now_ts(), "history": [],
            }
            full = ""
            try:
                async for piece in llm.chat_stream(
                    _draft_material(s, body.format, body.tone, body.length, body.extra,
                                    body.spark_ids, body.style, body.style_custom,
                                    body.ext_sparks),
                    model=llm.draft_model, max_tokens=4096,
                ):
                    full += piece
                    yield _sse({"t": "delta", "v": piece})
                if not full.strip():
                    raise RuntimeError("空回复")
            except Exception as e:
                log.error("文案生成失败: %s", e)
                yield _sse({"t": "error", "v": str(e)[:300]})
                return
            draft["content"] = full.strip()
            s.setdefault("drafts", []).insert(0, draft)  # 最新在前
            store.save_session(s)
            yield _sse({"t": "done", "draft": draft})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class ReviseBody(BaseModel):
    instruction: str


@app.post("/api/sessions/{sid}/drafts/{did}/revise")
async def draft_revise(sid: str, did: str, body: ReviseBody):
    async def gen():
        async with _lock(sid):
            s = _get_session(sid)
            d = next((x for x in s.get("drafts", []) if x["id"] == did), None)
            if not d:
                yield _sse({"t": "error", "v": "文案不存在"})
                return
            fmt_label = prompts.DRAFT_FORMATS.get(d["format"], d["format"])
            rv_style, rv_custom = d.get("style", "none"), ""
            if rv_style == "mine":
                card = store.load_state().get("user_style_card", {}).get("text", "")
                if card:
                    rv_style, rv_custom = "custom", card
            msgs = [
                {"role": "system", "content": prompts.draft_system_prompt(
                    fmt_label,
                    prompts.DRAFT_TONES.get(d["tone"], "口语随和"),
                    prompts.DRAFT_LENGTHS.get(d["length"], "中等长度"),
                    "",
                    store.get_profile().get("text", ""),
                    d["format"],
                    rv_style,
                    rv_custom,
                )},
                {"role": "user", "content": (
                    "【讨论材料】\n" + _transcript(s, 8000) + "\n【灵感卡片】\n" + _sparks_text(s)
                )},
                {"role": "assistant", "content": d["content"]},
                {"role": "user", "content": f"按以下要求修改这版成稿，输出修改后的完整成稿：\n{body.instruction}"},
            ]
            full = ""
            try:
                async for piece in llm.chat_stream(msgs, model=llm.draft_model, max_tokens=4096):
                    full += piece
                    yield _sse({"t": "delta", "v": piece})
                if not full.strip():
                    raise RuntimeError("空回复")
            except Exception as e:
                log.error("文案修改失败: %s", e)
                yield _sse({"t": "error", "v": str(e)[:300]})
                return
            d["history"].append(d["content"])
            d["content"] = full.strip()
            d["updated_at"] = store.now_ts()
            store.save_session(s)
            yield _sse({"t": "done", "draft": d})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class PolishBody(BaseModel):
    focus: str = ""   # 定向打磨重点（如体检建议）


@app.post("/api/sessions/{sid}/drafts/{did}/polish")
async def draft_polish(sid: str, did: str, body: PolishBody = None):
    """打磨：编辑红笔二遍稿（砍 AI 味、抽象换画面、锻金句、禁升华结尾）；可带定向重点"""
    focus = (body.focus if body else "").strip()
    async def gen():
        async with _lock(sid):
            s = _get_session(sid)
            d = next((x for x in s.get("drafts", []) if x["id"] == did), None)
            if not d:
                yield _sse({"t": "error", "v": "文案不存在"})
                return
            msgs = [
                {"role": "system", "content": prompts.polish_prompt()},
                {"role": "user", "content": (
                    "【讨论材料（事实边界，不得新增）】\n" + _transcript(s, 6000)
                    + "\n【灵感卡片】\n" + _sparks_text(s)
                    + "\n\n【待打磨的原稿】\n" + d["content"]
                    + ("\n\n【本次重点】优先解决以下问题：\n" + focus if focus else "")
                    + "\n\n请输出打磨后的完整修订稿。"
                )},
            ]
            full = ""
            try:
                async for piece in llm.chat_stream(msgs, model=llm.draft_model, max_tokens=4096):
                    full += piece
                    yield _sse({"t": "delta", "v": piece})
                if not full.strip():
                    raise RuntimeError("空回复")
            except Exception as e:
                log.error("打磨失败: %s", e)
                yield _sse({"t": "error", "v": str(e)[:300]})
                return
            d["history"].append(d["content"])
            d["content"] = full.strip()
            d["polished"] = True
            d["updated_at"] = store.now_ts()
            store.save_session(s)
            yield _sse({"t": "done", "draft": d})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/sessions/{sid}/drafts/{did}/titles")
async def draft_titles(sid: str, did: str):
    """标题工坊：一稿十题，覆盖不同手法"""
    async with _lock(sid):
        s = _get_session(sid)
        d = next((x for x in s.get("drafts", []) if x["id"] == did), None)
        if not d:
            raise HTTPException(404, "文案不存在")
        try:
            raw = await llm.chat_once(
                [{"role": "user", "content": prompts.titles_prompt() + d["content"]}],
                model=llm.draft_model, max_tokens=600, temperature=0.9, thinking=False,
            )
            data = _extract_json(raw)
            titles = [
                {"text": str(t.get("text", "")).strip()[:24], "type": str(t.get("type", "")).strip()[:8]}
                for t in (data.get("titles") or []) if isinstance(t, dict) and t.get("text")
            ][:10]
            if not titles:
                raise RuntimeError(f"无法解析: {raw[:80]}")
            d["titles"] = titles
            store.save_session(s)
            return {"titles": titles}
        except Exception as e:
            raise HTTPException(502, f"标题生成失败: {e}")


class ConvertBody(BaseModel):
    format: str


@app.post("/api/sessions/{sid}/drafts/{did}/convert")
async def draft_convert(sid: str, did: str, body: ConvertBody):
    """一稿多发：把已成稿改写成另一种格式（新稿入库）"""
    if body.format not in prompts.DRAFT_FORMATS:
        raise HTTPException(400, "未知格式")
    async def gen():
        async with _lock(sid):
            s = _get_session(sid)
            d = next((x for x in s.get("drafts", []) if x["id"] == did), None)
            if not d:
                yield _sse({"t": "error", "v": "文案不存在"})
                return
            src_label = prompts.DRAFT_FORMATS.get(d["format"], d["format"])
            dst_label = prompts.DRAFT_FORMATS.get(body.format, body.format)
            style_key, style_custom = d.get("style", "none"), ""
            if style_key == "mine":
                card = store.load_state().get("user_style_card", {}).get("text", "")
                if card:
                    style_key, style_custom = "custom", card
            msgs = [
                {"role": "system", "content": prompts.draft_system_prompt(
                    dst_label, prompts.DRAFT_TONES.get(d.get("tone", "casual"), "口语随和"),
                    prompts.DRAFT_LENGTHS.get(d.get("length", "medium"), "中等长度"),
                    "", "", body.format, style_key, style_custom,
                )},
                {"role": "user", "content": (
                    f"【改写任务】\n{prompts.convert_prompt(src_label, dst_label)}\n\n"
                    f"【已成稿原文（{src_label}）】\n{d['content']}"
                )},
            ]
            draft = {
                "id": store.new_id(), "format": body.format, "tone": d.get("tone", "casual"),
                "length": d.get("length", "medium"), "style": d.get("style", "none"),
                "instruction": f"由{src_label}转写", "content": "",
                "converted_from": did,
                "created_at": store.now_ts(), "updated_at": store.now_ts(), "history": [],
            }
            full = ""
            try:
                async for piece in llm.chat_stream(msgs, model=llm.draft_model, max_tokens=4096):
                    full += piece
                    yield _sse({"t": "delta", "v": piece})
                if not full.strip():
                    raise RuntimeError("空回复")
            except Exception as e:
                log.error("转格式失败: %s", e)
                yield _sse({"t": "error", "v": str(e)[:300]})
                return
            draft["content"] = full.strip()
            s.setdefault("drafts", []).insert(0, draft)
            store.save_session(s)
            yield _sse({"t": "done", "draft": draft})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# 写作周报
# ---------------------------------------------------------------------------
def _week_stats_text() -> str:
    week_start = None
    import time as _t
    now = _t.localtime()
    # 本周一 0 点
    import calendar
    ts = _t.mktime((now.tm_year, now.tm_mon, now.tm_mday - now.tm_wday + 1 if now.tm_wday else now.tm_mday - 6, 0, 0, 0, 0, 0, -1))
    sessions = store.list_sessions()
    week = [m for m in sessions if m.get("created_at", 0) >= ts]
    all_sparks = _all_sparks()
    lines = [
        f"本周新建话题 {len(week)} 个（现存共 {len(sessions)} 个）",
        f"本周讨论轮数 {sum(m['message_count'] for m in week) // 2}，产出字数 {sum(m.get('chars', 0) for m in week)}",
        f"累计灵感 {len(all_sparks)} 条；本周新收 {sum(m['spark_count'] for m in week)} 条",
        f"本周成稿 {sum(m['draft_count'] for m in week)} 篇",
    ]
    cats = {}
    for m in sessions:
        if m.get("category"):
            cats[m["category"]] = cats.get(m["category"], 0) + 1
    if cats:
        top = sorted(cats.items(), key=lambda x: -x[1])[:3]
        from store import TOPIC_CATEGORIES
        lines.append("话题分类分布：" + "、".join(f"{TOPIC_CATEGORIES.get(k, k)}×{v}" for k, v in top))
    recent_sparks = [sp["text"] for sp in all_sparks[:6]]
    if recent_sparks:
        lines.append("最近的灵感：\n" + "\n".join(f"- {t}" for t in recent_sparks))
    profile = store.get_profile().get("text", "")
    if profile:
        lines.append(f"写作画像：{profile[:150]}")
    return "\n".join(lines)


async def _gen_weekly_report() -> str:
    text = await llm.chat_once(
        [{"role": "user", "content": prompts.weekly_report_prompt(_week_stats_text())}],
        model=llm.draft_model, max_tokens=1500, temperature=0.7, thinking=False,
    )
    st = store.load_state()
    st["weekly_report"] = {"week": store.today()[:7] + f"-w{_isoweek()}", "text": text.strip(), "ts": store.now_ts()}
    store.save_state(st)
    return text.strip()


def _isoweek() -> str:
    import datetime
    return str(datetime.date.today().isocalendar()[1])


@app.get("/api/weekly_report")
async def weekly_report_get(refresh: str = ""):
    cached = store.load_state().get("weekly_report")
    if refresh != "1" and cached and cached.get("week", "").endswith("-w" + _isoweek()):
        return {"text": cached["text"], "cached": True}
    try:
        text = await _gen_weekly_report()
        return {"text": text, "cached": False}
    except Exception as e:
        if cached:
            return {"text": cached["text"], "cached": True}
        raise HTTPException(502, f"周报生成失败: {e}")


@app.post("/api/weekly_report/feishu")
async def weekly_report_feishu():
    """本周周报写入飞书「📊 写作共创坊 · 周报」文档（按周追加）"""
    if not fs.enabled and not feishu.MOCK:
        raise HTTPException(400, "飞书未配置")
    cached = store.load_state().get("weekly_report")
    text = cached.get("text") if cached else None
    if not text:
        text = await _gen_weekly_report()
    st = store.load_state()
    try:
        doc = st.get("weekly_doc") or {}
        if not doc.get("token"):
            created = await asyncio.to_thread(fs.create_doc, "📊 写作共创坊 · 周报")
            doc = {"token": created["token"], "url": created["url"]}
            st["weekly_doc"] = doc
        blocks = feishu.summary_blocks(f"📈 写作周报 · {store.today_label()}", text)
        await writer.enqueue(doc["token"], blocks, label="周报")
        store.save_state(st)
        return {"ok": True, "url": doc.get("url", "")}
    except Exception as e:
        store.save_state(st)
        raise HTTPException(502, f"写入飞书失败: {e}")


# ---------------------------------------------------------------------------
# 素材导入 / 金句锻造 / 写作目标 / Word 导出
# ---------------------------------------------------------------------------
class ImportBody(BaseModel):
    text: str


@app.post("/api/import")
async def import_material(body: ImportBody):
    """素材导入：粘贴长文 → 金句灵感 + 话题方向"""
    text = body.text.strip()
    if len(text) < 30:
        raise HTTPException(400, "材料太短（至少 30 字）")
    try:
        raw = await llm.chat_once(
            [{"role": "user", "content": prompts.import_prompt() + text[:12000]}],
            model=llm.draft_model, max_tokens=1000, temperature=0.5, thinking=False,
        )
        data = _extract_json(raw)
        sparks = [str(s).strip()[:300] for s in (data.get("sparks") or []) if str(s).strip()][:10]
        topics = []
        for t in (data.get("topics") or [])[:3]:
            if isinstance(t, dict) and t.get("title"):
                topics.append({
                    "title": str(t["title"]).strip()[:20],
                    "hook": str(t.get("hook", "")).strip()[:120],
                })
        if not sparks and not topics:
            raise RuntimeError(f"无法解析: {raw[:80]}")
        return {"gist": str(data.get("gist", "")).strip()[:60], "sparks": sparks, "topics": topics}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"导入解析失败: {e}")


class BatchSparksBody(BaseModel):
    texts: list
    origin: str = "import"


@app.post("/api/sessions/{sid}/sparks/batch")
async def sparks_batch(sid: str, body: BatchSparksBody):
    """批量收灵感（素材导入/金句锻造用），自动跳过重复"""
    async with _lock(sid):
        s = _get_session(sid)
        existing_heads = {sp["text"][:16] for sp in s.get("sparks", [])}
        added = []
        f = s.setdefault("feishu", {})
        for t in body.texts:
            t = str(t).strip()[:300]
            if not t or t[:16] in existing_heads:
                continue
            sp = {"id": store.new_id(), "text": t, "note": "", "origin": body.origin,
                  "ts": store.now_ts()}
            s["sparks"].append(sp)
            existing_heads.add(t[:16])
            added.append(sp)
            if f.get("doc_id") and (fs.enabled and fs.auto_record or MOCK):
                await writer.enqueue(f["doc_id"], [feishu.spark_block(sp["text"])], label="灵感")
                f["sparks_synced"] = f.get("sparks_synced", 0) + 1
        store.save_session(s)
        return {"added": len(added), "sparks": added}


@app.post("/api/sessions/{sid}/forge_quotes")
async def forge_quotes(sid: str):
    """金句锻造坊：从话题材料批量锻造新金句"""
    async with _lock(sid):
        s = _get_session(sid)
        if not s.get("messages"):
            raise HTTPException(400, "还没有讨论内容")
        material = (f"【讨论记录】\n{_transcript(s, 8000)}\n\n【已有灵感】\n{_sparks_text(s)}")
        try:
            raw = await llm.chat_once(
                [{"role": "user", "content": prompts.forge_quotes_prompt() + material}],
                model=llm.draft_model, max_tokens=900, temperature=0.9, thinking=False,
            )
            data = _extract_json(raw)
            quotes = [
                {"text": str(q.get("text", "")).strip()[:60],
                 "technique": str(q.get("technique", "")).strip()[:10]}
                for q in (data.get("quotes") or [])
                if isinstance(q, dict) and q.get("text")
            ][:10]
            if not quotes:
                raise RuntimeError(f"无法解析: {raw[:80]}")
            return {"quotes": quotes}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"锻造失败: {e}")


@app.get("/api/goals")
def goals_get():
    """写作目标：每周成稿 N 篇"""
    st = store.load_state()
    goal = int(st.get("weekly_goal", 3))
    week_key = store.today()
    # 本周成稿数：所有会话里 created_at 在本周一的稿
    import time as _t
    now = _t.localtime()
    week_start = _t.mktime((now.tm_year, now.tm_mon,
                            now.tm_mday - (now.tm_wday - 1 if now.tm_wday else -6), 0, 0, 0, 0, 0, -1))
    done = 0
    for meta in store.list_sessions():
        s = store.load_session(meta["id"])
        if not s:
            continue
        done += sum(1 for d in s.get("drafts", []) if d.get("created_at", 0) >= week_start)
    return {"weekly_goal": goal, "done": done, "week": week_key}


class GoalBody(BaseModel):
    weekly_goal: int


@app.post("/api/goals")
def goals_set(body: GoalBody):
    goal = max(1, min(21, int(body.weekly_goal)))
    st = store.load_state()
    st["weekly_goal"] = goal
    store.save_state(st)
    return {"weekly_goal": goal}


@app.get("/api/sessions/{sid}/export.docx")
def session_export_docx(sid: str):
    """整个话题导出 Word 文档"""
    s = _get_session(sid)
    try:
        from docx import Document
        from docx.shared import Pt, RGBColor
        from urllib.parse import quote as urlquote
    except ImportError:
        raise HTTPException(500, "缺少 python-docx 依赖（pip install python-docx）")

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Microsoft YaHei"
    style.font.size = Pt(11)

    doc.add_heading(s["title"], 0)
    cats = store.DISCUSSION_MODES
    ta = s.get("topic_analysis") or {}
    meta = f"模式：{cats.get(s.get('mode'), s.get('mode'))}"
    if ta.get("category"):
        meta += f" · 分类：{store.TOPIC_CATEGORIES.get(ta['category'], ta['category'])}"
    if isinstance(ta.get("maturity"), int):
        meta += f" · 成熟度：{ta['maturity']}/100"
    p = doc.add_paragraph()
    run = p.add_run(meta)
    run.font.color.rgb = RGBColor(0x88, 0x80, 0x74)
    run.font.size = Pt(9)

    if s.get("sparks"):
        doc.add_heading("💡 灵感火花", 1)
        for sp in s["sparks"]:
            doc.add_paragraph(sp["text"] + (f"（{sp['note']}）" if sp.get("note") else ""),
                              style="List Bullet")

    doc.add_heading("💬 讨论记录", 1)
    for m in s.get("messages", []):
        who = "🙋 我" if m["role"] == "user" else "✍️ 搭档"
        hp = doc.add_paragraph()
        hr = hp.add_run(who)
        hr.bold = True
        for para in m["content"].split("\n"):
            if para.strip():
                doc.add_paragraph(para.strip())

    if s.get("summary"):
        doc.add_heading("📌 讨论小结", 1)
        for para in s["summary"]["text"].split("\n"):
            if para.strip():
                doc.add_paragraph(para.strip())

    fmts = prompts.DRAFT_FORMATS
    styles_map = prompts.DRAFT_STYLES
    for d in s.get("drafts", []):
        label = fmts.get(d["format"], d["format"])
        stl = styles_map.get(d.get("style", "none"), "")
        suffix = f"（{stl}）" if stl and stl != "自然文风" else ""
        doc.add_heading(f"✍️ 文案 · {label}{suffix}", 1)
        for para in d["content"].split("\n"):
            t = para.strip()
            if not t:
                continue
            if t.startswith("# "):
                doc.add_heading(t[2:].strip(), 2)
            elif t.startswith("## "):
                doc.add_heading(t[3:].strip(), 3)
            else:
                doc.add_paragraph(t)

    import io
    buf = io.BytesIO()
    doc.save(buf)
    from fastapi.responses import Response
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition":
                 f"attachment; filename*=UTF-8''{urlquote(s['title'])}.docx"},
    )


# ---------------------------------------------------------------------------
# 三稿竞标 / 成稿体检
# ---------------------------------------------------------------------------
class ContestBody(BaseModel):
    format: str = "wechat"
    tone: str = "casual"
    length: str = "medium"
    style: str = "none"
    style_custom: str = ""
    extra: str = ""
    spark_ids: Optional[list] = None
    ext_sparks: Optional[list] = None


@app.post("/api/sessions/{sid}/drafts/contest")
async def draft_contest(sid: str, body: ContestBody):
    """三稿竞标：同一材料按三种角度各出一稿，择优留用"""
    async def gen():
        async with _lock(sid):
            s = _get_session(sid)
            if not s.get("messages"):
                yield _sse({"t": "error", "v": "先聊出一些素材，再来竞标"})
                return
            contest_id = store.new_id()
            drafts = []
            for i, (label, hint) in enumerate(prompts.CONTEST_ANGLES):
                yield _sse({"t": "start", "index": i, "label": label})
                extra_i = (body.extra + "\n" if body.extra.strip() else "") + \
                    f"【本稿角度】{hint}（这是同题竞标的第 {i + 1} 稿，与其它稿的角度必须明显不同）"
                draft = {
                    "id": store.new_id(), "format": body.format, "tone": body.tone,
                    "length": body.length, "style": body.style, "instruction": body.extra,
                    "content": "", "spark_ids": body.spark_ids,
                    "contest": {"id": contest_id, "index": i, "label": label},
                    "created_at": store.now_ts(), "updated_at": store.now_ts(), "history": [],
                }
                full = ""
                try:
                    async for piece in llm.chat_stream(
                        _draft_material(s, body.format, body.tone, body.length, extra_i,
                                        body.spark_ids, body.style, body.style_custom,
                                        body.ext_sparks),
                        model=llm.draft_model, max_tokens=4096,
                    ):
                        full += piece
                        yield _sse({"t": "delta", "v": piece, "index": i})
                    if not full.strip():
                        raise RuntimeError("空回复")
                except Exception as e:
                    log.error("竞标第%d稿失败: %s", i + 1, e)
                    yield _sse({"t": "error", "v": f"第 {i + 1} 稿（{label}）失败: {str(e)[:150]}",
                                "index": i})
                    continue
                draft["content"] = full.strip()
                s.setdefault("drafts", []).insert(0, draft)
                drafts.append(draft)
                yield _sse({"t": "one_done", "index": i, "draft": draft})
            store.save_session(s)
            yield _sse({"t": "done", "drafts": drafts})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/sessions/{sid}/drafts/{did}/checkup")
async def draft_checkup(sid: str, did: str):
    """成稿体检：四维严苛评分 + 问题定位 + 修改建议"""
    async with _lock(sid):
        s = _get_session(sid)
        d = next((x for x in s.get("drafts", []) if x["id"] == did), None)
        if not d:
            raise HTTPException(404, "文案不存在")
        try:
            raw = await llm.chat_once(
                [{"role": "user", "content": prompts.checkup_prompt() + d["content"]}],
                model=llm.draft_model, max_tokens=800, temperature=0.3, thinking=False,
            )
            data = _extract_json(raw)
            if not data:
                raise RuntimeError(f"无法解析: {raw[:80]}")

            def _score(k):
                try:
                    return max(0, min(100, int(data.get(k, 0))))
                except (TypeError, ValueError):
                    return 0

            checkup = {
                "ai_flavor": _score("ai_flavor"), "concreteness": _score("concreteness"),
                "rhythm": _score("rhythm"), "quote_density": _score("quote_density"),
                "overall": _score("overall"),
                "issues": [str(x).strip()[:120] for x in (data.get("issues") or []) if str(x).strip()][:4],
                "suggestions": [str(x).strip()[:120] for x in (data.get("suggestions") or []) if str(x).strip()][:4],
                "ts": store.now_ts(),
            }
            d["checkup"] = checkup
            store.save_session(s)
            return {"checkup": checkup}
        except Exception as e:
            raise HTTPException(502, f"体检失败: {e}")


# ---------------------------------------------------------------------------
# 我的文风 / 重答 / 写作看板
# ---------------------------------------------------------------------------
class StyleLearnBody(BaseModel):
    text: str


@app.get("/api/style_learn")
def style_learn_get():
    card = store.load_state().get("user_style_card") or {}
    return {"card": card.get("text", ""), "has": bool(card.get("text")), "ts": card.get("ts", 0)}


@app.post("/api/style_learn")
async def style_learn(body: StyleLearnBody):
    """文风采集：蒸馏作者专属文风卡"""
    text = body.text.strip()
    if len(text) < 100:
        raise HTTPException(400, "样本太短（至少 100 字，多多益善）")
    try:
        card = await llm.chat_once(
            [{"role": "user", "content": prompts.style_learn_prompt() + text[:16000]}],
            model=llm.draft_model, max_tokens=400, temperature=0.3, thinking=False,
        )
        if len(card.strip()) < 30:
            raise RuntimeError("蒸馏结果过短")
        st = store.load_state()
        st["user_style_card"] = {"text": card.strip()[:600], "ts": store.now_ts()}
        store.save_state(st)
        return {"card": card.strip()[:600]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"文风蒸馏失败: {e}")


@app.delete("/api/style_learn")
def style_learn_clear():
    st = store.load_state()
    st.pop("user_style_card", None)
    store.save_state(st)
    return {"ok": True}


@app.post("/api/sessions/{sid}/regen")
async def chat_regen(sid: str):
    """重答：丢弃最后一条搭档回复，重新生成"""
    async def gen():
        async with _lock(sid):
            s = _get_session(sid)
            if not s.get("messages") or s["messages"][-1]["role"] != "assistant":
                yield _sse({"t": "error", "v": "最后一条不是搭档回复，无法重答"})
                return
            s["messages"].pop()
            store.save_session(s)
            full, ok = "", False
            try:
                async for piece in llm.chat_stream(_chat_messages(s), model=llm.model):
                    full += piece
                    yield _sse({"t": "delta", "v": piece})
                ok = bool(full.strip())
                if not ok:
                    raise RuntimeError("空回复")
            except Exception as e:
                yield _sse({"t": "error", "v": str(e)[:300]})
                return
            s = _get_session(sid)
            s["messages"].append({"role": "assistant", "content": full, "ts": store.now_ts()})
            store.save_session(s)
            task = asyncio.create_task(_sync_turn_to_feishu(sid))
            _bg_tasks.add(task)
            task.add_done_callback(_bg_tasks.discard)
            yield _sse({"t": "done"})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/dashboard")
def dashboard():
    """写作看板数据：分类分布 / 每周成稿趋势 / 灵感与产出统计"""
    import time as _t
    import datetime
    sessions = store.list_sessions()
    cats = {}
    for m in sessions:
        if m.get("category"):
            cats[m["category"]] = cats.get(m["category"], 0) + 1
    # 近 6 周成稿趋势
    weeks = []
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    for i in range(5, -1, -1):
        ws = _t.mktime((monday - datetime.timedelta(weeks=i)).timetuple())
        we = ws + 7 * 86400
        drafts = sparks = 0
        for m in sessions:
            s = store.load_session(m["id"])
            if not s:
                continue
            drafts += sum(1 for d in s.get("drafts", []) if ws <= d.get("created_at", 0) < we)
            sparks += sum(1 for sp in s.get("sparks", []) if ws <= sp.get("ts", 0) < we)
        weeks.append({
            "label": f"{(monday - datetime.timedelta(weeks=i)).month}/{(monday - datetime.timedelta(weeks=i)).day}",
            "drafts": drafts, "sparks": sparks,
        })
    total_drafts = sum(m.get("draft_count", 0) for m in sessions)
    return {
        "categories": [{"key": k, "count": v} for k, v in cats.items()],
        "weeks": weeks,
        "totals": {
            "sessions": len(sessions),
            "sparks": sum(m.get("spark_count", 0) for m in sessions),
            "drafts": total_drafts,
            "chars": sum(m.get("chars", 0) for m in sessions),
        },
    }


# ---------------------------------------------------------------------------
# 观点擂台 / 选题库 / 版本历史 / 智能配图 / 发布包
# ---------------------------------------------------------------------------
class ArenaBody(BaseModel):
    rounds: int = 3


@app.post("/api/sessions/{sid}/arena")
async def arena(sid: str, body: ArenaBody):
    """观点擂台：双人格辩论（正方毒舌主编 vs 反方苏格拉底），流式写入消息流"""
    async def gen():
        async with _lock(sid):
            s = _get_session(sid)
            if not s.get("messages"):
                yield _sse({"t": "error", "v": "先聊出核心观点，再开擂台"})
                return
            topic = s.get("topic") or s["title"]
            rounds = max(1, min(body.rounds or 3, 5))
            opener = {"role": "assistant", "content": f"⚔️ 观点擂台开场：就「{topic}」",
                      "arena": {"side": "host", "round": 0}, "ts": store.now_ts()}
            s["messages"].append(opener)
            store.save_session(s)
            yield _sse({"t": "msg", "message": opener})
            for r in range(1, rounds + 1):
                for side in ("pro", "con"):
                    label, _persona = prompts.ARENA_SIDES[side]
                    # 最近的擂台发言作为对手上一轮
                    recent = [m["content"] for m in s["messages"][-6:]]
                    msgs = [
                        {"role": "system", "content": prompts.arena_system_prompt(side, topic)},
                        {"role": "user", "content": (
                            "【讨论材料】\n" + _transcript(s, 4000)
                            + "\n\n【台上最近的发言（最后一条是你的对手】\n"
                            + "\n---\n".join(recent[-3:])
                            + f"\n\n这是第 {r} 轮，轮到你（{label}）发言。"
                        )},
                    ]
                    yield _sse({"t": "start", "side": side, "round": r, "label": label})
                    full = ""
                    try:
                        async for piece in llm.chat_stream(msgs, model=llm.model,
                                                           temperature=0.9, max_tokens=400):
                            full += piece
                            yield _sse({"t": "delta", "v": piece, "side": side})
                    except Exception as e:
                        yield _sse({"t": "error", "v": f"{label}发言失败: {str(e)[:120]}"})
                        continue
                    msg = {"role": "assistant", "content": full.strip(),
                           "arena": {"side": side, "round": r}, "ts": store.now_ts()}
                    s["messages"].append(msg)
                    store.save_session(s)
                    yield _sse({"t": "msg", "message": msg})
            # 收尾：主持人总结
            closing = {"role": "assistant",
                       "content": "⚔️ 本轮擂台结束。想支持哪一方，直接插话开聊；再点「擂台」可加赛。",
                       "arena": {"side": "host", "round": rounds + 1}, "ts": store.now_ts()}
            s["messages"].append(closing)
            store.save_session(s)
            yield _sse({"t": "msg", "message": closing, "done": True})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---- 选题库 --------------------------------------------------------------
class BacklogBody(BaseModel):
    title: str
    note: str = ""
    from_: str = ""


@app.get("/api/backlog")
def backlog_list():
    return {"items": store.load_state().get("topic_backlog", [])}


@app.post("/api/backlog")
def backlog_add(body: BacklogBody):
    st = store.load_state()
    item = {"id": store.new_id(), "title": body.title.strip()[:40],
            "note": body.note.strip()[:100], "from": body.from_, "ts": store.now_ts()}
    if not item["title"]:
        raise HTTPException(400, "选题不能为空")
    st.setdefault("topic_backlog", []).append(item)
    store.save_state(st)
    return item


@app.delete("/api/backlog/{bid}")
def backlog_delete(bid: str):
    st = store.load_state()
    before = len(st.get("topic_backlog", []))
    st["topic_backlog"] = [x for x in st.get("topic_backlog", []) if x["id"] != bid]
    if len(st["topic_backlog"]) == before:
        raise HTTPException(404, "选题不存在")
    store.save_state(st)
    return {"ok": True}


# ---- 版本历史（任何内容修改自动入历史） ----------------------------------
class DraftPatch(BaseModel):
    content: str


@app.patch("/api/sessions/{sid}/drafts/{did}")
def draft_save_edit(sid: str, did: str, body: DraftPatch):
    s = _get_session(sid)
    d = next((x for x in s.get("drafts", []) if x["id"] == did), None)
    if not d:
        raise HTTPException(404, "文案不存在")
    if body.content != d["content"]:
        d["history"].append(d["content"])
        d["history"] = [h for i, h in enumerate(d["history"]) if h != body.content or i == len(d["history"]) - 1]
    d["content"] = body.content
    d["updated_at"] = store.now_ts()
    store.save_session(s)
    return d


# ---- 智能配图（无 key 出提示词；有 APIMart key 直接出图） -----------------
async def _gen_cover_image(did: str, prompt_cn: str):
    """后台生成封面图：提交 APIMart 任务 → 轮询 → 下载到 data/images/"""
    import urllib.request as _u
    key = (CONFIG.get("image", {}).get("api_key") or "").strip()
    if not key:
        return
    try:
        base = (CONFIG.get("image", {}).get("base_url") or "https://api.apimart.ai/v1").rstrip("/")
        import os
        os.makedirs(store.DATA_DIR + "/images", exist_ok=True)
        req = _u.Request(base + "/images/generations",
                         data=json.dumps({"model": "gpt-image-2", "prompt": prompt_cn,
                                          "size": "16:9", "n": 1}).encode(),
                         headers={"Authorization": "Bearer " + key,
                                  "Content-Type": "application/json; charset=utf-8"},
                         method="POST")
        with _u.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode())
        task_id = (data.get("data") or [{}])[0].get("task_id") or data.get("task_id")
        if not task_id:
            raise RuntimeError(str(data)[:120])
        import time as _t
        for _ in range(120):  # 最多等 6 分钟
            await asyncio.sleep(3)
            req = _u.Request(base + f"/tasks/{task_id}",
                             headers={"Authorization": "Bearer " + key}, method="GET")
            with _u.urlopen(req, timeout=30) as r:
                t = json.loads(r.read().decode())
            status = (t.get("data") or t.get("status") or "")
            if isinstance(status, dict):
                status = status.get("status", "")
            if str(status).lower() in ("succeeded", "success", "completed"):
                url = ((t.get("data") or {}).get("url") if isinstance(t.get("data"), dict)
                       else (t.get("data") or [{}])[0].get("url") if isinstance(t.get("data"), list)
                       else t.get("url"))
                if not url:
                    raise RuntimeError("完成但拿不到图片 URL")
                fn = f"cover_{did[:8]}_{int(store.now_ts())}.png"
                path = store.DATA_DIR + "/images/" + fn
                dreq = _u.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                with _u.urlopen(dreq, timeout=120) as r2, open(path, "wb") as f:
                    f.write(r2.read())
                # 写回稿卡
                for meta in store.list_sessions():
                    s2 = store.load_session(meta["id"])
                    if not s2:
                        continue
                    for dd in s2.get("drafts", []):
                        if dd["id"] == did:
                            dd["cover_image"] = "/data/images/" + fn
                            store.save_session(s2)
                            log.info("封面图已生成: %s", fn)
                            return
                return
            if str(status).lower() in ("failed", "error", "cancelled"):
                raise RuntimeError(f"任务失败: {status}")
        raise RuntimeError("轮询超时")
    except Exception as e:
        log.warning("封面图生成失败: %s", e)


@app.post("/api/sessions/{sid}/drafts/{did}/cover")
async def draft_cover(sid: str, did: str):
    """配图：LLM 出封面提示词；配置了 image key 则后台出图"""
    async with _lock(sid):
        s = _get_session(sid)
        d = next((x for x in s.get("drafts", []) if x["id"] == did), None)
        if not d:
            raise HTTPException(404, "文案不存在")
        try:
            raw = await llm.chat_once(
                [{"role": "user", "content": prompts.cover_prompt() + d["content"][:3000]}],
                model=llm.fast_model, max_tokens=300, temperature=0.8, thinking=False,
            )
            data = _extract_json(raw)
            cp = str(data.get("cover_prompt", "")).strip()
            if not cp:
                raise RuntimeError(f"无法解析: {raw[:80]}")
            d["cover_prompt"] = cp
            d["cover_style"] = str(data.get("style_note", "")).strip()[:20]
            d.pop("cover_image", None)
            store.save_session(s)
        except Exception as e:
            raise HTTPException(502, f"配图提示词生成失败: {e}")
    has_key = bool((CONFIG.get("image", {}).get("api_key") or "").strip())
    if has_key:
        task = asyncio.create_task(_gen_cover_image(did, cp))
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
    return {"cover_prompt": cp, "style_note": d.get("cover_style", ""),
            "image_pending": has_key, "image": None}


# ---- 发布包 --------------------------------------------------------------
@app.post("/api/sessions/{sid}/drafts/{did}/publish_pack")
async def draft_publish_pack(sid: str, did: str):
    async with _lock(sid):
        s = _get_session(sid)
        d = next((x for x in s.get("drafts", []) if x["id"] == did), None)
        if not d:
            raise HTTPException(404, "文案不存在")
        try:
            raw = await llm.chat_once(
                [{"role": "user", "content": prompts.publish_pack_prompt() + d["content"][:4000]}],
                model=llm.draft_model, max_tokens=700, temperature=0.7, thinking=False,
            )
            data = _extract_json(raw)
            pack = {
                "titles": [str(t).strip()[:24] for t in (data.get("title_candidates") or []) if str(t).strip()][:3],
                "summary": str(data.get("summary", "")).strip()[:80],
                "tags": [str(t).strip().lstrip("#")[:12] for t in (data.get("tags") or []) if str(t).strip()][:5],
                "wechat_tip": str(data.get("wechat_tip", "")).strip()[:60],
                "xhs_tip": str(data.get("xhs_tip", "")).strip()[:60],
            }
            if not pack["titles"]:
                raise RuntimeError(f"无法解析: {raw[:80]}")
            d["publish_pack"] = pack
            store.save_session(s)
            return {"pack": pack}
        except Exception as e:
            raise HTTPException(502, f"发布包生成失败: {e}")


class DraftPatch(BaseModel):
    content: str


@app.delete("/api/sessions/{sid}/drafts/{did}")
def draft_delete(sid: str, did: str):
    s = _get_session(sid)
    before = len(s.get("drafts", []))
    s["drafts"] = [x for x in s.get("drafts", []) if x["id"] != did]
    if len(s["drafts"]) == before:
        raise HTTPException(404, "文案不存在")
    store.save_session(s)
    return {"ok": True}


class DraftToFeishu(BaseModel):
    separate: bool = False   # True = 单独建一篇文档


@app.post("/api/sessions/{sid}/drafts/{did}/feishu")
async def draft_to_feishu(sid: str, did: str, body: DraftToFeishu):
    async with _lock(sid):
        s = _get_session(sid)
        d = next((x for x in s.get("drafts", []) if x["id"] == did), None)
        if not d:
            raise HTTPException(404, "文案不存在")
        if not fs.enabled and not MOCK:
            raise HTTPException(400, "飞书未配置（设置里填 app_id / app_secret）")
        fmt_label = prompts.DRAFT_FORMATS.get(d["format"], "文案")
        try:
            if body.separate:
                doc = await asyncio.to_thread(
                    fs.create_doc, f"{s['title']} · {fmt_label}"
                )
                await writer.enqueue(doc["token"], feishu.draft_blocks(fmt_label, d["content"]),
                                     label="文案")
                url = doc["url"]
            else:
                f = s.setdefault("feishu", {})
                if not f.get("doc_id"):
                    await _sync_turn_locked(sid)
                    s = _get_session(sid)
                    f = s["feishu"]
                await writer.enqueue(f["doc_id"], feishu.draft_blocks(fmt_label, d["content"]),
                                     label="文案")
                url = f.get("url", "")
            store.save_session(s)
            return {"ok": True, "url": url}
        except Exception as e:
            raise HTTPException(502, f"写入飞书失败: {e}")


# ---------------------------------------------------------------------------
# 飞书状态 / 自检 / 手动同步
# ---------------------------------------------------------------------------
@app.get("/api/feishu/status")
def feishu_status(sid: str = ""):
    info = {
        "enabled": fs.enabled,
        "auto_record": fs.auto_record,
        "backlog": writer.backlog,
        "last_error": writer.last_error,
        "doc": None,
    }
    if sid:
        s = store.load_session(sid)
        if s and s.get("feishu", {}).get("doc_id"):
            info["doc"] = {
                "id": s["feishu"]["doc_id"],
                "url": s["feishu"].get("url", ""),
                "turns_synced": s["feishu"].get("turns_synced", 0),
                "total_turns": len(s.get("messages", [])) // 2,
                "error": s["feishu"].get("error", ""),
            }
    return info


@app.post("/api/feishu/check")
async def feishu_check():
    return await asyncio.to_thread(fs.check)


class FeishuToggle(BaseModel):
    auto_record: Optional[bool] = None
    auto_weekly: Optional[bool] = None


@app.post("/api/feishu/toggle")
async def feishu_toggle(body: FeishuToggle):
    if body.auto_record is not None:
        CONFIG["feishu"]["auto_record"] = body.auto_record
    if body.auto_weekly is not None:
        CONFIG["feishu"]["auto_weekly"] = body.auto_weekly
        st = store.load_state()
        if not body.auto_weekly:
            st.pop("auto_weekly", None)  # 关掉时清标记，重开可立即生效
        else:
            st["auto_weekly"] = {"week": "", "ts": store.now_ts()}
        store.save_state(st)
    save_config(CONFIG)
    fs.update(CONFIG["feishu"])
    return {"ok": True, "auto_record": fs.auto_record,
            "auto_weekly": bool(CONFIG["feishu"].get("auto_weekly"))}


@app.post("/api/sessions/{sid}/feishu/resync")
async def feishu_resync(sid: str):
    async with _lock(sid):
        s = _get_session(sid)
        f = s.setdefault("feishu", {})
        if not fs.enabled and not MOCK:
            raise HTTPException(400, "飞书未配置")
        if not f.get("doc_id"):
            await _sync_turn_locked(sid)
            s = _get_session(sid)
            return {"ok": True, "url": s["feishu"].get("url", "")}
        # 已有文档：补写落后的轮次/灵感
        await _sync_turn_locked(sid)
        s = _get_session(sid)
        return {"ok": True, "url": s["feishu"].get("url", "")}


# ---------------------------------------------------------------------------
# 静态资源 & 启动
# ---------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
_IMAGES_DIR = BASE_DIR / "data" / "images"
_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/data/images", StaticFiles(directory=str(_IMAGES_DIR)), name="images")


if __name__ == "__main__":
    import socket
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="自检：LLM + 飞书连通性")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()

    if args.check:
        async def _check():
            print("== LLM ==")
            if MOCK:
                print("  MOCK 模式，跳过")
            elif not llm.ready:
                print("  ✗ 未配置 api_key（config.json -> llm.api_key）")
            else:
                try:
                    r = await llm.chat_once([{"role": "user", "content": "回复：连通"}],
                                            model=llm.fast_model, max_tokens=16, thinking=False)
                    print(f"  ✓ 模型 {llm.model} 回复：{r[:30]}")
                except Exception as e:
                    print(f"  ✗ {e}")
            print("== 飞书 ==")
            res = await asyncio.to_thread(fs.check)
            print(f"  {'✓' if res['ok'] else '✗'} {res['msg']}")
            await llm.close()
        asyncio.run(_check())
        sys.exit(0)

    host = "0.0.0.0" if CONFIG.get("server", {}).get("lan") else "127.0.0.1"
    if host == "0.0.0.0":
        token = CONFIG.get("server", {}).get("access_token") or ""
        if not token:
            import secrets
            token = secrets.token_hex(4)
            CONFIG["server"]["access_token"] = token
            save_config(CONFIG)
        print(f"局域网已开启，访问令牌：{token}")
    print(f"✍️ 写作共创坊 → http://{'127.0.0.1' if host == '127.0.0.1' else socket.gethostbyname(socket.gethostname())}:{args.port}")
    uvicorn.run(app, host=host, port=args.port, log_level="warning")
