"""
飞书内容生成 Agent
支持：法考小红书 / ADHD小红书 / 劳动法小红书
"""
import json
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

import anthropic
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

from feishu_api import FeishuAPI
from prompts import PAIN_POOL, get_system_prompt, parse_command

load_dotenv()

# ── 常量 ──────────────────────────────────────

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
SESSION_TTL = 1800  # 30分钟未操作自动清除

# ── 全局对象 ──────────────────────────────────

feishu = FeishuAPI()
claude = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

# Session 结构：
# {
#   open_id: {
#     state: "waiting_title" | "generating" | "done",
#     skill: str,
#     params: dict,
#     chat_id: str,
#     system: str,
#     messages: list,
#     last_active: float,
#   }
# }
sessions: dict = {}
welcomed_users: set = {}  # 已发送过欢迎消息的用户


# ── FastAPI 生命周期 ───────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    sessions.clear()

app = FastAPI(lifespan=lifespan)


# ── Webhook 入口 ──────────────────────────────

@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()

    # 飞书 URL 校验（首次配置时）
    if body.get("type") == "url_verification":
        return JSONResponse({"challenge": body["challenge"]})

    event_type = body.get("header", {}).get("event_type", "")
    if event_type == "im.message.receive_v1":
        background_tasks.add_task(handle_message, body.get("event", {}))

    return JSONResponse({"code": 0})


# ── 消息处理 ──────────────────────────────────

async def handle_message(event: dict):
    msg = event.get("message", {})
    sender = event.get("sender", {}).get("sender_id", {})
    open_id = sender.get("open_id", "")
    chat_id = msg.get("chat_id", "")
    msg_type = msg.get("message_type", "")

    if msg_type != "text" or not open_id or not chat_id:
        return

    try:
        text = json.loads(msg.get("content", "{}")).get("text", "").strip()
    except (json.JSONDecodeError, AttributeError):
        return

    if not text:
        return

    _expire_old_sessions()
    session = sessions.get(open_id)

    if open_id not in welcomed_users:
        welcomed_users.add(open_id)
        await feishu.send_text(chat_id, HELP_TEXT)

    if session and session["state"] == "waiting_title":
        await process_title_choice(open_id, chat_id, text, session)
    elif session and session["state"] == "generating":
        await feishu.send_text(chat_id, "⏳ 正在生成中，请稍候...")
    else:
        await process_new_command(open_id, chat_id, text)


# ── 新指令处理 ────────────────────────────────

HELP_TEXT = """👋 你好！我是内容生成助手，支持以下 3 类内容：

━━━━━━━━━━━━━━━━━━
📚 【法考小红书】
格式：法考 [赛道] [策略]
示例：法考 A 1

赛道：
  A = 在职备考（上班族考生）
  B = 二战/三战（全职备考）

策略：
  1 = 种草（吸引关注、引发共鸣）
  2 = 科普（知识点拆解、干货输出）
  3 = 鼓励（情绪价值、打气）
  4 = 规划（备考方法、时间安排）

考点由系统自动从题库抽取，无需填写。

━━━━━━━━━━━━━━━━━━
🧠 【ADHD 小红书】
格式一：小红书 【痛点话题】  → 生成4个标题供你选择
格式二：小红书 《你的标题》  → 直接用这个标题生成正文

示例：
  小红书 【明明想开始但身体动不了】
  小红书 《你不是懒，是大脑卡住了》

━━━━━━━━━━━━━━━━━━
⚖️ 【劳动法小红书】
格式：劳动法 【话题】 [赛道] [是/否]

赛道：
  A = 员工视角（维权/避坑）
  B = 企业视角（合规/管理）

是/否：是否在文中植入产品/工具推荐

示例：
  劳动法 【试用期被辞退】 A 是
  劳动法 【考勤管理漏洞】 B 否

━━━━━━━━━━━━━━━━━━
发送「帮助」随时查看此说明 💡"""


