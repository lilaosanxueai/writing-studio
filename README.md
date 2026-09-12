# ✍️ 写作共创坊

和 AI 搭档一起**聊话题、撞灵感**；讨论实时记录进**飞书云文档**；聊完的素材一键整理成**可直接用的文案**。

```
聊（讨论层）→ 记（灵感 + 飞书）→ 写（文案工坊）
```

## 它和"问答机器人"的区别

搭档不是有问必答的客服，而是平视的写作伙伴：

- **互相激发**：你抛一个模糊念头，搭档追问、给角度、举反例，把它聊成能写的东西
- **互相引领**：四种讨论模式各有分工——自由聊 / 头脑风暴（发散给方向）/ 深挖追问（一次盯一个点）/ 唱反调（专挑漏洞逼你想清楚）
- **素材意识**：讨论中冒出的金句，搭档会用 💡 标出来，一键收进灵感卡片；你的发言也能 ⭐ 收藏
- **忠于材料**：生成文案时优先用你自己的话、例子和金句，不替你发明观点

## 功能

| 模块 | 说明 |
|------|------|
| 💬 讨论 | 流式对话；话题自动命名；四种模式随时切换；快捷动作（换个角度/挑战我/梳理/挑金子） |
| 💡 灵感 | 💡 行一键收下、消息 ⭐ 收藏、手动速记；可编辑备注；自动写进飞书 |
| 📌 小结 | 一键生成讨论主线 / 已成形观点 / 金句 / 待深挖 |
| ✍️ 文案 | 8 种格式（公众号/小红书/朋友圈/口播稿/金句合集/演讲提纲/深度长文/自定义）× 5 种语气 × 3 档长度；流式生成、指令改写、手工编辑、复制/下载/存飞书 |
| 📘 飞书 | 每个话题一篇文档（自动建在「✍️ 写作共创坊」文件夹）；讨论实时异步写入，失败落盘重试；文案可追加进话题文档或单独成文 |

## 快速开始

```bash
cd writing-studio
pip install -r requirements.txt
python app.py            # → http://127.0.0.1:8320
```

- 首次使用点右上 ⚙ 填大模型 API Key（DeepSeek / 智谱等 OpenAI 兼容接口均可）与飞书应用凭据
- 自检：`python app.py --check`（LLM 连通 + 飞书文档读写，会在飞书建一篇自检文档并删除）
- Windows 双击 `start.bat` 即可

## 飞书应用准备

1. [飞书开放平台](https://open.feishu.cn/) 建一个**企业自建应用**
2. 开通权限：`docx:document`（云文档读写）、`drive:drive`（文件夹），发布版本
3. 把 App ID / App Secret 填进设置（或 `config.json` → `feishu`）
4. 文档默认建在应用根目录的「✍️ 写作共创坊」文件夹；也可在设置里指定 `folder_token`

> 个人版飞书同样可用：应用建在自己的租户里，`auto_share_tenant` 会把文档设为组织内可编辑，链接直接打开。

## 配置（config.json）

```jsonc
{
  "llm": {
    "api_key": "sk-…",              // 主模型
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-flash",        // 聊天（快）
    "draft_model": "deepseek-v4-pro", // 写文案（质量优先）
    "backup_api_key": "…",            // 备用（主模型挂了自动切）
    "backup_base_url": "https://open.bigmodel.cn/api/paas/v4",
    "backup_model": "glm-5.3-flash"
  },
  "feishu": {
    "app_id": "cli_…",
    "app_secret": "…",
    "folder_token": "",               // 留空自动建文件夹
    "auto_share_tenant": true,
    "auto_record": true               // 讨论实时写入（界面里也可开关）
  },
  "server": { "lan": false, "access_token": "" }
}
```

- `server.lan: true` 开局域网访问（手机可用），自动生成访问令牌打印在控制台
- 联调不碰外网：`WS_MOCK=1 python app.py`

## 目录结构

```
writing-studio/
├── app.py            # FastAPI 服务（对话/灵感/文案/飞书 API）
├── llm.py            # 大模型客户端（主备切换、流式）
├── feishu.py         # 飞书云文档客户端 + 异步写入队列
├── prompts.py        # 搭档人格 / 模式 / 文案工坊提示词
├── store.py          # 会话持久化（data/sessions/*.json）
├── static/           # 前端（原生 JS 单页）
├── config.json       # 本机配置（含密钥，已 gitignore）
└── data/             # 会话数据（已 gitignore）
```
