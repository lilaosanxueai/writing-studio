"""大模型客户端（OpenAI 兼容接口）

- 主模型 + 备用模型自动切换（主模型失败自动切备用）
- chat_once  : 拿完整回复（小结 / 起标题 / 自检用）
- chat_stream: 流式吐 delta（聊天 / 写文案用）
- thinking 关闭参数对 DeepSeek / GLM 通用，轻任务关思考链首字更快
- MOCK 模式：WS_MOCK=1 时返回固定文本，不真正调接口
"""
import logging
import os

import httpx

log = logging.getLogger("studio.llm")

MOCK = os.environ.get("WS_MOCK") == "1"

_MOCK_REPLY = "（mock 回复）这个点有意思，能展开说说吗？比如你第一次意识到这件事是什么时候？"


class LLM:
    def __init__(self, cfg: dict):
        self.api_key = (cfg.get("api_key") or "").strip()
        self.base_url = (cfg.get("base_url") or "https://api.deepseek.com").rstrip("/")
        self.model = cfg.get("model") or "deepseek-flash"
        self.fast_model = cfg.get("fast_model") or self.model
        self.draft_model = cfg.get("draft_model") or self.model
        self.backup_api_key = (cfg.get("backup_api_key") or "").strip()
        self.backup_base_url = (cfg.get("backup_base_url") or "").rstrip("/")
        self.backup_model = (cfg.get("backup_model") or "").strip()
        self.http = httpx.AsyncClient(timeout=180)

    @property
    def ready(self) -> bool:
        return bool(self.api_key)

    @property
    def backup_ready(self) -> bool:
        return bool(self.backup_api_key and self.backup_model and self.backup_base_url)

    def update(self, cfg: dict):
        self.__init__(cfg)

    def _providers(self):
        """候选顺序：主 → 备"""
        ps = [(self.base_url, self.api_key, self.model)]
        if self.backup_ready:
            ps.append((self.backup_base_url, self.backup_api_key, self.backup_model))
        return ps

    def _body(self, messages, model, stream, temperature, max_tokens, thinking):
        body = {
            "model": model,
            "messages": messages,
            "stream": stream,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if not thinking:
            body["thinking"] = {"type": "disabled"}
        return body

    def _headers(self, api_key):
        h = {"Content-Type": "application/json"}
        if api_key:
            h["Authorization"] = f"Bearer {api_key}"
        return h

    async def chat_once(self, messages, model=None, temperature=0.8,
                        max_tokens=2048, thinking=False) -> str:
        if MOCK:
            return _MOCK_REPLY
        last_err = None
        for base, key, mdl in self._providers():
            try:
                use_model = model or mdl
                # 指定了主库特有模型名而当前是备用库时，用备用库自己的模型
                if model and mdl != model:
                    use_model = mdl
                r = await self.http.post(
                    f"{base}/chat/completions",
                    json=self._body(messages, use_model, False, temperature, max_tokens, thinking),
                    headers=self._headers(key),
                )
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                data = r.json()
                msg = (data.get("choices") or [{}])[0].get("message", {})
                content = msg.get("content") or msg.get("reasoning_content") or ""
                if content.strip():
                    return content.strip()
                raise RuntimeError("空回复")
            except Exception as e:
                last_err = e
                log.warning("模型调用失败(%s): %s", mdl, e)
        raise RuntimeError(f"所有模型均调用失败: {last_err}")

    async def chat_stream(self, messages, model=None, temperature=0.8,
                          max_tokens=2048, thinking=False):
        """流式生成，yield 每个 delta 文本片段。失败自动切备用。"""
        if MOCK:
            yield _MOCK_REPLY
            return
        last_err = None
        for base, key, mdl in self._providers():
            try:
                use_model = model or mdl
                async with self.http.stream(
                    "POST",
                    f"{base}/chat/completions",
                    json=self._body(messages, use_model, True, temperature, max_tokens, thinking),
                    headers=self._headers(key),
                ) as r:
                    if r.status_code != 200:
                        text = (await r.aread()).decode("utf-8", "ignore")
                        raise RuntimeError(f"HTTP {r.status_code}: {text[:200]}")
                    got = False
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if not payload or payload == "[DONE]":
                            continue
                        try:
                            import json
                            obj = json.loads(payload)
                        except Exception:
                            continue
                        choices = obj.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta", {})
                        piece = delta.get("content") or ""
                        if piece:
                            got = True
                            yield piece
                    if got:
                        return
                    raise RuntimeError("流式返回为空")
            except Exception as e:
                last_err = e
                log.warning("流式调用失败(%s)，尝试下一个: %s", mdl, e)
        raise RuntimeError(f"所有模型均调用失败: {last_err}")

    async def close(self):
        await self.http.aclose()
