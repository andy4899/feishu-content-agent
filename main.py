"""
飞书内容生成 Agent
支持：法考小红书 / ADHD小红书 / 劳动法小红书
"""
import json
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Optional

import logging
import anthropic
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("app")

from feishu_api import FeishuAPI
from prompts import PAIN_POOL, get_pain_pool, get_system_prompt, parse_command

load_dotenv()

# ── 常量 ──────────────────────────────────────

MODEL = os.environ.get("ANTHROPIC_MODEL", "glm-5.1")
SESSION_TTL = 1800  # 30分钟未操作自动清除

# ── 全局对象 ──────────────────────────────────

feishu = FeishuAPI()
claude = anthropic.AsyncAnthropic(
    api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
    base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
)

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
welcomed_users: set = set()  # 已发送过欢迎消息的用户


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

HELP_TEXT = """👋 你好！我是内容生成助手，支持以下 4 类内容：

━━━━━━━━━━━━━━━━━━
📚 【法考小红书】
直接输入「法考」即可，考点自动从题库抽取
也可指定赛道和策略：法考 A 1

赛道：A = 在职备考  B = 二战三战
策略：1 = 种草  2 = 科普  3 = 鼓励  4 = 规划

━━━━━━━━━━━━━━━━━━
🧠 【ADHD 小红书】
直接输入「小红书」即可，话题自动从痛点库抽取
也可手动指定：小红书 【痛点话题】
或直接给标题：小红书 《你的标题》

━━━━━━━━━━━━━━━━━━
📰 【ADHD 公众号】
直接输入「公众号」即可，话题自动从痛点库抽取
也可手动指定：公众号 【痛点话题】
或直接给标题：公众号 《你的标题》

━━━━━━━━━━━━━━━━━━
⚖️ 【劳动法小红书】
格式：劳动法 [赛道] [植入]
示例：劳动法  /  劳动法 A 是  /  劳动法 B 否

赛道：A = 员工视角（默认） B = 企业视角
植入：是 = 带产品推荐（默认） 否 = 纯方法

话题自动从痛点库抽取，也可手动指定：
劳动法 【试用期被辞退】 A 是

━━━━━━━━━━━━━━━━━━
💡 所有类别都支持自动选题，直接输入关键词即可
选标题时回复「换」可换新话题重新生成
发送「帮助」随时查看此说明"""


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
        elif mode == "auto" or not topic:
            # 自动选题
            pool = get_pain_pool(skill)
            if pool.is_empty():
                await feishu.send_text(chat_id, "📦 ADHD痛点池已空，正在自动生成新话题...")
                topic = "ADHD日常困扰"
            else:
                topic = pool.get_next()
            params["input"] = topic
            await feishu.send_text(chat_id, f"📌 本期选题：{topic}\n📦 剩余：{pool.remaining()} 条\n\n⏳ 生成标题中...")
            first_user_msg = (
                f"话题/痛点：{topic}。请执行模块1，生成4个标题，输出后等待我选择。"
            )
        else:
            await feishu.send_text(chat_id, f"📝 话题：{topic}\n\n⏳ 生成标题中...")
            first_user_msg = (
                f"话题/痛点：{topic}。请执行模块1，生成4个标题，输出后等待我选择。"
            )

    # ── ADHD 公众号 ──
    elif skill == "adhd_gzh":
        mode = params.get("mode", "topic")
        topic = params.get("input", "")
        if mode == "title":
            await feishu.send_text(chat_id, f"📰 已有标题：「{topic}」\n\n⏳ 直接生成公众号正文和配图提示词中...")
            first_user_msg = (
                f"已有标题：「{topic}」，跳过模块1，直接执行模块2、模块3、模块4，完整输出。"
            )
        elif mode == "auto" or not topic:
            # 自动选题
            pool = get_pain_pool(skill)
            if pool.is_empty():
                await feishu.send_text(chat_id, "📦 ADHD痛点池已空，正在自动生成新话题...")
                topic = "ADHD日常困扰"
            else:
                topic = pool.get_next()
            params["input"] = topic
            await feishu.send_text(chat_id, f"📌 本期选题：{topic}\n📦 剩余：{pool.remaining()} 条\n\n⏳ 生成公众号标题中...")
            first_user_msg = (
                f"话题/痛点：{topic}。请执行模块1，生成4组标题（主标题+副标题），输出后等待我选择。"
            )
        else:
            await feishu.send_text(chat_id, f"📰 话题：{topic}\n\n⏳ 生成公众号标题中...")
            first_user_msg = (
                f"话题/痛点：{topic}。请执行模块1，生成4组标题（主标题+副标题），输出后等待我选择。"
            )

    # ── 劳动法 ──
    else:
        inject_label = "是" if params.get("inject") else "否"
        track_label = "员工端" if params["track"] == "A" else "企业端/HR"
        topic = params.get("topic")
        if not topic:
            # 自动选题
            pool = get_pain_pool("laborlaw")
            if pool.is_empty():
                await feishu.send_text(chat_id, "📦 劳动法痛点池已空，正在自动生成新话题...")
                topic = "职场权益保护"
            else:
                topic = pool.get_next()
            params["topic"] = topic
        await feishu.send_text(
            chat_id,
            f"📌 本期选题：{topic}\n"
            f"赛道：{track_label}  植入：{inject_label}\n\n"
            "⏳ 生成标题中...",
        )
        first_user_msg = (
            f"话题：{topic}，赛道：{params['track']}，植入：{inject_label}。"
            "请执行模块1，生成5个标题（每个≤20字），输出表格：标题候选 | 命中要素 | 安全校验。输出后等待我选择。"
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
    reply_clean = clean_output(reply)
    sessions[open_id]["messages"].append({"role": "assistant", "content": reply})

    # 判断 skill 是否需要等标题选择（adhd_xhs 已有标题模式不需要）
    skip_title_wait = skill == "adhd_xhs" and params.get("mode") == "title"
    if skip_title_wait:
        sessions[open_id]["state"] = "done"
        await _finalize(open_id, chat_id, reply_clean, skill, params)
    else:
        sessions[open_id]["state"] = "waiting_title"
        sessions[open_id]["last_active"] = time.time()
        await feishu.send_text(chat_id, reply_clean)


# ── 标题选择处理 ──────────────────────────────

async def process_title_choice(open_id: str, chat_id: str, choice: str, session: dict):
    choice = choice.strip()

    # 重新生成：换一个痛点重新出标题
    if choice in {"换", "换一个", "重新", "重新生成", "重选", "退出", "下一个"}:
        sessions.pop(open_id, None)
        # 把当前痛点放回池子（索引回退1）或直接用新痛点
        await feishu.send_text(chat_id, "🔄 好的，帮你换一个新话题重新生成标题...\n\n⏳ 请稍候...")
        # 直接当作新指令处理，会自动选题
        await process_new_command(open_id, chat_id, _reconstruct_command(session))
        return

    if choice in {"1", "2", "3", "4", "5"}:
        user_msg = f"我选择标题{choice}。请继续执行模块2、模块3、模块4，完整输出所有内容。"
    elif any(k in choice for k in ["推荐", "你推荐", "推荐的"]):
        user_msg = "用你推荐的标题，继续执行模块2、模块3、模块4，完整输出所有内容。"
    else:
        await feishu.send_text(chat_id, "请回复 1-5 选择标题，或回复「换」重新生成新话题")
        return

    session["state"] = "generating"
    session["last_active"] = time.time()
    session["messages"].append({"role": "user", "content": user_msg})

    await feishu.send_text(chat_id, "✅ 已选标题，正在生成正文和图片提示词，请稍候...")

    try:
        reply = await _call_claude(session["system"], session["messages"])
        reply_clean = clean_output(reply)
        session["messages"].append({"role": "assistant", "content": reply})
        session["state"] = "done"
        await _finalize(open_id, chat_id, reply_clean, session["skill"], session["params"])
    except Exception as e:
        await feishu.send_text(chat_id, f"⚠️ 生成失败：{e}")


# ── 生成完毕：创建飞书文档 ─────────────────────

async def _finalize(open_id: str, chat_id: str, content: str, skill: str, params: dict):
    # 直接把内容发到聊天（send_text 内部会自动按 3000 字分段）
    if content:
        await feishu.send_text(chat_id, f"✅ 内容已生成完毕！\n\n{content}")
    else:
        await feishu.send_text(chat_id, "⚠️ 内容生成为空，请重试。")

    # 存飞书文档
    try:
        doc_title = _build_doc_title(skill, params)
        doc_url = await feishu.create_document(doc_title, content)
        if doc_url:
            await feishu.send_text(chat_id, f"📄 飞书文档：{doc_url}")
    except Exception as e:
        logger.exception("create_document failed")
        await feishu.send_text(chat_id, f"⚠️ 文档写入失败：{e}\n内容已在上方消息中，请手动复制保存。")

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
        max_tokens=8192,
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


def _reconstruct_command(session: dict) -> str:
    """根据 session 信息还原用户指令，用于「换一个」时重新触发。"""
    skill = session.get("skill", "")
    params = session.get("params", {})
    if skill == "lawexam":
        track = params.get("track", "A")
        strategy = params.get("strategy", "1")
        return f"法考 {track} {strategy}"
    elif skill == "adhd_xhs":
        return "小红书"
    elif skill == "adhd_gzh":
        return "公众号"
    elif skill == "laborlaw":
        track = params.get("track", "A")
        inject = "是" if params.get("inject", True) else "否"
        return f"劳动法 {track} {inject}"
    return "帮助"


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
    """手动补充痛点池，body: {"skill": "lawexam", "topics": ["topic1", ...]}"""
    body = await request.json()
    skill = body.get("skill", "lawexam")
    topics = body.get("topics", [])
    pool = get_pain_pool(skill)
    pool.refill(topics)
    return {"added": len(topics), "total": pool.remaining()}


# ── 输出清洗 ──────────────────────────────────

def clean_output(text: str) -> str:
    """强制清除 AI 输出中的 ** ## 模块标签 等违规格式。
    即使 AI 不遵守纯文本排版协议，post-processing 也能保证输出干净。"""
    # 1. 清除 **加粗** → 替换为「文字」
    text = re.sub(r'\*\*(.+?)\*\*', r'「\1」', text)
    # 2. 清除 ## 标题标记
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # 3. 清除模块标签（01. 破冰留人 / 02. 核心内容 等）
    module_labels = [
        r'\d{2}\.\s*破冰留人',
        r'\d{2}\.\s*核心内容',
        r'\d{2}\.\s*长期养成式互动',
        r'\d{2}\.\s*标签矩阵',
        r'\d{2}\.\s*异常捕捉',
        r'\d{2}\.\s*系统日志',
        r'\d{2}\.\s*核心归因',
        r'\d{2}\.\s*补丁程序',
        r'\d{2}\.\s*执行触点',
        r'\d{2}\.\s*引导互动',
        r'\d{2}\.\s*话题注入',
    ]
    for label in module_labels:
        text = re.sub(rf'^{label}\s*\n?', '', text, flags=re.MULTILINE)
    # 4. 替换中文引号 "…" → 「…」
    text = text.replace('“', '「').replace('”', '」')
    text = text.replace('„', '「').replace('‟', '」')
    # 5. 清除 > 引用块标记
    text = re.sub(r'^>\s+', '', text, flags=re.MULTILINE)
    # 6. 清除 - 列表标记（短横线+空格开头），但保留 emoji 开头的行
    text = re.sub(r'^-\s+(?![🔹🔸▫️⚡⚠️✅💡🫧📌])', '🔹 ', text, flags=re.MULTILINE)
    # 7. 清除 Markdown 表格行（| --- | --- | 格式）
    text = re.sub(r'^\|[\s:|-]+\|$', '', text, flags=re.MULTILINE)
    # 8. 清除表格分隔线
    text = re.sub(r'^\|[-:\s]+\|$', '', text, flags=re.MULTILINE)
    # 9. 清除多余空行（连续3个以上空行压缩为2个）
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text
