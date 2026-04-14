#!/usr/bin/env python3
"""
Telegram Temp Mail Bot (temp-mail.org API)
- /start: Papar temp mail semasa dengan countdown 5 minit
- Auto-rotate email setiap 5 minit
- Polling inbox berterusan (setiap 5 saat)
- Butang Delete (MERAH - style:"destructive" Bot API 9.4)
- Dedup 100%: ingat mail_unique_id untuk email semasa sahaja
- Semua mesej dalam <blockquote> HTML
- GitHub Actions: cron 5 jam, auto-cancel run lama
"""

import os
import sys
import re
import time
import hashlib
import signal
import asyncio
import logging
import html
import random
import string
from datetime import datetime, timezone, timedelta

import httpx

# ─── CONFIG ───
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
if not BOT_TOKEN:
    print("FATAL: BOT_TOKEN not set")
    sys.exit(1)

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TEMPMAIL_API = "https://api.temp-mail.org/request"

ROTATE_INTERVAL = 300  # 5 minit
INBOX_POLL_INTERVAL = 5  # setiap 5 saat
MAX_RUNTIME = 4 * 3600 + 58 * 60  # 4 jam 58 minit

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tempmail-bot")

shutdown_event = asyncio.Event()


# ─── STATE PER USER ───
class UserState:
    __slots__ = ("email", "email_md5", "created_at", "seen_ids", "last_start_msg_id", "domain")

    def __init__(self):
        self.email: str = ""
        self.email_md5: str = ""
        self.created_at: float = 0.0
        self.seen_ids: set = set()
        self.last_start_msg_id: int = 0
        self.domain: str = ""


users: dict[int, UserState] = {}


def get_user(chat_id: int) -> UserState:
    if chat_id not in users:
        users[chat_id] = UserState()
    return users[chat_id]


# ─── TELEGRAM HELPERS ───
async def tg(client: httpx.AsyncClient, method: str, **kwargs) -> dict | None:
    for attempt in range(3):
        try:
            r = await client.post(f"{TG_API}/{method}", json=kwargs, timeout=30)
            data = r.json()
            if data.get("ok"):
                return data.get("result")
            err = data.get("description", "")
            # Rate limit
            if r.status_code == 429:
                retry = data.get("parameters", {}).get("retry_after", 3)
                log.warning("Rate limited, wait %ss", retry)
                await asyncio.sleep(retry)
                continue
            log.error("TG %s fail: %s", method, err)
            return None
        except Exception as e:
            log.error("TG %s error (attempt %d): %s", method, attempt + 1, e)
            await asyncio.sleep(2)
    return None


async def answer_cb(client: httpx.AsyncClient, cb_id: str, text: str = ""):
    await tg(client, "answerCallbackQuery", callback_query_id=cb_id, text=text)


# ─── TEMP-MAIL.ORG API ───
_domains_cache: list[str] = []


async def get_domains(client: httpx.AsyncClient) -> list[str]:
    global _domains_cache
    if _domains_cache:
        return _domains_cache
    for attempt in range(3):
        try:
            r = await client.get(f"{TEMPMAIL_API}/domains/format/json/", timeout=15)
            if r.status_code == 200:
                domains = r.json()
                if isinstance(domains, list) and domains:
                    _domains_cache = domains
                    log.info("Domains loaded: %s", domains)
                    return domains
        except Exception as e:
            log.error("get_domains attempt %d: %s", attempt + 1, e)
            await asyncio.sleep(2)
    return []


def generate_username(length: int = 10) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


async def create_email(client: httpx.AsyncClient) -> tuple[str, str, str]:
    """Return (email, md5_hash, domain) or ("","","") on failure."""
    domains = await get_domains(client)
    if not domains:
        return "", "", ""
    domain = random.choice(domains)
    username = generate_username()
    email = f"{username}{domain}"
    md5 = hashlib.md5(email.lower().encode()).hexdigest()
    log.info("Created email: %s (md5: %s)", email, md5)
    return email, md5, domain


async def check_inbox(client: httpx.AsyncClient, md5: str) -> list[dict]:
    try:
        r = await client.get(f"{TEMPMAIL_API}/mail/id/{md5}/format/json/", timeout=15)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                return data
        # 404 = no messages
        return []
    except Exception as e:
        log.error("check_inbox error: %s", e)
        return []


