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
    sys_prompt = prompts.partner_system_prompt(s.get("topic") or s["title"], s.get("mode", "free"), sparks)
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
    yield
    await llm.close()
    fs.http.close()


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
            "enabled": fs.enabled,
        },
        "server": {"lan": bool(CONFIG.get("server", {}).get("lan"))},
        "modes": store.DISCUSSION_MODES,
        "draft_formats": prompts.DRAFT_FORMATS,
        "draft_tones": prompts.DRAFT_TONES,
        "draft_lengths": prompts.DRAFT_LENGTHS,
    }


class ConfigUpdate(BaseModel):
    llm: Optional[dict] = None
    feishu: Optional[dict] = None


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


@app.post("/api/sessions")
def session_create(body: SessionCreate):
    s = store.new_session(body.title, body.mode)
    return s


@app.get("/api/sessions/{sid}")
def session_get(sid: str):
    return _get_session(sid)


class SessionPatch(BaseModel):
    title: Optional[str] = None
    mode: Optional[str] = None


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
        store.save_session(s)
        return s


@app.delete("/api/sessions/{sid}")
def session_delete(sid: str):
    if not store.delete_session(sid):
        raise HTTPException(404, "会话不存在")
    return {"ok": True}


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
                model=llm.draft_model, max_tokens=2048, thinking=True,
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
# 文案工坊
# ---------------------------------------------------------------------------
class DraftBody(BaseModel):
    format: str = "wechat"
    tone: str = "casual"
    length: str = "medium"
    extra: str = ""


def _draft_material(s: dict, fmt: str, tone: str, length: str, extra: str) -> list:
    fmt_label = prompts.DRAFT_FORMATS.get(fmt, fmt)
    return [
        {"role": "system", "content": prompts.draft_system_prompt(
            fmt_label,
            prompts.DRAFT_TONES.get(tone, "口语随和"),
            prompts.DRAFT_LENGTHS.get(length, "中等长度"),
            extra,
        )},
        {"role": "user", "content": (
            f"【讨论记录】\n{_transcript(s)}\n\n【灵感卡片】\n{_sparks_text(s)}\n\n"
            "请基于以上材料开始整理成稿。"
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
                "length": body.length, "instruction": body.extra, "content": "",
                "created_at": store.now_ts(), "updated_at": store.now_ts(), "history": [],
            }
            full = ""
            try:
                async for piece in llm.chat_stream(
                    _draft_material(s, body.format, body.tone, body.length, body.extra),
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
            msgs = [
                {"role": "system", "content": prompts.draft_system_prompt(
                    fmt_label,
                    prompts.DRAFT_TONES.get(d["tone"], "口语随和"),
                    prompts.DRAFT_LENGTHS.get(d["length"], "中等长度"),
                    "",
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


class DraftPatch(BaseModel):
    content: str


@app.patch("/api/sessions/{sid}/drafts/{did}")
def draft_save_edit(sid: str, did: str, body: DraftPatch):
    s = _get_session(sid)
    d = next((x for x in s.get("drafts", []) if x["id"] == did), None)
    if not d:
        raise HTTPException(404, "文案不存在")
    d["content"] = body.content
    d["updated_at"] = store.now_ts()
    store.save_session(s)
    return d


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
    auto_record: bool


@app.post("/api/feishu/toggle")
async def feishu_toggle(body: FeishuToggle):
    CONFIG["feishu"]["auto_record"] = body.auto_record
    save_config(CONFIG)
    fs.update(CONFIG["feishu"])
    return {"ok": True, "auto_record": body.auto_record}


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
