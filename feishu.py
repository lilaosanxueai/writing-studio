"""飞书云文档客户端

职责：
  - tenant_access_token 自动刷新
  - 根目录（或用户配置的文件夹）下建「✍️ 写作共创坊」文件夹
  - 每个话题一篇文档；讨论轮次 / 灵感 / 小结 / 文案全部追加写入
  - 异步写入队列：聊天主流程只入队，飞书慢/挂都不影响对话；失败落盘待恢复
  - MOCK 模式：WS_MOCK=1 时不真正调飞书，方便离线联调

块协议（飞书 Docx Block）：
  text=2, heading1..4=3..6, bullet=12, divider=22
"""
import asyncio
import json
import logging
import os
import re
import time

import httpx

import store

log = logging.getLogger("studio.feishu")

BASE = "https://open.feishu.cn/open-apis"
MOCK = os.environ.get("WS_MOCK") == "1"
FOLDER_NAME = "✍️ 写作共创坊"
CHUNK = 20  # 单次追加块数上限


# ---------------------------------------------------------------------------
# 块构造
# ---------------------------------------------------------------------------
def _style(bold=False, italic=False):
    s = {}
    if bold:
        s["bold"] = True
    if italic:
        s["italic"] = True
    return s


def run(content, bold=False, italic=False):
    return {"content": content, "text_element_style": _style(bold, italic)}


def text_block(runs):
    return {"block_type": 2, "text": {"elements": [{"text_run": r} for r in runs], "style": {}}}


def heading_block(text, level=2):
    level = max(1, min(4, level))
    return {
        "block_type": 2 + level,
        f"heading{level}": {"elements": [{"text_run": run(text)}], "style": {}},
    }


def bullet_block(text):
    return {"block_type": 12, "bullet": {"elements": [{"text_run": run(text)}], "style": {}}}


def divider_block():
    return {"block_type": 22, "divider": {}}


def _split_paras(text, limit=1800):
    text = (text or "").strip()
    if not text:
        return ["（无内容）"]
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    out = []
    for p in paras:
        while len(p) > limit:
            out.append(p[:limit])
            p = p[limit:]
        out.append(p)
    return out or ["（无内容）"]


def session_head_blocks(title, mode_label, date_str):
    return [
        heading_block(f"{title}", 2),
        text_block([run(f"讨论模式：{mode_label} · {date_str} · 由「写作共创坊」记录", italic=True)]),
        divider_block(),
    ]


def turn_blocks(turn_no, user_text, ai_text):
    """一轮讨论 → 文档块"""
    blocks = [text_block([run(f"—— 第 {turn_no} 轮", bold=True)])]
    blocks.append(text_block([run("🙋 我：", bold=True)]))
    for para in _split_paras(user_text):
        blocks.append(text_block([run(para)]))
    blocks.append(text_block([run("✍️ 搭档：", bold=True)]))
    for para in _split_paras(ai_text):
        blocks.append(text_block([run(para)]))
    return blocks


def sparks_head_blocks():
    return [heading_block("💡 灵感火花", 2)]


def spark_block(text):
    return bullet_block(text)


def summary_blocks(summary, date_str):
    blocks = [heading_block(f"📌 讨论小结 · {date_str}", 2)]
    for para in _split_paras(summary):
        blocks.append(text_block([run(para)]))
    blocks.append(divider_block())
    return blocks


def draft_blocks(format_label, content):
    blocks = [heading_block(f"✍️ 文案 · {format_label}", 2)]
    for para in _split_paras(content):
        blocks.append(text_block([run(para)]))
    blocks.append(divider_block())
    return blocks


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------
class FeishuError(RuntimeError):
    pass


