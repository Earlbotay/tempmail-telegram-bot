#!/usr/bin/env python3
"""
Telegram Temp Mail Bot
- /start: Papar temp mail semasa dengan countdown 5 minit
- Auto-rotate email setiap 5 minit
- Auto-check inbox setiap saat (polling berterusan)
- Butang Delete & Tukar Email (MERAH - style:"danger" Bot API 9.4)
- Dedup inbox: ingat semua email untuk session semasa sahaja
- Semua mesej dalam <blockquote> HTML
- Dijalankan di GitHub Actions (cron 5 jam)
"""

import os
import sys
import re
import time
import signal
import asyncio
import logging
import html as html_mod
from datetime import datetime, timezone

import httpx

# ─── CONFIG ───
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
API_MAIL = "https://api.guerrillamail.com/ajax.php"
ROTATE_INTERVAL = 300  # 5 minit
INBOX_POLL_INTERVAL = 5  # 5 saat - poll inbox berterusan
COUNTDOWN_UPDATE_INTERVAL = 30  # Update countdown display setiap 30s
MAX_RUNTIME = 5 * 3600 - 120  # 4h58m

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
    # Dedup: set of (mail_from, mail_subject, mail_excerpt) tuples
    # Hanya untuk email semasa - reset bila tukar email
    "seen_signatures": set(),
    "seen_mail_ids": set(),
    "msg_ids": {},  # chat_id -> message_id for edit
    "running": True,
    "offset": 0,
}


