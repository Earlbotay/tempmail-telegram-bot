#!/usr/bin/env python3
"""
Telegram Temp Mail Bot
- /start: Papar temp mail semasa dengan countdown 5 minit
- Auto-rotate email setiap 5 minit
- Auto-check inbox setiap 10 saat
- Butang Delete (merah) untuk ganti email segera
- Semua mesej dalam <blockquote> HTML
- Dijalankan di GitHub Actions (cron 5 jam)
"""

import os
import sys
import time
import signal
import asyncio
import logging
import html
from datetime import datetime, timezone, timedelta

import httpx

# ─── CONFIG ───
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
API_MAIL = "https://api.guerrillamail.com/ajax.php"
ROTATE_INTERVAL = 300  # 5 minit
INBOX_CHECK_INTERVAL = 10  # 10 saat
MAX_RUNTIME = 5 * 3600 - 120  # 4h58m (buffer 2 min sebelum workflow tamat)

# ─── LOGGING ───
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tempmail-bot")

# ─── STATE ───
state = {
    "sid_token": None,
    "email": None,
    "rotate_at": 0,
    "chat_ids": set(),
    "last_mail_ids": set(),
    "msg_ids": {},  # chat_id -> message_id for edit
    "running": True,
    "offset": 0,
}


# ═══════════════════════════════════════════
#  GUERRILLAMAIL API
# ═══════════════════════════════════════════
async def mail_api(client: httpx.AsyncClient, params: dict) -> dict:
    """Call GuerrillaMail API with error handling."""
    if state["sid_token"]:
        params["sid_token"] = state["sid_token"]
    try:
        resp = await client.get(API_MAIL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if "sid_token" in data:
            state["sid_token"] = data["sid_token"]
        return data
    except Exception as e:
        log.error(f"Mail API error: {e}")
        return {}


async def get_new_email(client: httpx.AsyncClient) -> str:
    """Dapatkan email baru (forget + get)."""
    if state["sid_token"]:
        await mail_api(client, {"f": "forget_me"})
        state["sid_token"] = None
    data = await mail_api(client, {"f": "get_email_address"})
    email = data.get("email_addr", "error@unknown")
    state["email"] = email
    state["rotate_at"] = time.time() + ROTATE_INTERVAL
    state["last_mail_ids"] = set()
    log.info(f"New email: {email}")
    return email


async def check_inbox(client: httpx.AsyncClient) -> list:
    """Semak inbox, return senarai email baru sahaja."""
    data = await mail_api(client, {"f": "check_email", "seq": "0"})
    emails = data.get("list", [])
    if not isinstance(emails, list):
        return []
    new_emails = []
    for em in emails:
        mid = str(em.get("mail_id", ""))
        if mid and mid not in state["last_mail_ids"]:
            state["last_mail_ids"].add(mid)
            new_emails.append(em)
    return new_emails


async def fetch_email(client: httpx.AsyncClient, mail_id: str) -> dict:
    """Baca email penuh."""
    return await mail_api(client, {"f": "fetch_email", "email_id": mail_id})


# ═══════════════════════════════════════════
#  TELEGRAM API
# ═══════════════════════════════════════════
async def tg(client: httpx.AsyncClient, method: str, data: dict = None) -> dict:
    """Call Telegram Bot API."""
    try:
        resp = await client.post(f"{API_TG}/{method}", json=data or {}, timeout=30)
        result = resp.json()
        if not result.get("ok"):
            log.warning(f"TG {method} error: {result.get('description', 'unknown')}")
        return result
    except Exception as e:
        log.error(f"TG {method} exception: {e}")
        return {"ok": False}


def build_main_message() -> str:
    """Bina mesej utama dengan email + countdown."""
    email = state["email"] or "Loading..."
    remaining = max(0, int(state["rotate_at"] - time.time()))
    mins = remaining // 60
    secs = remaining % 60
    
    escaped_email = html.escape(email)
    
    text = (
        f"<blockquote>📬 <b>Temp Mail Bot</b>\n\n"
        f"📧 Email semasa:\n"
        f"<code>{escaped_email}</code>\n\n"
        f"⏳ Auto-tukar dalam: <b>{mins}m {secs:02d}s</b>\n\n"
        f"💡 Email ini akan ditukar secara automatik setiap 5 minit.\n"
        f"Tekan butang <b>🗑 Delete</b> untuk tukar segera.</blockquote>"
    )
    return text


def delete_keyboard() -> dict:
    """Inline keyboard dengan butang Delete merah (menggunakan emoji + color hint)."""
    # Telegram Bot API - gunakan destructive/red style button
    # Teknik: web_app atau callback with color emoji to signal red
    return {
        "inline_keyboard": [
            [
                {
                    "text": "🗑 DELETE & TUKAR EMAIL",
                    "callback_data": "delete_email",
                }
            ],
            [
                {
                    "text": "🔄 REFRESH INBOX",
                    "callback_data": "refresh_inbox",
                }
            ],
        ]
    }


async def send_main_msg(client: httpx.AsyncClient, chat_id: int) -> None:
    """Hantar atau edit mesej utama."""
    text = build_main_message()
    keyboard = delete_keyboard()

    old_msg_id = state["msg_ids"].get(chat_id)
    if old_msg_id:
        # Cuba edit mesej sedia ada
        result = await tg(client, "editMessageText", {
            "chat_id": chat_id,
            "message_id": old_msg_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": keyboard,
        })
        if result.get("ok"):
            return
        # Kalau gagal edit, hantar baru

    result = await tg(client, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": keyboard,
    })
    if result.get("ok"):
        state["msg_ids"][chat_id] = result["result"]["message_id"]