class Feishu:
    def __init__(self, cfg: dict):
        self.app_id = (cfg.get("app_id") or "").strip()
        self.app_secret = (cfg.get("app_secret") or "").strip()
        self.folder_token_cfg = (cfg.get("folder_token") or "").strip()
        self.auto_share = bool(cfg.get("auto_share_tenant"))
        self.auto_record = bool(cfg.get("auto_record", True))
        self._token = None
        self._token_exp = 0.0
        self.http = httpx.Client(timeout=30)
        self._domain = ""

    @property
    def enabled(self) -> bool:
        return bool(self.app_id and self.app_secret)

    def update(self, cfg: dict):
        self.__init__(cfg)

    def _get_token(self):
        if MOCK:
            return "mock-token"
        if self._token and time.time() < self._token_exp - 300:
            return self._token
        r = self.http.post(
            f"{BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
        )
        data = r.json()
        if data.get("code") != 0:
            raise FeishuError(f"获取 token 失败: {data.get('msg')} (app_id={self.app_id[:10]}...)")
        self._token = data["tenant_access_token"]
        self._token_exp = time.time() + data.get("expire", 7200)
        return self._token

    def _api(self, method, path, body=None, params=None):
        if MOCK:
            return {"mock": True}
        r = self.http.request(
            method, f"{BASE}{path}", json=body, params=params,
            headers={"Authorization": "Bearer " + self._get_token()},
        )
        data = r.json()
        if data.get("code") != 0:
            raise FeishuError(f"[{method} {path}] code={data.get('code')} {data.get('msg')}")
        return data.get("data") or {}

    # ---- 文件夹 / 文档 ----------------------------------------------------
    def domain(self) -> str:
        if MOCK:
            return "example.com"
        if self._domain:
            return self._domain
        st = store.load_state()
        if st.get("domain"):
            self._domain = st["domain"]
            return self._domain
        try:
            root = self._api("GET", "/drive/explorer/v2/root_folder/meta").get("token", "root")
            files = self._api(
                "GET", "/drive/v1/files",
                params={"folder_token": root, "page_size": 5, "user_id_type": "open_id"},
            ).get("files") or []
            for f in files:
                if f.get("url"):
                    self._domain = f["url"].split("/")[2]
                    st["domain"] = self._domain
                    store.save_state(st)
                    return self._domain
        except Exception:
            pass
        self._domain = "feishu.cn"
        return self._domain

    def doc_url(self, doc_id: str) -> str:
        return f"https://{self.domain()}/docx/{doc_id}"

    def _parse_folder_input(self):
        v = self.folder_token_cfg
        if not v:
            return "", ""
        m = re.search(r"folder/([A-Za-z0-9]+)", v)
        if m:
            domain = v.split("/")[2] if v.startswith("http") else ""
            return m.group(1), domain
        return v.strip(), ""

    def ensure_folder(self) -> str:
        """返回工作文件夹 token：优先用户配置；否则在应用根目录建「✍️ 写作共创坊」并缓存"""
        if MOCK:
            return "mock-folder"
        st = store.load_state()
        token, domain = self._parse_folder_input()
        if domain and not st.get("domain"):
            self._domain = domain
            st["domain"] = domain
            store.save_state(st)
        if token:
            return token
        if st.get("folder_token"):
            return st["folder_token"]
        root = self._api("GET", "/drive/explorer/v2/root_folder/meta").get("token", "")
        data = self._api("POST", f"/drive/explorer/v2/folder/{root}", body={"title": FOLDER_NAME})
        token = data.get("token", "")
        if not token:
            raise FeishuError(f"创建根文件夹失败: {data}")
        if data.get("url"):
            self._domain = data["url"].split("/")[2]
            st["domain"] = self._domain
        st["folder_token"] = token
        store.save_state(st)
        return token

    def _share_doc(self, doc_id: str):
        """把文档设为组织内可编辑（个人租户里实际只有你自己能看，链接可直接打开）"""
        try:
            self._api(
                "PATCH", f"/drive/v1/permissions/{doc_id}/public",
                body={"link_share_entity": "tenant_editable"},
                params={"type": "docx"},
            )
        except Exception as e:
            log.warning("设置文档共享失败（不影响写入，可在飞书里手动开）: %s", e)

    def create_doc(self, title: str) -> dict:
        if MOCK:
            return {"token": f"mockdoc{int(time.time())}", "url": "https://example.com/docx/mock"}
        body = {"title": title, "folder_token": self.ensure_folder()}
        data = self._api("POST", "/docx/v1/documents", body=body)
        doc_id = (data.get("document") or {}).get("document_id", "")
        if not doc_id:
            raise FeishuError(f"创建文档失败: {data}")
        if self.auto_share:
            self._share_doc(doc_id)
        return {"token": doc_id, "url": self.doc_url(doc_id)}

    def rename_doc(self, doc_id: str, title: str):
        if MOCK:
            return
        self._api("PATCH", f"/docx/v1/documents/{doc_id}", body={"title": title})

    def append_blocks(self, doc_id: str, blocks: list):
        if MOCK:
            log.info("[MOCK 飞书] 向文档 %s 追加 %d 块", doc_id, len(blocks))
            return
        for i in range(0, len(blocks), CHUNK):
            chunk = blocks[i: i + CHUNK]
            self._api(
                "POST",
                f"/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
                body={"children": chunk, "index": -1},
            )

    def check(self) -> dict:
        """连通自检：token / 文档读写 / 文件夹，分项报告"""
        result = {"ok": False, "app_id": self.app_id, "msg": ""}
        if MOCK:
            return {"ok": True, "msg": "MOCK 模式（未真正调飞书）"}
        try:
            if not self.enabled:
                result["msg"] = "未配置 app_id / app_secret"
                return result
            self._get_token()
        except Exception as e:
            result["msg"] = str(e)
            return result
        docx_ok, docx_msg = True, ""
        try:
            doc = self.create_doc("✍️ 写作共创坊 · 自检（可删除）")
            self.append_blocks(doc["token"], [text_block([run("自检")]), divider_block()])
            try:
                self._api("DELETE", f"/drive/v1/files/{doc['token']}", params={"type": "docx"})
            except Exception:
                docx_msg = "（自检文档已建在应用空间，可手动删）"
        except Exception as e:
            docx_ok, docx_msg = False, str(e)[:160]
        folder_ok, folder_msg = True, ""
        try:
            result["folder_token"] = self.ensure_folder()
        except Exception as e:
            folder_ok, folder_msg = False, str(e)[:160]
        result["ok"] = docx_ok
        if docx_ok and folder_ok:
            result["msg"] = "飞书连接正常（文档 + 文件夹）"
        elif docx_ok:
            result["msg"] = ("文档读写正常，但文件夹权限缺失：记录会建在应用空间，你打不开。"
                             "请在开放平台开通 drive:drive 并发布版本"
                             + (f"（{folder_msg}）" if folder_msg else ""))
        else:
            result["msg"] = docx_msg
        return result