# ═══════════════════════════════════════════
#  GUERRILLAMAIL API
# ═══════════════════════════════════════════
async def mail_api(client: httpx.AsyncClient, params: dict) -> dict:
    if state["sid_token"]:
        params["sid_token"] = state["sid_token"]
    try:
        resp = await client.get(API_MAIL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if "sid_token" in data:
            state["sid_token"] = data["sid_token"]
        return data
    except httpx.TimeoutException:
        log.warning("Mail API timeout")
        return {}
    except httpx.HTTPStatusError as e:
        log.warning(f"Mail API HTTP {e.response.status_code}")
        return {}
    except Exception as e:
        log.error(f"Mail API error: {e}")
        return {}


async def get_new_email(client: httpx.AsyncClient) -> str:
    """Dapatkan email baru. Reset dedup untuk session baru."""
    # Forget session lama
    if state["sid_token"]:
        try:
            await mail_api(client, {"f": "forget_me"})
        except Exception:
            pass
        state["sid_token"] = None

    data = await mail_api(client, {"f": "get_email_address"})
    email = data.get("email_addr", "")
    if not email:
        log.error("Gagal dapatkan email baru, cuba lagi...")
        await asyncio.sleep(2)
        data = await mail_api(client, {"f": "get_email_address"})
        email = data.get("email_addr", "error@unknown")

    state["email"] = email
    state["rotate_at"] = time.time() + ROTATE_INTERVAL
    # RESET dedup - hanya ingat email untuk session semasa
    state["seen_signatures"] = set()
    state["seen_mail_ids"] = set()
    log.info(f"Email baru: {email}")
    return email


async def check_inbox(client: httpx.AsyncClient) -> list:
    """Semak inbox. Return hanya email BARU yang belum pernah dilihat."""
    data = await mail_api(client, {"f": "check_email", "seq": "0"})
    all_emails = data.get("list", [])
    if not isinstance(all_emails, list):
        return []

    new_emails = []
    for em in all_emails:
        mail_id = str(em.get("mail_id", ""))
        # Dedup by mail_id
        if mail_id in state["seen_mail_ids"]:
            continue
        # Dedup by content signature (100% match check)
        sig = (
            str(em.get("mail_from", "")).strip().lower(),
            str(em.get("mail_subject", "")).strip().lower(),
            str(em.get("mail_excerpt", "")).strip().lower(),
            str(em.get("mail_timestamp", "")),
        )
        if sig in state["seen_signatures"]:
            continue
        # Email ni baru!
        state["seen_mail_ids"].add(mail_id)
        state["seen_signatures"].add(sig)
        new_emails.append(em)

    return new_emails


async def fetch_email_body(client: httpx.AsyncClient, mail_id: str) -> str:
    """Baca email penuh, bersihkan HTML."""
    data = await mail_api(client, {"f": "fetch_email", "email_id": mail_id})
    raw = data.get("mail_body", "")
    # Bersihkan HTML
    clean = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
    clean = re.sub(r"<[^>]+>", "", clean)
    clean = clean.strip()
    if len(clean) > 1500:
        clean = clean[:1500] + "..."
    return html_mod.escape(clean) if clean else "(Tiada kandungan)"


# ═══════════════════════════════════════════
#  TELEGRAM API
# ═══════════════════════════════════════════
async def tg(client: httpx.AsyncClient, method: str, data: dict = None) -> dict:
    try:
        resp = await client.post(f"{API_TG}/{method}", json=data or {}, timeout=30)
        result = resp.json()
        if not result.get("ok"):
            desc = result.get("description", "unknown")
            # Jangan spam log untuk "message is not modified"
            if "message is not modified" not in desc:
                log.warning(f"TG {method}: {desc}")
        return result
    except httpx.TimeoutException:
        log.warning(f"TG {method} timeout")
        return {"ok": False}
    except Exception as e:
        log.error(f"TG {method} error: {e}")
        return {"ok": False}


def build_main_message() -> str:
    email = state["email"] or "Memuat..."
    remaining = max(0, int(state["rotate_at"] - time.time()))
    mins = remaining // 60
    secs = remaining % 60

    e = html_mod.escape(email)

    return (
        f"<blockquote>"
        f"<b>Temp Mail Bot</b>\n\n"
        f"Email semasa:\n"
        f"<code>{e}</code>\n\n"
        f"Auto-tukar dalam: <b>{mins}m {secs:02d}s</b>\n\n"
        f"Email ini ditukar automatik setiap 5 minit.\n"
        f"Tekan butang di bawah untuk tukar segera."
        f"</blockquote>"
    )


def build_keyboard() -> dict:
    """
    Inline keyboard: butang DELETE & TUKAR EMAIL (MERAH)
    Guna style: "danger" dari Bot API 9.4 (Feb 2026)
    """
    return {
        "inline_keyboard": [
            [
                {
                    "text": "DELETE & TUKAR EMAIL",
                    "callback_data": "delete_email",
                    "style": "danger",  # Bot API 9.4 - MERAH
                }
            ],
        ]
    }


async def send_or_update_main(client: httpx.AsyncClient, chat_id: int) -> None:
    """Hantar atau edit mesej utama."""
    text = build_main_message()
    kb = build_keyboard()

    old_msg = state["msg_ids"].get(chat_id)
    if old_msg:
        result = await tg(client, "editMessageText", {
            "chat_id": chat_id,
            "message_id": old_msg,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": kb,
        })
        if result.get("ok"):
            return
        # Gagal edit (mesej dah lama/delete) - hantar baru

    result = await tg(client, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": kb,
    })
    if result.get("ok"):
        state["msg_ids"][chat_id] = result["result"]["message_id"]