# ─── FORMAT HELPERS ───
def escape(text: str) -> str:
    return html.escape(str(text)) if text else ""


def time_left_str(created_at: float) -> str:
    elapsed = time.time() - created_at
    remaining = max(0, ROTATE_INTERVAL - elapsed)
    mins = int(remaining) // 60
    secs = int(remaining) % 60
    return f"{mins}m {secs:02d}s"


def make_start_text(state: UserState) -> str:
    tl = time_left_str(state.created_at)
    return (
        f"<blockquote><b>Temporary Email</b>\n\n"
        f"<code>{escape(state.email)}</code>\n\n"
        f"Auto-tukar dalam: <b>{tl}</b></blockquote>"
    )


def make_delete_keyboard():
    """Butang DELETE MERAH guna style:'destructive' (Bot API 9.4)"""
    return {
        "inline_keyboard": [
            [
                {
                    "text": "Delete & Tukar Email",
                    "callback_data": "delete_email",
                    "style": "destructive",
                }
            ]
        ]
    }


def format_email_msg(mail: dict) -> str:
    sender = escape(mail.get("mail_from", "Unknown"))
    subject = escape(mail.get("mail_subject", "(No Subject)"))
    preview = escape(mail.get("mail_preview", ""))
    text_body = mail.get("mail_text_only") or mail.get("mail_text") or ""
    # Truncate long bodies
    if len(text_body) > 1500:
        text_body = text_body[:1500] + "..."
    text_body = escape(text_body)

    return (
        f"<blockquote><b>Email Baru Diterima!</b>\n\n"
        f"<b>Dari:</b> {sender}\n"
        f"<b>Subjek:</b> {subject}\n"
        f"<b>Preview:</b> {preview}\n\n"
        f"<b>Isi:</b>\n{text_body}</blockquote>"
    )


# ─── CORE: ASSIGN EMAIL TO USER ───
async def assign_new_email(client: httpx.AsyncClient, chat_id: int) -> bool:
    state = get_user(chat_id)
    email, md5, domain = await create_email(client)
    if not email:
        await tg(
            client,
            "sendMessage",
            chat_id=chat_id,
            text="<blockquote>Gagal dapatkan email. Cuba lagi sebentar.</blockquote>",
            parse_mode="HTML",
        )
        return False
    state.email = email
    state.email_md5 = md5
    state.domain = domain
    state.created_at = time.time()
    state.seen_ids = set()  # Reset dedup untuk email baru
    return True


async def send_start_message(client: httpx.AsyncClient, chat_id: int):
    state = get_user(chat_id)
    text = make_start_text(state)
    kb = make_delete_keyboard()
    result = await tg(
        client,
        "sendMessage",
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=kb,
    )
    if result:
        state.last_start_msg_id = result.get("message_id", 0)


# ─── HANDLE /start ───
async def handle_start(client: httpx.AsyncClient, chat_id: int):
    state = get_user(chat_id)
    # Sentiasa buat email baru bila /start
    ok = await assign_new_email(client, chat_id)
    if ok:
        await send_start_message(client, chat_id)


# ─── HANDLE CALLBACK (DELETE) ───
async def handle_callback(client: httpx.AsyncClient, cb: dict):
    cb_id = cb.get("id", "")
    data = cb.get("data", "")
    msg = cb.get("message", {})
    chat_id = msg.get("chat", {}).get("id")

    if not chat_id:
        await answer_cb(client, cb_id, "Error")
        return

    if data == "delete_email":
        await answer_cb(client, cb_id, "Memadam & menukar email...")
        # Delete old start message button (edit to remove keyboard)
        old_msg_id = msg.get("message_id")
        if old_msg_id:
            state = get_user(chat_id)
            old_email = escape(state.email)
            await tg(
                client,
                "editMessageText",
                chat_id=chat_id,
                message_id=old_msg_id,
                text=f"<blockquote><s>{old_email}</s>\n<i>Dipadam</i></blockquote>",
                parse_mode="HTML",
            )
        ok = await assign_new_email(client, chat_id)
        if ok:
            await send_start_message(client, chat_id)