async def send_email_notification(client: httpx.AsyncClient, email_data: dict) -> None:
    """Hantar notifikasi email baru kepada semua chat."""
    sender = html.escape(str(email_data.get("mail_from", "Unknown")))
    subject = html.escape(str(email_data.get("mail_subject", "No Subject")))
    
    # Baca email penuh
    mail_id = str(email_data.get("mail_id", ""))
    body_text = ""
    if mail_id:
        full = await fetch_email(client, mail_id)
        raw_body = full.get("mail_body", "")
        # Bersihkan HTML tags dari body
        import re
        clean = re.sub(r"<[^>]+>", "", raw_body)
        clean = html.escape(clean.strip()[:1500])  # Limit 1500 chars
        body_text = clean if clean else "(Empty body)"
    
    text = (
        f"<blockquote>📩 <b>EMAIL BARU MASUK!</b>\n\n"
        f"👤 Dari: <b>{sender}</b>\n"
        f"📋 Subjek: <b>{subject}</b>\n\n"
        f"📄 Kandungan:\n{body_text}</blockquote>"
    )
    
    for chat_id in list(state["chat_ids"]):
        await tg(client, "sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        })


# ═══════════════════════════════════════════
#  UPDATE HANDLER
# ═══════════════════════════════════════════
async def handle_updates(client: httpx.AsyncClient) -> None:
    """Poll dan handle Telegram updates."""
    result = await tg(client, "getUpdates", {
        "offset": state["offset"],
        "timeout": 1,
        "allowed_updates": ["message", "callback_query"],
    })
    
    if not result.get("ok"):
        return
    
    updates = result.get("result", [])
    for update in updates:
        state["offset"] = update["update_id"] + 1
        
        # Handle /start command
        msg = update.get("message", {})
        if msg.get("text", "").startswith("/start"):
            chat_id = msg["chat"]["id"]
            state["chat_ids"].add(chat_id)
            log.info(f"User /start: chat_id={chat_id}")
            
            # Hantar welcome + email semasa
            if not state["email"]:
                await get_new_email(client)
            await send_main_msg(client, chat_id)
        
        # Handle callback (Delete button / Refresh)
        cb = update.get("callback_query", {})
        if cb:
            cb_data = cb.get("data", "")
            chat_id = cb["message"]["chat"]["id"]
            cb_id = cb["id"]
            state["chat_ids"].add(chat_id)
            
            if cb_data == "delete_email":
                # Answer callback dulu
                await tg(client, "answerCallbackQuery", {
                    "callback_query_id": cb_id,
                    "text": "🗑 Memadam & menukar email...",
                    "show_alert": False,
                })
                # Tukar email baru
                await get_new_email(client)
                # Update mesej untuk semua chat
                for cid in list(state["chat_ids"]):
                    await send_main_msg(client, cid)
                    
            elif cb_data == "refresh_inbox":
                await tg(client, "answerCallbackQuery", {
                    "callback_query_id": cb_id,
                    "text": "🔄 Memeriksa inbox...",
                    "show_alert": False,
                })
                new_emails = await check_inbox(client)
                if new_emails:
                    for em in new_emails:
                        await send_email_notification(client, em)
                else:
                    await tg(client, "answerCallbackQuery", {
                        "callback_query_id": cb_id,
                        "text": "📭 Tiada email baru.",
                        "show_alert": True,
                    })


# ═══════════════════════════════════════════
#  MAIN LOOP
# ═══════════════════════════════════════════
async def main():
    """Main event loop."""
    log.info("═" * 50)
    log.info("  TEMP MAIL TELEGRAM BOT - STARTING")
    log.info("═" * 50)
    
    if not BOT_TOKEN:
        log.error("BOT_TOKEN not set!")
        sys.exit(1)
    
    start_time = time.time()
    last_inbox_check = 0
    last_countdown_update = 0
    
    async with httpx.AsyncClient() as client:
        # Dapatkan email pertama
        await get_new_email(client)
        log.info(f"Bot started with email: {state['email']}")
        
        # Set bot commands
        await tg(client, "setMyCommands", {
            "commands": [{"command": "start", "description": "Dapatkan Temp Mail"}]
        })
        
        while state["running"]:
            try:
                now = time.time()
                elapsed = now - start_time
                
                # Check runtime limit (GitHub Actions safety)
                if elapsed >= MAX_RUNTIME:
                    log.info(f"Runtime limit reached ({elapsed:.0f}s). Shutting down.")
                    # Notify users
                    for chat_id in list(state["chat_ids"]):
                        await tg(client, "sendMessage", {
                            "chat_id": chat_id,
                            "text": "<blockquote>🔄 <b>Bot sedang restart...</b>\nSila tunggu sebentar.</blockquote>",
                            "parse_mode": "HTML",
                        })
                    break
                
                # 1. Handle Telegram updates
                await handle_updates(client)
                
                # 2. Auto-rotate email setiap 5 minit
                if now >= state["rotate_at"] and state["email"]:
                    log.info("Auto-rotating email...")
                    old_email = state["email"]
                    await get_new_email(client)
                    # Notify semua user
                    for chat_id in list(state["chat_ids"]):
                        await tg(client, "sendMessage", {
                            "chat_id": chat_id,
                            "text": (
                                f"<blockquote>🔄 <b>Email ditukar automatik!</b>\n\n"
                                f"❌ Lama: <s>{html.escape(old_email)}</s>\n"
                                f"✅ Baru: <code>{html.escape(state['email'])}</code></blockquote>"
                            ),
                            "parse_mode": "HTML",
                        })
                        await send_main_msg(client, chat_id)
                
                # 3. Auto-check inbox setiap 10 saat
                if now - last_inbox_check >= INBOX_CHECK_INTERVAL and state["email"]:
                    last_inbox_check = now
                    new_emails = await check_inbox(client)
                    for em in new_emails:
                        await send_email_notification(client, em)
                
                # 4. Update countdown setiap 30 saat
                if now - last_countdown_update >= 30 and state["chat_ids"]:
                    last_countdown_update = now
                    for chat_id in list(state["chat_ids"]):
                        await send_main_msg(client, chat_id)
                
                # Small delay to avoid hammering APIs
                await asyncio.sleep(1)
                
            except Exception as e:
                log.error(f"Main loop error: {e}")
                await asyncio.sleep(5)
    
    log.info("Bot shutdown complete.")


# ─── SIGNAL HANDLERS ───
def shutdown(sig, frame):
    log.info(f"Signal {sig} received. Shutting down...")
    state["running"] = False

signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)

if __name__ == "__main__":
    asyncio.run(main())