async def notify_new_email(client: httpx.AsyncClient, em: dict) -> None:
    """Hantar notifikasi email baru ke semua user."""
    sender = html_mod.escape(str(em.get("mail_from", "Unknown")))
    subject = html_mod.escape(str(em.get("mail_subject", "No Subject")))

    mail_id = str(em.get("mail_id", ""))
    body = "(Tiada kandungan)"
    if mail_id:
        body = await fetch_email_body(client, mail_id)

    text = (
        f"<blockquote>"
        f"<b>EMAIL BARU MASUK!</b>\n\n"
        f"Dari: <b>{sender}</b>\n"
        f"Subjek: <b>{subject}</b>\n\n"
        f"Kandungan:\n{body}"
        f"</blockquote>"
    )

    for chat_id in list(state["chat_ids"]):
        await tg(client, "sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        })


async def notify_rotation(client: httpx.AsyncClient, old_email: str) -> None:
    """Notify semua user tentang email rotation."""
    text = (
        f"<blockquote>"
        f"<b>Email ditukar automatik!</b>\n\n"
        f"Lama: <s>{html_mod.escape(old_email)}</s>\n"
        f"Baru: <code>{html_mod.escape(state['email'])}</code>"
        f"</blockquote>"
    )
    for chat_id in list(state["chat_ids"]):
        await tg(client, "sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        })
        await send_or_update_main(client, chat_id)


# ═══════════════════════════════════════════
#  UPDATE HANDLER
# ═══════════════════════════════════════════
async def handle_updates(client: httpx.AsyncClient) -> None:
    result = await tg(client, "getUpdates", {
        "offset": state["offset"],
        "timeout": 1,
        "allowed_updates": ["message", "callback_query"],
    })

    if not result.get("ok"):
        return

    for update in result.get("result", []):
        state["offset"] = update["update_id"] + 1

        try:
            # ─── /start ───
            msg = update.get("message", {})
            text = msg.get("text", "")
            if text.startswith("/start"):
                chat_id = msg["chat"]["id"]
                state["chat_ids"].add(chat_id)
                log.info(f"/start dari chat_id={chat_id}")

                # Sentiasa berfungsi: kalau belum ada email, dapatkan baru
                if not state["email"]:
                    await get_new_email(client)
                await send_or_update_main(client, chat_id)

            # ─── Callback: Delete & Tukar ───
            cb = update.get("callback_query")
            if cb:
                cb_data = cb.get("data", "")
                cb_id = cb["id"]
                chat_id = cb["message"]["chat"]["id"]
                state["chat_ids"].add(chat_id)

                if cb_data == "delete_email":
                    await tg(client, "answerCallbackQuery", {
                        "callback_query_id": cb_id,
                        "text": "Memadam & menukar email...",
                        "show_alert": False,
                    })
                    old = state["email"]
                    await get_new_email(client)
                    await notify_rotation(client, old or "")

        except Exception as e:
            log.error(f"Handle update error: {e}")


# ═══════════════════════════════════════════
#  MAIN LOOP
# ═══════════════════════════════════════════
async def main():
    log.info("=" * 50)
    log.info("  TEMP MAIL TELEGRAM BOT - STARTING")
    log.info("=" * 50)

    if not BOT_TOKEN:
        log.error("BOT_TOKEN not set!")
        sys.exit(1)

    start_time = time.time()
    last_inbox_check = 0
    last_countdown_update = 0

    async with httpx.AsyncClient() as client:
        # Email pertama
        await get_new_email(client)
        log.info(f"Bot started: {state['email']}")

        # Set bot commands
        await tg(client, "setMyCommands", {
            "commands": [{"command": "start", "description": "Dapatkan Temp Mail"}]
        })

        # Clear pending updates
        await tg(client, "getUpdates", {"offset": -1, "timeout": 0})
        state["offset"] = 0

        while state["running"]:
            try:
                now = time.time()

                # Runtime limit (GitHub Actions safety)
                if now - start_time >= MAX_RUNTIME:
                    log.info("Runtime limit. Shutting down.")
                    for cid in list(state["chat_ids"]):
                        await tg(client, "sendMessage", {
                            "chat_id": cid,
                            "text": "<blockquote><b>Bot sedang restart...</b>\nSila tunggu sebentar.</blockquote>",
                            "parse_mode": "HTML",
                        })
                    break

                # 1. Handle Telegram updates (sentiasa responsive)
                await handle_updates(client)

                # 2. Auto-rotate email setiap 5 minit
                if now >= state["rotate_at"] and state["email"]:
                    log.info("Auto-rotate email...")
                    old = state["email"]
                    await get_new_email(client)
                    await notify_rotation(client, old)

                # 3. Inbox polling berterusan (setiap 5 saat)
                if now - last_inbox_check >= INBOX_POLL_INTERVAL and state["email"]:
                    last_inbox_check = now
                    new_emails = await check_inbox(client)
                    for em in new_emails:
                        log.info(f"Email baru: {em.get('mail_subject', '?')}")
                        await notify_new_email(client, em)

                # 4. Update countdown display setiap 30 saat
                if now - last_countdown_update >= COUNTDOWN_UPDATE_INTERVAL and state["chat_ids"]:
                    last_countdown_update = now
                    for cid in list(state["chat_ids"]):
                        await send_or_update_main(client, cid)

                await asyncio.sleep(1)

            except Exception as e:
                log.error(f"Main loop error: {e}")
                await asyncio.sleep(3)

    log.info("Bot shutdown complete.")


# ─── SIGNAL HANDLERS ───
def shutdown(sig, frame):
    log.info(f"Signal {sig}. Shutting down...")
    state["running"] = False

signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)

if __name__ == "__main__":
    asyncio.run(main())
