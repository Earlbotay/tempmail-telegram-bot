#!/usr/bin/env python3
"""
Telegram Temp Mail Bot (scrape web2.temp-mail.org)
- /start: Tunjuk email semasa (buat baru kalau belum ada)
- Auto-rotate email setiap 5 minit
- Polling inbox setiap 5 saat (berterusan)
- Butang TUKAR EMAIL (merah/destructive)
- Dedup: ingat message id untuk session semasa, reset bila tukar email
- /start sentiasa berfungsi
- GitHub Actions: cron 5 jam
"""

import os
import sys
import time
import signal
import asyncio
import logging
import traceback

import cloudscraper
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler
from telegram.constants import ParseMode

# ── Config ──────────────────────────────────────────────────────────────────
TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_CHAT_ID = int(os.environ.get("CHAT_ID", "0"))
ROTATE_INTERVAL = 300  # 5 minit
POLL_INTERVAL = 5      # 5 saat
COUNTDOWN_UPDATE = 30  # update countdown setiap 30 saat
MAX_RUNTIME = 17700    # 4 jam 55 minit

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tempmail")

# ── Global State ────────────────────────────────────────────────────────────
scraper = cloudscraper.create_scraper()

current_mailbox = ""
current_token = ""
seen_ids: set[str] = set()
mail_created_at: float = 0.0
status_message_id: int = 0
bg_task: asyncio.Task | None = None
bot_instance: Bot | None = None