# ---------------------------------------------------------------------------
# 异步写入队列
# ---------------------------------------------------------------------------
class FeishuWriter:
    """聊天主流程只入队；后台 worker 串行写飞书，重试 3 次后落盘待恢复"""

    def __init__(self, fs: Feishu):
        self.fs = fs
        self.queue: asyncio.Queue = asyncio.Queue()
        self._task = None
        self.pending_count = 0
        self.last_error = ""

    def start(self):
        if self._task is None or self._task.done():
            st = store.load_state()
            for item in st.get("pending_feishu", []):
                self.queue.put_nowait(item)
            if st.get("pending_feishu"):
                log.info("恢复待写入飞书的块 %d 组", len(st["pending_feishu"]))
            st["pending_feishu"] = []
            store.save_state(st)
            self._task = asyncio.get_event_loop().create_task(self._worker())
            log.info("飞书写入队列已启动")

    async def enqueue(self, doc_id: str, blocks: list, label: str = ""):
        if not self.fs.enabled and not MOCK:
            return
        if not doc_id:
            return
        await self.queue.put({"doc": doc_id, "blocks": blocks, "label": label})

    async def _worker(self):
        while True:
            item = await self.queue.get()
            for attempt in range(3):
                try:
                    await asyncio.to_thread(self.fs.append_blocks, item["doc"], item["blocks"])
                    self.last_error = ""
                    break
                except Exception as e:
                    self.last_error = f"{item.get('label', '')}: {e}"
                    log.warning("飞书写入失败(%s) 第%d次: %s", item.get("label"), attempt + 1, e)
                    if attempt == 2:
                        st = store.load_state()
                        st.setdefault("pending_feishu", []).append(item)
                        store.save_state(st)
                        log.error("飞书写入最终失败，已落盘待恢复: %s", item.get("label"))
                    else:
                        await asyncio.sleep(2 * (attempt + 1))
            self.queue.task_done()

    @property
    def backlog(self) -> int:
        return self.queue.qsize() + len(store.load_state().get("pending_feishu", []))
