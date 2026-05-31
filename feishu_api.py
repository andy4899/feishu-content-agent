"""
飞书 API 封装：消息发送 + 文档创建
"""
import json
import os
from datetime import datetime, timedelta

import httpx

BASE = "https://open.feishu.cn/open-apis"


class FeishuAPI:
    def __init__(self):
        self.app_id = os.environ.get("FEISHU_APP_ID", "")
        self.app_secret = os.environ.get("FEISHU_APP_SECRET", "")
        self.folder_token = os.environ.get("FEISHU_FOLDER_TOKEN", "")
        self._token: str | None = None
        self._expires_at: datetime | None = None

    async def _token_headers(self) -> dict:
        now = datetime.now()
        if not self._token or not self._expires_at or now >= self._expires_at:
            async with httpx.AsyncClient() as c:
                r = await c.post(
                    f"{BASE}/auth/v3/tenant_access_token/internal",
                    json={"app_id": self.app_id, "app_secret": self.app_secret},
                    timeout=10,
                )
                data = r.json()
                self._token = data["tenant_access_token"]
                self._expires_at = now + timedelta(seconds=data.get("expire", 7200) - 120)
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    # ── 消息 ──────────────────────────────────────

    async def send_text(self, chat_id: str, text: str):
        headers = await self._token_headers()
        # 飞书文本消息长度限制 30720 字节；超出则分段发送
        chunks = _split_text(text, 3000)
        async with httpx.AsyncClient() as c:
            for chunk in chunks:
                await c.post(
                    f"{BASE}/im/v1/messages?receive_id_type=chat_id",
                    headers=headers,
                    json={
                        "receive_id": chat_id,
                        "msg_type": "text",
                        "content": json.dumps({"text": chunk}),
                    },
                    timeout=15,
                )

    async def send_card(self, chat_id: str, card: dict):
        """发送飞书卡片消息（用于标题选择等交互）"""
        headers = await self._token_headers()
        async with httpx.AsyncClient() as c:
            await c.post(
                f"{BASE}/im/v1/messages?receive_id_type=chat_id",
                headers=headers,
                json={
                    "receive_id": chat_id,
                    "msg_type": "interactive",
                    "content": json.dumps(card),
                },
                timeout=15,
            )

    # ── 文档 ──────────────────────────────────────

    async def create_document(self, title: str, content: str) -> str:
        """
        创建飞书文档并写入内容，返回文档 URL。
        失败返回空字符串。
        """
        headers = await self._token_headers()
        async with httpx.AsyncClient(timeout=30) as c:
            # 1. 创建空文档
            body: dict = {"title": title}
            if self.folder_token:
                body["folder_token"] = self.folder_token
            r = await c.post(f"{BASE}/docx/v1/documents", headers=headers, json=body)
            data = r.json()
            if data.get("code") != 0:
                print(f"[Feishu] create_document error: {data}")
                return ""

            doc = data["data"]["document"]
            doc_id: str = doc["document_id"]
            doc_url: str = doc["url"]

            # 2. 获取根 block id
            r2 = await c.get(f"{BASE}/docx/v1/documents/{doc_id}", headers=headers)
            root_block_id: str = r2.json()["data"]["document"]["body"]["block_id"]

            # 3. 逐块写入内容（每块最多 2000 字）
            chunks = _split_text(content, 2000)
            for chunk in chunks:
                await c.post(
                    f"{BASE}/docx/v1/documents/{doc_id}/blocks/{root_block_id}/children",
                    headers=headers,
                    json={
                        "children": [
                            {
                                "block_type": 2,  # paragraph
                                "paragraph": {
                                    "elements": [
                                        {"type": "text_run", "text_run": {"content": chunk}}
                                    ]
                                },
                            }
                        ]
                    },
                )

            return doc_url


# ── helpers ───────────────────────────────────

def _split_text(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]
