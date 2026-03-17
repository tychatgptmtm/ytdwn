import asyncio
import logging
import os
import re
import shutil
import uuid
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, Message
import yt_dlp

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise ValueError("Не найден BOT_TOKEN в переменных окружения")

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Консервативные лимиты, чтобы бот стабильно работал.
SEND_AS_VIDEO_MAX_BYTES = 45 * 1024 * 1024
SEND_AS_DOCUMENT_MAX_BYTES = 49 * 1024 * 1024

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger(__name__)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

YOUTUBE_REGEX = re.compile(
    r"(https?://(?:www\.)?(?:youtube\.com/watch\?v=[^\s]+|youtu\.be/[^\s]+|youtube\.com/shorts/[^\s]+))",
    re.IGNORECASE,
)


# =========================
# HELPERS
# =========================
def extract_youtube_url(text: str) -> str | None:
    if not text:
        return None
    match = YOUTUBE_REGEX.search(text)
    return match.group(1) if match else None


def sanitize_caption(text: str | None) -> str:
    if not text:
        return "Готово."
    text = text.replace("<", "").replace(">", "")
    return text[:900]


def download_youtube_video(url: str) -> tuple[Path, dict]:
    unique_id = str(uuid.uuid4())
    outtmpl = str(DOWNLOAD_DIR / f"{unique_id}.%(ext)s")

    ydl_opts = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "windowsfilenames": True,
        "merge_output_format": "mp4",
        "format": "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4][height<=720]/best[height<=720]/best",
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        prepared = Path(ydl.prepare_filename(info))

        possible_files = []
        possible_mp4 = prepared.with_suffix(".mp4")
        possible_files.append(possible_mp4)
        possible_files.append(prepared)
        possible_files.extend(DOWNLOAD_DIR.glob(f"{unique_id}.*"))

        for file_path in possible_files:
            if file_path.exists() and file_path.is_file():
                return file_path, info

    raise FileNotFoundError("Скачанный файл не найден")


async def safe_remove(path: Path | None):
    if not path:
        return
    try:
        if path.exists():
            path.unlink()
    except Exception:
        logger.exception("Не удалось удалить файл: %s", path)


# =========================
# BOT HANDLERS
# =========================
@dp.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(
        "Привет.\n\n"
        "Пришли ссылку на YouTube или YouTube Shorts, и я попробую скачать видео.\n\n"
        "Если файл небольшой — отправлю как обычное видео.\n"
        "Если побольше — отправлю как файл."
    )


@dp.message(F.text)
async def youtube_handler(message: Message):
    url = extract_youtube_url(message.text)
    if not url:
        await message.answer("Пришли ссылку именно на YouTube или Shorts.")
        return

    status = await message.answer("Скачиваю видео...")
    file_path = None

    try:
        file_path, info = await asyncio.to_thread(download_youtube_video, url)

        if not file_path.exists():
            await status.edit_text("Файл не найден после скачивания.")
            return

        file_size = file_path.stat().st_size
        title = sanitize_caption(info.get("title"))
        upload = FSInputFile(file_path)

        if file_size <= SEND_AS_VIDEO_MAX_BYTES:
            await message.answer_video(
                video=upload,
                caption=title,
                supports_streaming=True,
            )
            await status.delete()
            return

        if file_size <= SEND_AS_DOCUMENT_MAX_BYTES:
            await message.answer_document(
                document=upload,
                caption=f"{title}\n\nОтправил как файл, потому что видео получилось крупнее обычного лимита.",
            )
            await status.delete()
            return

        size_mb = round(file_size / 1024 / 1024, 1)
        await status.edit_text(
            f"Видео скачалось, но файл слишком большой для отправки ботом: {size_mb} MB.\n"
            "Попробуй другое видео или короче ролик."
        )

    except Exception:
        logger.exception("Ошибка при обработке ссылки")
        await status.edit_text(
            "Не получилось скачать видео.\n"
            "Иногда ссылка временно недоступна или YouTube режет выдачу."
        )
    finally:
        await safe_remove(file_path)


# =========================
# HEALTH SERVER FOR RENDER
# =========================
async def health_handler(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "youtube-downloader-bot"})


async def root_handler(_: web.Request) -> web.Response:
    return web.Response(text="YouTube Telegram bot is running.")


async def run_web_server() -> None:
    app = web.Application()
    app.router.add_get("/", root_handler)
    app.router.add_get("/healthz", health_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()

    logger.info("Health server started on port %s", PORT)

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()


async def main() -> None:
    logger.info("Bot started")
    await asyncio.gather(
        dp.start_polling(bot),
        run_web_server(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
