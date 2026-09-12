# -*- coding: utf-8 -*-
"""端到端冒烟测试：建话题 → 聊天 → 灵感 → 小结 → 文案 → 飞书状态"""
import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
BASE = "http://127.0.0.1:8320"


def call(method, path, body=None, stream=False):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    r = urllib.request.urlopen(req, timeout=300)
    if stream:
        return r  # 返回原始流
    return json.loads(r.read().decode("utf-8"))


def read_sse(resp):
    """读 SSE 流，返回 (full_text, done_event)"""
    full, done = "", None
    buf = b""
    while True:
        chunk = resp.read(1)
        if not chunk:
            break
        buf += chunk
        while b"\n\n" in buf:
            block, buf = buf.split(b"\n\n", 1)
            for line in block.decode("utf-8").split("\n"):
                if line.startswith("data: "):
                    ev = json.loads(line[6:])
                    if ev["t"] == "delta":
                        full += ev["v"]
                    elif ev["t"] == "done":
                        done = ev
                    elif ev["t"] == "error":
                        raise RuntimeError("SSE error: " + ev["v"])
    return full, done


ok = lambda name: print(f"  ✓ {name}")
fail = lambda name, e: (print(f"  ✗ {name}: {e}"), sys.exit(1))

print("1. 建话题（深挖模式）")
s = call("POST", "/api/sessions", {"title": "为什么人越来越不想社交", "mode": "deepdive"})
sid = s["id"]
ok(f"sid={sid}")

print("2. 第一轮聊天（流式）")
resp = call("POST", f"/api/sessions/{sid}/chat",
            {"message": "我最近想写一篇关于「为什么人越来越不想社交」的文章，只有一个模糊感觉：大家好像都很累，懒得维持关系了。帮我把话题聊透。"},
            stream=True)
full, done = read_sse(resp)
print(f"  搭档回复 {len(full)} 字，预览: {full[:80]}…")
print(f"  done 事件: {done}")
if len(full) < 50:
    fail("聊天回复太短", len(full))
ok("聊天流式完成")

print("3. 等待飞书异步同步…")
import time
for _ in range(20):
    st = call("GET", f"/api/feishu/status?sid={sid}")
    if st.get("doc"):
        break
    time.sleep(1)
doc = st.get("doc") or {}
print(f"  doc: {doc.get('url')} | 已记录 {doc.get('turns_synced')}/{doc.get('total_turns')} 轮 | err={doc.get('error')}")
if not doc.get("url"):
    fail("飞书文档未创建", st)
for _ in range(15):
    st = call("GET", f"/api/feishu/status?sid={sid}")
    d = st.get("doc") or {}
    if d.get("turns_synced", 0) >= 1 and st.get("backlog", 1) == 0:
        break
    time.sleep(1)
ok(f"飞书已记录 {st['doc']['turns_synced']} 轮，积压 {st['backlog']}")

print("4. 第二轮聊天 + 💡 灵感行")
resp = call("POST", f"/api/sessions/{sid}/chat",
            {"message": "对，就是那种明明不讨厌对方，但一想到要出门赴约就累的感觉。"}, stream=True)
full2, done2 = read_sse(resp)
has_spark = "💡" in full2
print(f"  回复 {len(full2)} 字，含💡行: {has_spark}")
ok("第二轮完成")

print("5. 手动加灵感")
sp = call("POST", f"/api/sessions/{sid}/sparks",
          {"text": "社交疲惫的本质是「情绪劳动的预支」", "origin": "user"})
ok(f"spark id={sp['id']}")
time.sleep(3)

print("6. 生成小结")
r = call("POST", f"/api/sessions/{sid}/summary")
sm = r["summary"]["text"]
print(f"  小结 {len(sm)} 字，预览: {sm[:100]}…")
if len(sm) < 80:
    fail("小结太短", len(sm))
ok("小结完成")

print("7. 生成文案（朋友圈格式，流式）")
resp = call("POST", f"/api/sessions/{sid}/drafts",
            {"format": "moments", "tone": "casual", "length": "short", "extra": ""}, stream=True)
dtext, ddone = read_sse(resp)
draft = (ddone or {}).get("draft", {})
print(f"  文案 {len(draft.get('content',''))} 字，预览: {draft.get('content','')[:80]}…")
if not draft.get("content"):
    fail("文案为空", dtext[:100])
ok("文案生成完成")

print("8. 文案写进飞书（追加进话题文档）")
r = call("POST", f"/api/sessions/{sid}/drafts/{draft['id']}/feishu", {"separate": False})
print(f"  url: {r.get('url')}")
if not r.get("ok"):
    fail("文案写飞书失败", r)
time.sleep(4)
st = call("GET", f"/api/feishu/status?sid={sid}")
ok(f"积压 {st['backlog']}，最后错误: {st['last_error'] or '无'}")

print("9. 会话持久化检查")
s2 = call("GET", f"/api/sessions/{sid}")
assert len(s2["messages"]) == 4, "消息数不对"
assert len(s2["sparks"]) == 1
assert len(s2["drafts"]) == 1
assert s2["summary"]
ok(f"消息 {len(s2['messages'])} / 灵感 {len(s2['sparks'])} / 文案 {len(s2['drafts'])} / 有小结")

print("10. 清理测试会话")
call("DELETE", f"/api/sessions/{sid}")
ok("已删除（飞书文档保留，可在飞书手动清理）")

print("\n🎉 全部通过")
