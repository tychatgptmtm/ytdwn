import asyncio
import logging
import os
import re
import shutil
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, Message
import yt_dlp

BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

MAX_VIDEO_BYTES = 45 * 1024 * 1024
MAX_DOCUMENT_BYTES = 49 * 1024 * 1024

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not found in environment variables")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("youtube_bot")

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

YOUTUBE_REGEX = re.compile(
    r"(https?://(?:www\.)?(?:youtube\.com/watch\?v=[^\s&]+|youtu\.be/[^\s?&]+|youtube\.com/shorts/[^\s?&]+)[^\s]*)",
    re.IGNORECASE,
)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/healthz"):
            self.send_response(200)
            self.send_header("Content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return


def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health server started on port %s", PORT)
    return server


def extract_youtube_url(text: str) -> str | None:
    if not text:
        return None
    match = YOUTUBE_REGEX.search(text)
    return match.group(1) if match else None


def _find_downloaded_file(unique_id: str) -> Path:
    matches = sorted(DOWNLOAD_DIR.glob(f"{unique_id}.*"))
    media_matches = [m for m in matches if m.suffix.lower() not in {".part", ".ytdl", ".temp"}]
    if media_matches:
        return media_matches[0]
    raise FileNotFoundError("Downloaded file not found")


def _progressive_opts(outtmpl: str) -> dict:
    return {
        "outtmpl": outtmpl,
        "format": "best[ext=mp4][vcodec!=none][acodec!=none]/best[protocol=https][vcodec!=none][acodec!=none]/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "restrictfilenames": True,
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 2,
        "http_headers": {
            "User-Agent": "Mozilla/5.0",
        },
    }


def _fallback_opts(outtmpl: str) -> dict:
    return {
        "outtmpl": outtmpl,
        "format": "best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "restrictfilenames": True,
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 2,
        "http_headers": {
            "User-Agent": "Mozilla/5.0",
        },
    }


def download_youtube_video(url: str) -> tuple[Path, str]:
    unique_id = str(uuid.uuid4())
    outtmpl = str(DOWNLOAD_DIR / f"{unique_id}.%(ext)s")
    errors: list[str] = []

    for attempt_name, opts_factory in (
        ("progressive", _progressive_opts),
        ("fallback", _fallback_opts),
    ):
        try:
            logger.info("Download attempt: %s | %s", attempt_name, url)
            with yt_dlp.YoutubeDL(opts_factory(outtmpl)) as ydl:
                info = ydl.extract_info(url, download=True)
                prepared = Path(ydl.prepare_filename(info))

            if prepared.exists() and prepared.suffix.lower() not in {".part", ".temp", ".ytdl"}:
                return prepared, info.get("title") or "video"

            found = _find_downloaded_file(unique_id)
            return found, info.get("title") or "video"
        except Exception as e:
            msg = f"{attempt_name}: {e}"
            errors.append(msg)
            logger.exception("Download failed: %s", msg)

    raise RuntimeError(" | ".join(errors))


async def safe_remove(path: Path | None):
    if not path:
        return
    try:
        if path.exists():
            path.unlink()
    except Exception:
        logger.exception("Failed to remove file: %s", path)


@dp.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(
        "Привет.\n\n"
        "Пришли ссылку на YouTube или Shorts, и я попробую скачать видео.\n\n"
        "Если файл небольшой — отправлю как видео. Если побольше — как файл."
    )


@dp.message(F.text)
async def youtube_handler(message: Message):
    url = extract_youtube_url(message.text)
    if not url:
        await message.answer("Пришли ссылку именно на YouTube или Shorts.")
        return

    status = await message.answer("Скачиваю видео...")
    video_path: Path | None = None

    try:
        video_path, title = await asyncio.to_thread(download_youtube_video, url)
        file_size = video_path.stat().st_size
        input_file = FSInputFile(video_path, filename=video_path.name)

        if file_size <= MAX_VIDEO_BYTES:
            await message.answer_video(video=input_file, caption=title[:900])
        elif file_size <= MAX_DOCUMENT_BYTES:
            await message.answer_document(document=input_file, caption=title[:900])
        else:
            await message.answer(
                "Видео скачалось, но файл слишком большой для отправки через Telegram."
            )

        await status.delete()
    except Exception as e:
        logger.exception("Handler error for URL %s", url)
        error_text = str(e)
        user_text = "Не получилось скачать видео."

        lowered = error_text.lower()
        if "sign in to confirm" in lowered or "not a bot" in lowered:
            user_text += "\nYouTube временно режет скачивание с сервера."
        elif "private video" in lowered:
            user_text += "\nЭто приватное видео."
        elif "unavailable" in lowered:
            user_text += "\nВидео недоступно."
        else:
            user_text += "\nПопробуй другую ссылку или повтори позже."

        await status.edit_text(user_text)
    finally:
        await safe_remove(video_path)


async def main():
    server = start_health_server()
    try:
        await dp.start_polling(bot)
    finally:
        server.shutdown()
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