# ── Temp-Mail.org Functions ─────────────────────────────────────────────────
def create_mailbox() -> tuple[str, str]:
    """POST web2.temp-mail.org/mailbox → (email, token)"""
    for attempt in range(3):
        try:
            resp = scraper.post("https://web2.temp-mail.org/mailbox", timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                return data["mailbox"], data["token"]
            log.warning(f"create_mailbox attempt {attempt+1}: HTTP {resp.status_code}")
        except Exception as e:
            log.warning(f"create_mailbox attempt {attempt+1}: {e}")
        time.sleep(2)
    raise RuntimeError("Gagal buat mailbox selepas 3 cubaan")


def check_inbox(token: str) -> list[dict]:
    """GET web2.temp-mail.org/messages → list of messages"""
    try:
        resp = scraper.get(
            "https://web2.temp-mail.org/messages",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("messages", [])
        log.warning(f"check_inbox: HTTP {resp.status_code}")
    except Exception as e:
        log.warning(f"check_inbox: {e}")
    return []


def get_message_detail(token: str, msg_id: str) -> dict | None:
    """GET web2.temp-mail.org/messages/{id} → full message"""
    try:
        resp = scraper.get(
            f"https://web2.temp-mail.org/messages/{msg_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        log.warning(f"get_message_detail: {e}")
    return None


# ── Telegram Helpers ────────────────────────────────────────────────────────
def make_keyboard() -> InlineKeyboardMarkup:
    """Satu butang sahaja: TUKAR EMAIL"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 TUKAR EMAIL", callback_data="new_email")]
    ])


def format_status(email: str, created_at: float) -> str:
    """Format mesej status dengan countdown"""
    elapsed = time.time() - created_at
    remaining = max(0, ROTATE_INTERVAL - int(elapsed))
    mins = remaining // 60
    secs = remaining % 60
    return (
        f"<blockquote>"
        f"📧 <b>Temp Mail Aktif</b>\n\n"
        f"<code>{email}</code>\n\n"
        f"⏳ Auto-tukar dalam: <b>{mins}:{secs:02d}</b>\n"
        f"📬 Inbox dipantau setiap {POLL_INTERVAL} saat"
        f"</blockquote>"
    )


def format_email_notification(msg: dict) -> str:
    """Format notifikasi email masuk"""
    sender = msg.get("from", "Unknown")
    subject = msg.get("subject", "(Tiada Subjek)")
    # Cuba bodyText dulu, fallback bodyPreview
    body = msg.get("bodyText", "") or msg.get("bodyPreview", "") or msg.get("intro", "")
    if not body:
        body = "(Tiada kandungan)"
    # Trim body kalau terlalu panjang
    if len(body) > 3000:
        body = body[:3000] + "\n\n... (dipotong)"
    return (
        f"<blockquote>"
        f"📩 <b>Email Baru Diterima!</b>\n\n"
        f"👤 Dari: {_escape(sender)}\n"
        f"📋 Subjek: {_escape(subject)}\n\n"
        f"{'─' * 30}\n"
        f"{_escape(body)}"
        f"</blockquote>"
    )


def _escape(text: str) -> str:
    """Escape HTML characters"""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ── Core Logic ──────────────────────────────────────────────────────────────
async def setup_new_mailbox(bot: Bot, chat_id: int, edit_msg_id: int = 0) -> None:
    """Buat mailbox baru dan hantar/edit mesej status"""
    global current_mailbox, current_token, seen_ids, mail_created_at, status_message_id

    # Reset state
    seen_ids = set()
    mail_created_at = time.time()

    # Buat mailbox
    try:
        current_mailbox, current_token = create_mailbox()
    except RuntimeError as e:
        err_text = f"<blockquote>❌ {_escape(str(e))}\nSila cuba /start lagi.</blockquote>"
        if edit_msg_id:
            try:
                await bot.edit_message_text(err_text, chat_id=chat_id, message_id=edit_msg_id, parse_mode=ParseMode.HTML)
            except Exception:
                await bot.send_message(chat_id, err_text, parse_mode=ParseMode.HTML)
        else:
            await bot.send_message(chat_id, err_text, parse_mode=ParseMode.HTML)
        return

    log.info(f"Mailbox baru: {current_mailbox}")
    text = format_status(current_mailbox, mail_created_at)
    kb = make_keyboard()

    if edit_msg_id:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=edit_msg_id, parse_mode=ParseMode.HTML, reply_markup=kb)
            status_message_id = edit_msg_id
            return
        except Exception:
            pass

    msg = await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb)
    status_message_id = msg.message_id


async def background_loop(bot: Bot, chat_id: int) -> None:
    """Background loop: polling inbox + countdown update + auto-rotate"""
    global current_mailbox, current_token, seen_ids, mail_created_at, status_message_id

    last_countdown_update = 0.0

    while True:
        try:
            now = time.time()
            elapsed = now - mail_created_at

            # ── Auto-rotate ──
            if elapsed >= ROTATE_INTERVAL:
                log.info("Auto-rotate email...")
                await setup_new_mailbox(bot, chat_id, status_message_id)
                last_countdown_update = time.time()
                await asyncio.sleep(POLL_INTERVAL)
                continue

            # ── Update countdown setiap 30 saat ──
            if now - last_countdown_update >= COUNTDOWN_UPDATE and status_message_id:
                try:
                    text = format_status(current_mailbox, mail_created_at)
                    await bot.edit_message_text(
                        text,
                        chat_id=chat_id,
                        message_id=status_message_id,
                        parse_mode=ParseMode.HTML,
                        reply_markup=make_keyboard(),
                    )
                    last_countdown_update = now
                except Exception:
                    pass  # Message not modified is OK

            # ── Check inbox ──
            if current_token:
                messages = check_inbox(current_token)
                for msg in messages:
                    msg_id = msg.get("_id", msg.get("id", ""))
                    if msg_id and msg_id not in seen_ids:
                        seen_ids.add(msg_id)
                        # Cuba ambil detail penuh
                        detail = get_message_detail(current_token, msg_id)
                        email_data = detail if detail else msg
                        text = format_email_notification(email_data)
                        try:
                            await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
                        except Exception as e:
                            log.warning(f"Gagal hantar notifikasi: {e}")

        except asyncio.CancelledError:
            log.info("Background loop cancelled")
            return
        except Exception as e:
            log.error(f"Background loop error: {e}")

        await asyncio.sleep(POLL_INTERVAL)


# ── Handlers ────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context) -> None:
    """Handle /start - tunjuk email semasa, buat baru HANYA kalau belum ada"""
    global bg_task, bot_instance

    if not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    bot = context.bot
    bot_instance = bot

    log.info(f"/start dari chat {chat_id}")

    # Kalau dah ada email aktif + background loop masih jalan → tunjuk semula je
    if current_mailbox and current_token and bg_task and not bg_task.done():
        log.info(f"Email sedia ada: {current_mailbox}, tunjuk semula")
        text = format_status(current_mailbox, mail_created_at)
        kb = make_keyboard()
        msg = await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb)
        # Update status_message_id supaya countdown update mesej terbaru
        global status_message_id
        status_message_id = msg.message_id
        return

    # Belum ada email / background loop dah mati → buat baru
    # Cancel background task lama kalau ada
    if bg_task and not bg_task.done():
        bg_task.cancel()
        try:
            await bg_task
        except (asyncio.CancelledError, Exception):
            pass

    # Setup mailbox baru
    await setup_new_mailbox(bot, chat_id)

    # Mulakan background loop baru
    bg_task = asyncio.create_task(background_loop(bot, chat_id))


async def callback_handler(update: Update, context) -> None:
    """Handle butang callback"""
    global bg_task

    query = update.callback_query
    if not query:
        return

    await query.answer()
    chat_id = query.message.chat_id
    msg_id = query.message.message_id
    bot = context.bot

    if query.data == "new_email":
        log.info("Butang TUKAR EMAIL ditekan")

        # Cancel background task
        if bg_task and not bg_task.done():
            bg_task.cancel()
            try:
                await bg_task
            except (asyncio.CancelledError, Exception):
                pass

        # Edit mesej sedia ada → "Sedang menukar..."
        try:
            await bot.edit_message_text(
                "<blockquote>⏳ Sedang menukar email...</blockquote>",
                chat_id=chat_id,
                message_id=msg_id,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

        # Setup mailbox baru (edit mesej yang sama)
        await setup_new_mailbox(bot, chat_id, msg_id)

        # Mulakan background loop baru
        bg_task = asyncio.create_task(background_loop(bot, chat_id))


# ── Main ────────────────────────────────────────────────────────────────────
async def main() -> None:
    if not TOKEN:
        log.error("BOT_TOKEN not set!")
        sys.exit(1)

    log.info("Bot starting...")
    start_time = time.time()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Start polling
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    log.info(f"Bot running! Max runtime: {MAX_RUNTIME}s")

    # Run sampai MAX_RUNTIME
    try:
        while time.time() - start_time < MAX_RUNTIME:
            await asyncio.sleep(10)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass

    log.info("Shutting down...")
    await app.updater.stop()
    await app.stop()
    await app.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
