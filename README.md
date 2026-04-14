# 📬 Temp Mail Telegram Bot

Bot Telegram yang menyediakan email sementara (temporary email) secara automatik.

## Fungsi
- `/start` - Dapatkan email sementara
- Auto-rotate email setiap 5 minit
- Auto-check inbox setiap 10 saat
- Notifikasi email baru secara real-time
- Butang Delete untuk tukar email segera
- Butang Refresh untuk semak inbox manual
- Semua mesej dalam format blockquote HTML

## Tech Stack
- Python 3.12 + httpx
- GuerrillaMail API (temp email)
- Telegram Bot API (long polling)
- GitHub Actions (cron setiap 5 jam)

## Setup
1. Set GitHub Secrets:
   - `BOT_TOKEN` - Telegram Bot Token
   - `GH_PAT` - GitHub Personal Access Token
2. Enable workflow di tab Actions
3. Trigger manual atau tunggu cron schedule

## License
MIT
