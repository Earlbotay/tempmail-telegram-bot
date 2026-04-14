#!/usr/bin/env python3
"""
Telegram Temp Mail Bot (scrape web2.temp-mail.org)
- /start: Buat mailbox baru, papar email, mula polling
- Auto-rotate email setiap 5 minit
- Polling inbox setiap 5 saat (berterusan)
- Butang Delete & Tukar Email (MERAH)
- Dedup 100%: ingat mail_id untuk email semasa sahaja, reset bila tukar
- /start sentiasa berfungsi - cancel task lama, buat baru
- Semua mesej dalam <blockquote> HTML
- GitHub Actions: cron 5 jam
"""

import os
import sys
import re
import time
import signal
import asyncio
import logging
import hashlib
from html import unescape

import cloudscraper
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
TEMPMAIL_URL = "https://web2.temp-mail.org"
ROTATE_INTERVAL = 300  # 5 minit
POLL_INTERVAL = 5      # 5 saat
MAX_RUNTIME = 17880    # 4h58m untuk GitHub Actions

# ─── cloudscraper session (bypass cloudflare) ──────────────────────
scraper = cloudscraper.create_scraper(allow_brotli=True)

def tm_request(method: str, path: str, token: str = None, retries: int = 3):
    """Make request to temp-mail.org with retry."""
    url = f"{TEMPMAIL_URL}{path}"
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    
    for attempt in range(retries):
        try:
            if method == "POST":
                resp = scraper.post(url, headers=headers, timeout=30)
            else:
                resp = scraper.get(url, headers=headers, timeout=30)
            
            if resp.status_code == 200:
                return resp.json()
            else:
                log.warning(f"tm_request {method} {path} -> {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            log.warning(f"tm_request {method} {path} attempt {attempt+1} failed: {e}")
        
        if attempt < retries - 1:
            time.sleep(2 * (attempt + 1))
    
    return None

def create_mailbox():
    """Create new temp mailbox via web2.temp-mail.org."""
    data = tm_request("POST", "/mailbox")
    if data and data.get("mailbox") and data.get("token"):
        return {"email": data["mailbox"], "token": data["token"]}
    return None

def fetch_messages(token: str):
    """Fetch inbox messages."""
    data = tm_request("GET", "/messages", token=token)
    if data and isinstance(data.get("messages"), list):
        return data["messages"]
    return []

def fetch_message_detail(token: str, msg_id: str):
    """Fetch full message content."""
    data = tm_request("GET", f"/messages/{msg_id}", token=token)
    return data

def clean_html(html_str: str) -> str:
    """Strip HTML tags, return clean text."""
    if not html_str:
        return ""
    text = re.sub(r'<style[^>]*>.*?</style>', '', html_str, flags=re.DOTALL)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = unescape(text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

# ─── Per-user state ────────────────────────────────────────────────
user_data: dict = {}
# user_data[chat_id] = {
#   "email": str,
#   "token": str,
#   "seen_ids": set(),       # mail IDs dah dilihat (untuk dedup)
#   "created_at": float,     # timestamp mailbox dibuat
#   "msg_id": int,           # telegram message id untuk edit countdown
#   "poll_task": Task,       # asyncio task untuk polling
#   "rotate_task": Task,     # asyncio task untuk auto-rotate
# }

def cancel_user_tasks(chat_id: int):
    """Cancel semua running tasks untuk user."""
    ud = user_data.get(chat_id)
    if not ud:
        return
    for key in ("poll_task", "rotate_task"):
        task = ud.get(key)
        if task and not task.done():
            task.cancel()
            log.info(f"Cancelled {key} for {chat_id}")

def get_delete_keyboard():
    """Keyboard dengan butang DELETE merah sahaja."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🗑 DELETE & TUKAR EMAIL",
            callback_data="delete_email"
        )
    ]])

async def setup_new_email(chat_id: int, context: ContextTypes.DEFAULT_TYPE, is_first: bool = True):
    """Create mailbox baru dan setup polling + rotate."""
    # Cancel existing tasks
    cancel_user_tasks(chat_id)
    
    # Create new mailbox
    mailbox = None
    for attempt in range(3):
        mailbox = create_mailbox()
        if mailbox:
            break
        log.warning(f"create_mailbox attempt {attempt+1} failed for {chat_id}")
        await asyncio.sleep(2)
    
    if not mailbox:
        await context.bot.send_message(
            chat_id=chat_id,
            text="<blockquote>❌ Gagal mendapatkan email sementara.\nSila cuba /start sekali lagi.</blockquote>",
            parse_mode="HTML"
        )
        return
    
    email = mailbox["email"]
    token = mailbox["token"]
    now = time.time()
    
    # Init user data (reset seen_ids untuk email baru)
    user_data[chat_id] = {
        "email": email,
        "token": token,
        "seen_ids": set(),
        "created_at": now,
        "msg_id": None,
        "poll_task": None,
        "rotate_task": None,
    }
    
    # Kira countdown
    expire_time = now + ROTATE_INTERVAL
    mins = ROTATE_INTERVAL // 60
    
    text = (
        f"<blockquote>📧 Email Sementara Anda:\n\n"
        f"<code>{email}</code>\n\n"
        f"⏳ Auto-tukar dalam {mins} minit\n"
        f"📬 Inbox dipantau setiap {POLL_INTERVAL} saat</blockquote>"
    )
    
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=get_delete_keyboard()
    )
    user_data[chat_id]["msg_id"] = msg.message_id
    
    # Start polling dan rotate tasks
    ud = user_data[chat_id]
    ud["poll_task"] = asyncio.create_task(
        poll_inbox_loop(chat_id, context)
    )
    ud["rotate_task"] = asyncio.create_task(
        auto_rotate_loop(chat_id, context)
    )

async def poll_inbox_loop(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Polling inbox berterusan setiap POLL_INTERVAL saat."""
    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            
            ud = user_data.get(chat_id)
            if not ud or not ud.get("token"):
                return
            
            token = ud["token"]
            seen = ud["seen_ids"]
            
            try:
                messages = await asyncio.get_event_loop().run_in_executor(
                    None, fetch_messages, token
                )
            except Exception as e:
                log.warning(f"fetch_messages error for {chat_id}: {e}")
                continue
            
            if not messages:
                continue
            
            for msg in messages:
                mail_id = msg.get("_id", "")
                if not mail_id or mail_id in seen:
                    continue
                
                # Mark as seen SEGERA sebelum process (elak double)
                seen.add(mail_id)
                
                # Fetch full detail
                try:
                    detail = await asyncio.get_event_loop().run_in_executor(
                        None, fetch_message_detail, token, mail_id
                    )
                except Exception as e:
                    log.warning(f"fetch_detail error for {mail_id}: {e}")
                    continue
                
                if not detail:
                    continue
                
                sender = detail.get("from", "Unknown")
                subject = detail.get("subject", "No Subject")
                body_html = detail.get("bodyHtml", "")
                body_text = detail.get("bodyText", "")
                
                # Clean body - prefer text, fallback to cleaned html
                body = body_text.strip() if body_text and body_text.strip() else clean_html(body_html)
                if not body:
                    body = "(tiada kandungan)"
                
                # Trim body kalau terlalu panjang
                if len(body) > 3000:
                    body = body[:3000] + "\n... (dipotong)"
                
                # Escape HTML chars dalam body
                body = (body
                    .replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;"))
                
                notif = (
                    f"<blockquote>📩 EMAIL BARU\n\n"
                    f"Dari: {sender}\n"
                    f"Subjek: {subject}\n\n"
                    f"{body}</blockquote>"
                )
                
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=notif,
                        parse_mode="HTML",
                        disable_web_page_preview=True
                    )
                except Exception as e:
                    log.warning(f"send notif error for {chat_id}: {e}")
                    
    except asyncio.CancelledError:
        log.info(f"poll_inbox_loop cancelled for {chat_id}")
    except Exception as e:
        log.error(f"poll_inbox_loop error for {chat_id}: {e}")

async def auto_rotate_loop(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Auto-rotate email setiap ROTATE_INTERVAL saat dengan countdown updates."""
    try:
        ud = user_data.get(chat_id)
        if not ud:
            return
        
        created = ud["created_at"]
        
        # Update countdown setiap 30 saat
        while True:
            elapsed = time.time() - created
            remaining = ROTATE_INTERVAL - elapsed
            
            if remaining <= 0:
                break
            
            # Update countdown message
            mins_left = int(remaining) // 60
            secs_left = int(remaining) % 60
            
            if ud.get("msg_id"):
                text = (
                    f"<blockquote>📧 Email Sementara Anda:\n\n"
                    f"<code>{ud['email']}</code>\n\n"
                    f"⏳ Auto-tukar dalam {mins_left}m {secs_left}s\n"
                    f"📬 Inbox dipantau setiap {POLL_INTERVAL} saat</blockquote>"
                )
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=ud["msg_id"],
                        text=text,
                        parse_mode="HTML",
                        reply_markup=get_delete_keyboard()
                    )
                except Exception:
                    pass  # message might be deleted or unchanged
            
            # Sleep 30s or remaining time, whichever is shorter
            wait = min(30, remaining)
            await asyncio.sleep(wait)
        
        # Time's up - rotate!
        log.info(f"Auto-rotating email for {chat_id}")
        
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="<blockquote>🔄 Email telah tamat tempoh. Menjana email baru...</blockquote>",
                parse_mode="HTML"
            )
        except Exception:
            pass
        
        await setup_new_email(chat_id, context, is_first=False)
        
    except asyncio.CancelledError:
        log.info(f"auto_rotate_loop cancelled for {chat_id}")
    except Exception as e:
        log.error(f"auto_rotate_loop error for {chat_id}: {e}")

# ─── Handlers ──────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start - sentiasa berfungsi, buat email baru setiap kali."""
    chat_id = update.effective_chat.id
    log.info(f"/start from {chat_id}")
    await setup_new_email(chat_id, context, is_first=True)

async def btn_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle butang delete."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "delete_email":
        chat_id = query.message.chat_id
        log.info(f"Delete & tukar email for {chat_id}")
        
        try:
            await query.edit_message_text(
                text="<blockquote>🗑 Email dipadam. Menjana email baru...</blockquote>",
                parse_mode="HTML"
            )
        except Exception:
            pass
        
        await setup_new_email(chat_id, context, is_first=False)

# ─── Main ──────────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        log.error("BOT_TOKEN not set!")
        sys.exit(1)
    
    start_time = time.time()
    
    # Graceful shutdown
    def handle_signal(sig, frame):
        log.info(f"Signal {sig} received, shutting down...")
        sys.exit(0)
    
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    
    log.info("Starting Temp Mail Bot (web2.temp-mail.org scrape)...")
    
    # Test temp-mail.org connection
    test = create_mailbox()
    if test:
        log.info(f"temp-mail.org OK - test email: {test['email']}")
    else:
        log.warning("temp-mail.org test failed - will retry on /start")
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(btn_callback))
    
    # Runtime limiter untuk GitHub Actions
    async def check_runtime(context: ContextTypes.DEFAULT_TYPE):
        elapsed = time.time() - start_time
        if elapsed >= MAX_RUNTIME:
            log.info(f"Max runtime {MAX_RUNTIME}s reached, shutting down")
            # Cancel semua user tasks
            for cid in list(user_data.keys()):
                cancel_user_tasks(cid)
            os._exit(0)
    
    from telegram.ext import JobQueue
    app.job_queue.run_repeating(check_runtime, interval=60, first=60)
    
    log.info("Bot is running!")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"]
    )

if __name__ == "__main__":
    main()