# ─── POLLING: TELEGRAM UPDATES ───
async def poll_updates(client: httpx.AsyncClient):
    offset = 0
    while not shutdown_event.is_set():
        try:
            r = await client.get(
                f"{TG_API}/getUpdates",
                params={"offset": offset, "timeout": 10, "allowed_updates": '["message","callback_query"]'},
                timeout=15,
            )
            data = r.json()
            if not data.get("ok"):
                await asyncio.sleep(2)
                continue
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                # Handle /start
                msg = update.get("message")
                if msg:
                    text = msg.get("text", "")
                    chat_id = msg.get("chat", {}).get("id")
                    if chat_id and text.strip().lower() in ("/start", "/start@"):
                        asyncio.create_task(handle_start(client, chat_id))
                # Handle callback
                cb = update.get("callback_query")
                if cb:
                    asyncio.create_task(handle_callback(client, cb))
        except httpx.ReadTimeout:
            continue
        except Exception as e:
            log.error("poll_updates error: %s", e)
            await asyncio.sleep(3)


# ─── POLLING: INBOX CHECK ───
async def poll_inbox(client: httpx.AsyncClient):
    while not shutdown_event.is_set():
        await asyncio.sleep(INBOX_POLL_INTERVAL)
        for chat_id, state in list(users.items()):
            if not state.email_md5:
                continue
            try:
                mails = await check_inbox(client, state.email_md5)
                for mail in mails:
                    uid = mail.get("mail_unique_id", "")
                    if not uid:
                        continue
                    # Dedup: skip kalau dah nampak
                    if uid in state.seen_ids:
                        continue
                    state.seen_ids.add(uid)
                    text = format_email_msg(mail)
                    await tg(client, "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")
            except Exception as e:
                log.error("poll_inbox error for %d: %s", chat_id, e)


# ─── AUTO-ROTATE EMAIL SETIAP 5 MINIT ───
async def auto_rotate(client: httpx.AsyncClient):
    while not shutdown_event.is_set():
        await asyncio.sleep(15)  # Check setiap 15 saat
        now = time.time()
        for chat_id, state in list(users.items()):
            if not state.email:
                continue
            elapsed = now - state.created_at
            if elapsed >= ROTATE_INTERVAL:
                log.info("Auto-rotate for chat %d", chat_id)
                old_email = escape(state.email)
                # Edit old message
                if state.last_start_msg_id:
                    await tg(
                        client,
                        "editMessageText",
                        chat_id=chat_id,
                        message_id=state.last_start_msg_id,
                        text=f"<blockquote><s>{old_email}</s>\n<i>Tamat tempoh (5 minit)</i></blockquote>",
                        parse_mode="HTML",
                    )
                ok = await assign_new_email(client, chat_id)
                if ok:
                    await send_start_message(client, chat_id)


# ─── COUNTDOWN UPDATER ───
async def update_countdown(client: httpx.AsyncClient):
    """Update countdown text setiap 30 saat."""
    while not shutdown_event.is_set():
        await asyncio.sleep(30)
        for chat_id, state in list(users.items()):
            if not state.email or not state.last_start_msg_id:
                continue
            text = make_start_text(state)
            kb = make_delete_keyboard()
            try:
                await tg(
                    client,
                    "editMessageText",
                    chat_id=chat_id,
                    message_id=state.last_start_msg_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
            except Exception:
                pass  # Ignore edit errors (message not modified)


# ─── MAIN ───
async def main():
    log.info("Bot starting...")
    start_time = time.time()

    # Delete webhook if any
    async with httpx.AsyncClient() as client:
        await tg(client, "deleteWebhook", drop_pending_updates=False)

    async with httpx.AsyncClient(http2=False) as client:
        # Verify bot
        me = await tg(client, "getMe")
        if me:
            log.info("Bot: @%s", me.get("username", "?"))
        else:
            log.error("Cannot connect to Telegram API!")
            return

        # Launch all tasks
        tasks = [
            asyncio.create_task(poll_updates(client)),
            asyncio.create_task(poll_inbox(client)),
            asyncio.create_task(auto_rotate(client)),
            asyncio.create_task(update_countdown(client)),
        ]

        # Shutdown timer
        async def shutdown_timer():
            while not shutdown_event.is_set():
                await asyncio.sleep(10)
                if time.time() - start_time >= MAX_RUNTIME:
                    log.info("Max runtime reached, shutting down...")
                    shutdown_event.set()

        tasks.append(asyncio.create_task(shutdown_timer()))

        # Signal handler
        def signal_handler(*_):
            log.info("Signal received, shutting down...")
            shutdown_event.set()

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        await shutdown_event.wait()
        for t in tasks:
            t.cancel()
        log.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