async def process_new_command(open_id: str, chat_id: str, text: str):
    if text.strip() in ("帮助", "help", "？", "?", "使用说明"):
        await feishu.send_text(chat_id, HELP_TEXT)
        return

    result = parse_command(text)
    if not result:
        await feishu.send_text(
            chat_id,
            "🤔 未识别指令，发送「帮助」查看完整使用说明。\n\n"
            "快速参考：\n"
            "• 法考 A 1\n"
            "• 小红书 【ADHD话题】\n"
            "• 小红书 《已有标题》\n"
            "• 劳动法 【话题】 A 是",
        )
        return

    skill, params = result

    # ── 法考：从痛点池取考点 ──
    if skill == "lawexam":
        if PAIN_POOL.is_empty():
            await feishu.send_text(chat_id, "📦 痛点池已空，正在自动生成新考点...")
            topic = await _auto_generate_topic(params["track"])
        else:
            topic = PAIN_POOL.get_next()
        params["topic"] = topic
        await feishu.send_text(
            chat_id,
            f"🔍 本次考点：{topic}\n"
            f"📦 池中剩余：{PAIN_POOL.remaining()} 条\n\n"
            "⏳ 生成标题中...",
        )
        first_user_msg = (
            f"考点：{params['topic']}，赛道：{params['track']}，策略：{params['strategy']}。"
            "请执行模块1，生成4个标题，输出后等待我选择。"
        )

    # ── ADHD 小红书 ──
    elif skill == "adhd_xhs":
        mode = params.get("mode", "topic")
        topic = params.get("input", "")
        if mode == "title":
            await feishu.send_text(chat_id, f"📝 已有标题：「{topic}」\n\n⏳ 直接生成正文和图片提示词中...")
            first_user_msg = (
                f"已有标题：「{topic}」，跳过模块1，直接执行模块2、模块3、模块4，完整输出。"
            )
        else:
            await feishu.send_text(chat_id, f"📝 话题：{topic}\n\n⏳ 生成标题中...")
            first_user_msg = (
                f"话题/痛点：{topic}。请执行模块1，生成4个标题，输出后等待我选择。"
            )

    # ── 劳动法 ──
    else:
        inject_label = "是" if params.get("inject") else "否"
        track_label = "员工端" if params["track"] == "A" else "企业端/HR"
        await feishu.send_text(
            chat_id,
            f"📝 话题：{params['topic']}\n"
            f"赛道：{track_label}  植入：{inject_label}\n\n"
            "⏳ 生成标题中...",
        )
        first_user_msg = (
            f"话题：{params['topic']}，赛道：{params['track']}，植入：{inject_label}。"
            "请执行模块1，生成4个标题，输出后等待我选择。"
        )

    system_prompt = get_system_prompt(skill)
    msgs = [{"role": "user", "content": first_user_msg}]

    sessions[open_id] = {
        "state": "generating",
        "skill": skill,
        "params": params,
        "chat_id": chat_id,
        "system": system_prompt,
        "messages": msgs,
        "last_active": time.time(),
    }

    reply = await _call_claude(system_prompt, msgs)
    sessions[open_id]["messages"].append({"role": "assistant", "content": reply})

    # 判断 skill 是否需要等标题选择（adhd_xhs 已有标题模式不需要）
    skip_title_wait = skill == "adhd_xhs" and params.get("mode") == "title"
    if skip_title_wait:
        sessions[open_id]["state"] = "done"
        await _finalize(open_id, chat_id, reply, skill, params)
    else:
        sessions[open_id]["state"] = "waiting_title"
        sessions[open_id]["last_active"] = time.time()
        await feishu.send_text(chat_id, reply)


# ── 标题选择处理 ──────────────────────────────

