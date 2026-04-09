#!/usr/bin/env python3
"""Telegram bot: sticker → GIF converter with configurable size."""

import asyncio
import gzip
import logging
import os
import subprocess
import tempfile

from PIL import Image
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Load .env file if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

DEFAULT_SIZE = 512
MIN_SIZE = 64
MAX_SIZE = 1024


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_user_size(context: ContextTypes.DEFAULT_TYPE) -> tuple[int, int]:
    size = context.user_data.get("size", DEFAULT_SIZE)
    return size if isinstance(size, tuple) else (size, size)


# ---------------------------------------------------------------------------
# Conversion functions (run in thread pool — blocking I/O & CPU)
# ---------------------------------------------------------------------------

def _convert_webp(input_path: str, output_path: str, w: int, h: int) -> bool:
    """Static or animated WebP → GIF."""
    try:
        img = Image.open(input_path)
        frames: list[Image.Image] = []

        try:
            while True:
                frame = img.copy().convert("RGBA").resize((w, h), Image.LANCZOS)
                frames.append(frame)
                img.seek(img.tell() + 1)
        except EOFError:
            pass

        if not frames:
            return False

        if len(frames) == 1:
            frames[0].convert("P", palette=Image.ADAPTIVE).save(output_path, "GIF")
        else:
            frames[0].save(
                output_path,
                save_all=True,
                append_images=frames[1:],
                loop=0,
                optimize=False,
                format="GIF",
            )
        return True
    except Exception as e:
        logger.error("WebP → GIF: %s", e)
        return False


def _get_ffmpeg() -> str:
    """Return path to ffmpeg executable (bundled via imageio-ffmpeg or system)."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        pass
    return "ffmpeg"


def _convert_webm(input_path: str, output_path: str, w: int, h: int) -> bool:
    """Video sticker (.webm) → GIF via ffmpeg."""
    try:
        ffmpeg = _get_ffmpeg()
        scale = f"scale={w}:{h}:flags=lanczos"
        palette_path = output_path + ".palette.png"

        # Step 1: generate palette for high-quality GIF
        r1 = subprocess.run(
            [
                ffmpeg, "-y", "-i", input_path,
                "-vf", f"fps=25,{scale},palettegen=stats_mode=diff",
                palette_path,
            ],
            capture_output=True,
            timeout=60,
        )

        if r1.returncode == 0 and os.path.exists(palette_path):
            # Step 2: encode with palette
            r2 = subprocess.run(
                [
                    ffmpeg, "-y",
                    "-i", input_path,
                    "-i", palette_path,
                    "-filter_complex", f"fps=25,{scale}[v];[v][1:v]paletteuse=dither=bayer",
                    output_path,
                ],
                capture_output=True,
                timeout=60,
            )
            success = r2.returncode == 0
        else:
            # Fallback: no palette
            r3 = subprocess.run(
                [ffmpeg, "-y", "-i", input_path, "-vf", f"fps=25,{scale}", output_path],
                capture_output=True,
                timeout=60,
            )
            success = r3.returncode == 0

        if os.path.exists(palette_path):
            os.remove(palette_path)

        return success
    except Exception as e:
        logger.error("WebM → GIF: %s", e)
        return False


def _convert_tgs(input_path: str, output_path: str, w: int, h: int) -> bool:
    """Animated sticker (.tgs / lottie gzip) → GIF via rlottie-python."""
    try:
        import rlottie_python as rl  # pip install rlottie-python

        json_path = input_path + ".json"
        with gzip.open(input_path, "rb") as f_in, open(json_path, "wb") as f_out:
            f_out.write(f_in.read())

        anim = rl.LottieAnimation.from_file(json_path)
        total_frames: int = anim.lottie_animation_get_totalframe()
        fps: float = anim.lottie_animation_get_framerate()
        duration_ms = max(1, int(1000 / fps))

        frames: list[Image.Image] = []
        for i in range(total_frames):
            buf = anim.lottie_animation_render(i, w, h)
            # rlottie renders in ARGB32 (on most platforms → stored as BGRA bytes)
            img = Image.frombytes("RGBA", (w, h), bytes(buf))
            r, g, b, a = img.split()
            # Swap R↔B to convert BGRA → RGBA
            frames.append(Image.merge("RGBA", (b, g, r, a)))

        if os.path.exists(json_path):
            os.remove(json_path)

        if not frames:
            return False

        frames[0].save(
            output_path,
            save_all=True,
            append_images=frames[1:],
            loop=0,
            duration=duration_ms,
            optimize=False,
            format="GIF",
        )
        return True

    except ImportError:
        logger.error("rlottie-python not installed — run: pip install rlottie-python")
        return False
    except Exception as e:
        logger.error("TGS → GIF: %s", e)
        return False


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "Отправь мне стикер — получи GIF.\n\n"
    "Команды:\n"
    "  /size — текущий размер\n"
    "  /size <N> — квадрат N×N (например /size 256)\n"
    "  /size <W>x<H> — произвольный размер (например /size 320x240)\n\n"
    "Диапазон: {min}–{max} px\n"
    "По умолчанию: {default}×{default} px\n\n"
    "Поддерживаемые типы:\n"
    "  • Статичные .webp\n"
    "  • Анимированные .tgs (нужен rlottie-python)\n"
    "  • Видео .webm (нужен ffmpeg)"
).format(min=MIN_SIZE, max=MAX_SIZE, default=DEFAULT_SIZE)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    w, h = get_user_size(context)
    await update.message.reply_text(f"Привет!\n\n{HELP_TEXT}\n\nТекущий размер: {w}×{h} px")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


async def cmd_size(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        w, h = get_user_size(context)
        await update.message.reply_text(f"Текущий размер GIF: {w}×{h} px")
        return

    raw = context.args[0].strip().lower()
    try:
        if "x" in raw:
            w_s, h_s = raw.split("x", 1)
            w, h = int(w_s), int(h_s)
        else:
            w = h = int(raw)
    except ValueError:
        await update.message.reply_text(
            "Неверный формат.\n"
            "Примеры: /size 256   или   /size 320x240"
        )
        return

    if not (MIN_SIZE <= w <= MAX_SIZE and MIN_SIZE <= h <= MAX_SIZE):
        await update.message.reply_text(
            f"Размер должен быть от {MIN_SIZE} до {MAX_SIZE} px."
        )
        return

    context.user_data["size"] = (w, h)
    await update.message.reply_text(f"Размер установлен: {w}×{h} px")


async def handle_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sticker = update.message.sticker
    w, h = get_user_size(context)

    status = await update.message.reply_text("Конвертирую...")

    loop = asyncio.get_running_loop()

    with tempfile.TemporaryDirectory() as tmpdir:
        file_obj = await sticker.get_file()
        output_path = os.path.join(tmpdir, "output.gif")

        if sticker.is_video:
            input_path = os.path.join(tmpdir, "sticker.webm")
            await file_obj.download_to_drive(input_path)
            success = await loop.run_in_executor(
                None, _convert_webm, input_path, output_path, w, h
            )
            kind = "видео-стикер"
            tip = "Убедитесь, что ffmpeg установлен и доступен в PATH."

        elif sticker.is_animated:
            input_path = os.path.join(tmpdir, "sticker.tgs")
            await file_obj.download_to_drive(input_path)
            success = await loop.run_in_executor(
                None, _convert_tgs, input_path, output_path, w, h
            )
            kind = "анимированный стикер"
            tip = "Убедитесь, что rlottie-python установлен: pip install rlottie-python"

        else:
            input_path = os.path.join(tmpdir, "sticker.webp")
            await file_obj.download_to_drive(input_path)
            success = await loop.run_in_executor(
                None, _convert_webp, input_path, output_path, w, h
            )
            kind = "статичный стикер"
            tip = ""

        await status.delete()

        if success and os.path.exists(output_path):
            with open(output_path, "rb") as gif:
                await update.message.reply_document(
                    gif,
                    filename="sticker.gif",
                    caption=f"{kind} → GIF ({w}×{h} px)",
                )
        else:
            text = f"Не удалось конвертировать {kind}."
            if tip:
                text += f"\n{tip}"
            await update.message.reply_text(text)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "Токен не задан. Установите переменную окружения BOT_TOKEN.\n"
            "Пример: export BOT_TOKEN=1234567890:AABBcc..."
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("size", cmd_size))
    app.add_handler(MessageHandler(filters.Sticker.ALL, handle_sticker))

    logger.info("Бот запущен. Нажмите Ctrl+C для остановки.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