async def process_title_choice(open_id: str, chat_id: str, choice: str, session: dict):
    choice = choice.strip()

    if choice in {"1", "2", "3", "4"}:
        user_msg = f"我选择标题{choice}。请继续执行模块2、模块3、模块4，完整输出所有内容。"
    elif any(k in choice for k in ["推荐", "你推荐", "推荐的"]):
        user_msg = "用你推荐的标题，继续执行模块2、模块3、模块4，完整输出所有内容。"
    else:
        await feishu.send_text(chat_id, "请回复 1、2、3、4 或「用你推荐的」")
        return

    session["state"] = "generating"
    session["last_active"] = time.time()
    session["messages"].append({"role": "user", "content": user_msg})

    await feishu.send_text(chat_id, "✅ 已选标题，正在生成正文和图片提示词，请稍候...")

    reply = await _call_claude(session["system"], session["messages"])
    session["messages"].append({"role": "assistant", "content": reply})
    session["state"] = "done"

    await _finalize(open_id, chat_id, reply, session["skill"], session["params"])


# ── 生成完毕：创建飞书文档 ─────────────────────

async def _finalize(open_id: str, chat_id: str, content: str, skill: str, params: dict):
    doc_title = _build_doc_title(skill, params)
    doc_url = await feishu.create_document(doc_title, content)

    if doc_url:
        await feishu.send_text(
            chat_id,
            f"✅ 内容已生成完毕！\n\n📄 飞书文档：{doc_url}",
        )
    else:
        # 文档创建失败时直接发送内容（前1500字 + 提示）
        preview = content[:1500]
        await feishu.send_text(
            chat_id,
            f"✅ 内容已生成（文档创建失败，直接发送内容）：\n\n{preview}\n\n"
            f"{'…（内容较长已截断）' if len(content) > 1500 else ''}",
        )

    sessions.pop(open_id, None)


# ── 内部工具 ──────────────────────────────────

def _build_doc_title(skill: str, params: dict) -> str:
    from datetime import date
    today = date.today().strftime("%Y-%m-%d")
    if skill == "lawexam":
        topic = params.get("topic", "")[:8]
        return f"法考_小红书_{today}_{topic}_赛道{params['track']}_策略{params['strategy']}"
    elif skill == "adhd_xhs":
        topic = params.get("input", "")[:10]
        return f"ADHD_小红书_{today}_{topic}"
    else:
        topic = params.get("topic", "")[:10]
        track = "员工" if params["track"] == "A" else "企业"
        return f"劳动法_小红书_{today}_{topic}_{track}端"


async def _call_claude(system: str, messages: list) -> str:
    resp = await claude.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=system,
        messages=messages,
    )
    return resp.content[0].text


async def _auto_generate_topic(track: str) -> str:
    """痛点池为空时，用 Claude 自动生成一个法考考点"""
    track_desc = "在职备考" if track == "A" else "二战三战"
    resp = await claude.messages.create(
        model=MODEL,
        max_tokens=60,
        messages=[{
            "role": "user",
            "content": (
                f"为{track_desc}的法考考生生成一个备考卡点，"
                "格式：具体考点名称+卡点描述，15字以内，只输出文本本身，不加任何前缀或标点。"
            ),
        }],
    )
    return resp.content[0].text.strip()


def _expire_old_sessions():
    cutoff = time.time() - SESSION_TTL
    expired = [uid for uid, s in sessions.items() if s["last_active"] < cutoff]
    for uid in expired:
        sessions.pop(uid, None)


# ── 健康检查 ──────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "active_sessions": len(sessions),
        "pain_pool_remaining": PAIN_POOL.remaining(),
    }


# ── 管理指令（飞书消息中以 / 开头触发）─────────

@app.post("/admin/refill-pool")
async def refill_pool(request: Request):
    """手动补充痛点池，body: {"topics": ["topic1", ...]}"""
    body = await request.json()
    topics = body.get("topics", [])
    PAIN_POOL.refill(topics)
    return {"added": len(topics), "total": PAIN_POOL.remaining()}
