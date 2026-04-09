#!/usr/bin/env python3
"""
TG bot + Discord bot — sticker → GIF pipeline.

TG команды:
  /start, /help   — справка
  /size [N|WxH]   — настройка размера GIF
  /server <ID>    — установить Discord-сервер для загрузки эмодзи
  /server off     — отключить загрузку эмодзи

Поведение:
  • Каждый стикер → GIF → отправляется в Discord-канал
  • Если задан сервер (/server), GIF также загружается как эмодзи на этот сервер.
    Если Discord отклоняет файл — бот уменьшает размер и повторяет попытку.
"""

import asyncio
import gzip
import io
import logging
import os
import subprocess
import sys
import tempfile
import time
from typing import Optional

import discord
from PIL import Image
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
# Заглушить мусор от httpx
logging.getLogger("httpx").setLevel(logging.WARNING)

# ── Config ────────────────────────────────────────────────────────────────────
TG_TOKEN           = os.environ.get("BOT_TOKEN", "")
DISCORD_TOKEN      = os.environ.get("DISCORD_TOKEN", "")
DISCORD_CHANNEL_ID = 1491384501777989644

DEFAULT_SIZE = 512
MIN_SIZE     = 64
MAX_SIZE     = 1024
EMOJI_LIMIT  = 256 * 1024   # 256 KB

# ── Shared state ──────────────────────────────────────────────────────────────
# gif_queue: каждый элемент — dict {"gif": bytes, "filename": str}
gif_queue: asyncio.Queue
emoji_server_id: Optional[int] = None   # None = не загружать эмодзи

_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

def _load_state() -> None:
    global emoji_server_id
    try:
        import json
        with open(_STATE_FILE, "r") as f:
            data = json.load(f)
        emoji_server_id = data.get("emoji_server_id")
        if emoji_server_id:
            logger.info("Загружен сервер для эмодзи: %s", emoji_server_id)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("Не удалось загрузить state.json: %s", e)

def _save_state() -> None:
    import json
    try:
        with open(_STATE_FILE, "w") as f:
            json.dump({"emoji_server_id": emoji_server_id}, f)
    except Exception as e:
        logger.warning("Не удалось сохранить state.json: %s", e)

# ── GIF helpers ───────────────────────────────────────────────────────────────

def _get_ffmpeg() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return "ffmpeg"


def _extract_gif_frames(src: Image.Image) -> tuple[list[Image.Image], list[int]]:
    """Extract fully composited RGBA frames and their durations from a GIF.

    Handles both delta-encoded frames (disposal=0/1, composite onto canvas)
    and full frames (disposal=2, treat each independently).
    """
    frames: list[Image.Image] = []
    durations: list[int] = []
    canvas = Image.new("RGBA", src.size, (0, 0, 0, 0))
    prev_canvas = canvas.copy()
    idx = 0
    while True:
        try:
            src.seek(idx)
        except EOFError:
            break
        durations.append(src.info.get("duration", 100))
        disposal = src.info.get("disposal", 0)   # 0/1=keep, 2=clear bg, 3=restore prev
        frame_rgba = src.convert("RGBA")
        work = canvas.copy()
        work.paste(frame_rgba, (0, 0), frame_rgba)
        frames.append(work)
        if disposal == 2:
            # Next frame starts on a clean transparent background
            canvas = Image.new("RGBA", src.size, (0, 0, 0, 0))
        elif disposal == 3:
            # Next frame starts on what was there before this frame
            canvas = prev_canvas.copy()
        else:
            # disposal 0 or 1: keep — next frame composites on current result
            prev_canvas = canvas.copy()
            canvas = work
        idx += 1
    return frames, durations


def _rgba_to_gif_frame(frame: Image.Image) -> Image.Image:
    """Convert an RGBA frame to a GIF-compatible palette image with transparency.

    GIF supports only 1-bit transparency (one palette index = transparent).
    We reserve palette index 255 for transparent pixels (alpha < 128).
    """
    alpha = frame.split()[3]
    # Quantize RGB to 255 colors, leaving index 255 free for transparency
    indexed = frame.convert("RGB").quantize(
        colors=255, method=Image.Quantize.FASTOCTREE, dither=0
    )
    pixels = list(indexed.getdata())
    alpha_px = list(alpha.getdata())
    pixels = [255 if a < 128 else p for p, a in zip(pixels, alpha_px)]
    indexed.putdata(pixels)
    indexed.info["transparency"] = 255
    return indexed


def _frames_to_gif(frames: list[Image.Image], durations: list[int]) -> bytes:
    """Convert RGBA frames to an animated GIF with transparency preserved."""
    buf = io.BytesIO()
    indexed = [_rgba_to_gif_frame(f) for f in frames]
    if len(indexed) == 1:
        indexed[0].save(buf, format="GIF")
    else:
        indexed[0].save(
            buf,
            format="GIF",
            save_all=True,
            append_images=indexed[1:],
            loop=0,
            duration=durations,
            optimize=False,
            disposal=2,   # clear to transparent before each frame
        )
    return buf.getvalue()


def _resize_gif(data: bytes, w: int, h: int) -> bytes:
    """Resize all frames of a GIF to w×h, preserving animation and timing."""
    src = Image.open(io.BytesIO(data))
    frames, durations = _extract_gif_frames(src)
    if not frames:
        return data
    resized = [f.resize((w, h), Image.LANCZOS) for f in frames]
    return _frames_to_gif(resized, durations)


def _convert_webp(input_path: str, w: int, h: int) -> Optional[bytes]:
    try:
        img = Image.open(input_path)
        # WebP animated frames are full frames (not deltas), but we still
        # need to extract durations and resize properly.
        frames: list[Image.Image] = []
        durations: list[int] = []
        try:
            while True:
                durations.append(img.info.get("duration", 100))
                frames.append(img.copy().convert("RGBA").resize((w, h), Image.LANCZOS))
                img.seek(img.tell() + 1)
        except EOFError:
            pass

        if not frames:
            return None

        return _frames_to_gif(frames, durations)
    except Exception as e:
        logger.error("WebP → GIF: %s", e)
        return None


def _convert_webm(input_path: str, w: int, h: int) -> Optional[bytes]:
    try:
        import tempfile as tf
        with tf.NamedTemporaryFile(suffix=".gif", delete=False) as tmp:
            output_path = tmp.name

        ffmpeg  = _get_ffmpeg()
        scale   = f"scale={w}:{h}:flags=lanczos"
        palette = output_path + ".palette.png"

        r1 = subprocess.run(
            [ffmpeg, "-y", "-i", input_path,
             "-vf", f"fps=25,{scale},palettegen=stats_mode=diff", palette],
            capture_output=True, timeout=60,
        )
        if r1.returncode == 0 and os.path.exists(palette):
            r2 = subprocess.run(
                [ffmpeg, "-y", "-i", input_path, "-i", palette,
                 "-filter_complex", f"fps=25,{scale}[v];[v][1:v]paletteuse=dither=bayer",
                 output_path],
                capture_output=True, timeout=60,
            )
            ok = r2.returncode == 0
        else:
            r3 = subprocess.run(
                [ffmpeg, "-y", "-i", input_path, "-vf", f"fps=25,{scale}", output_path],
                capture_output=True, timeout=60,
            )
            ok = r3.returncode == 0

        for p in [palette]:
            if os.path.exists(p):
                os.remove(p)

        if ok and os.path.exists(output_path):
            with open(output_path, "rb") as f:
                data = f.read()
            os.remove(output_path)
            return data
        return None
    except Exception as e:
        logger.error("WebM → GIF: %s", e)
        return None


def _tgs_bytes_to_gif(tgs_bytes: bytes, w: int, h: int) -> bytes:
    """Convert TGS bytes → GIF bytes inline (no subprocess) via rlottie-python."""
    import gzip
    import rlottie_python as rl

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        json_path = tmp.name
    try:
        with gzip.open(io.BytesIO(tgs_bytes), "rb") as f:
            with open(json_path, "wb") as out:
                out.write(f.read())

        anim = rl.LottieAnimation.from_file(json_path)
        total_frames: int = anim.lottie_animation_get_totalframe()
        fps: float = anim.lottie_animation_get_framerate()
        duration_ms = max(1, int(1000 / fps))

        frames = []
        for i in range(total_frames):
            buf = anim.lottie_animation_render(i, w, h)
            img = Image.frombytes("RGBA", (w, h), bytes(buf))
            r, g, b, a = img.split()
            frames.append(Image.merge("RGBA", (b, g, r, a)))  # BGRA → RGBA
    finally:
        if os.path.exists(json_path):
            os.remove(json_path)

    if not frames:
        raise RuntimeError("rlottie вернул 0 кадров")

    return _frames_to_gif(frames, [duration_ms] * len(frames))


def _convert_tgs(input_path: str, w: int, h: int) -> Optional[bytes]:
    """Animated sticker (.tgs) → GIF.
    Runs rlottie in a separate subprocess to isolate segfaults from the main process.
    """
    try:
        import tempfile as tf
        with tf.NamedTemporaryFile(suffix=".gif", delete=False) as tmp:
            output_path = tmp.name

        converter = os.path.join(os.path.dirname(os.path.abspath(__file__)), "convert_tgs.py")
        result = subprocess.run(
            [sys.executable, converter, input_path, str(w), str(h), output_path],
            capture_output=True,
            timeout=40,
        )
        stderr = result.stderr.decode(errors="replace").strip()
        stdout = result.stdout.decode(errors="replace").strip()
        if result.returncode == 0 and os.path.exists(output_path):
            logger.info("TGS subprocess: %s", stderr)
            with open(output_path, "rb") as f:
                data = f.read()
            os.remove(output_path)
            return data
        else:
            detail = stderr or stdout or f"exit code {result.returncode}"
            logger.error("TGS subprocess failed (exit %d): %s", result.returncode, detail)
            raise RuntimeError(detail)
    except subprocess.TimeoutExpired:
        raise RuntimeError("TGS conversion timed out")
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(str(e)) from e

def _prepare_emoji_gif(data: bytes, size: int, frame_step: int = 1) -> bytes:
    """Resize GIF to size×size, optionally keeping only every frame_step-th frame."""
    src = Image.open(io.BytesIO(data))
    frames, durations = _extract_gif_frames(src)
    if not frames:
        return data

    if frame_step > 1:
        # Keep every Nth frame; merge durations so animation speed stays correct
        merged_frames = []
        merged_durations = []
        for i in range(0, len(frames), frame_step):
            merged_frames.append(frames[i])
            merged_durations.append(sum(durations[i:i + frame_step]))
        frames, durations = merged_frames, merged_durations

    resized = [f.resize((size, size), Image.LANCZOS) for f in frames]
    return _frames_to_gif(resized, durations)


# ── Discord bot ───────────────────────────────────────────────────────────────

class DiscordClient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.message_content = True
        intents.messages = True
        super().__init__(intents=intents)
        self._monitored_channels: set[int] = set()

    async def on_ready(self):
        logger.info("Discord бот запущен как %s", self.user)
        self.loop.create_task(self._process_queue())

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        # !stic — включить/выключить мониторинг канала
        if message.content.strip().lower() == "!stic":
            cid = message.channel.id
            if cid in self._monitored_channels:
                self._monitored_channels.discard(cid)
                await message.channel.send("Мониторинг TGS в этом канале **отключён**.")
                logger.info("Мониторинг отключён для канала %s", cid)
            else:
                self._monitored_channels.add(cid)
                await message.channel.send(
                    "Мониторинг TGS в этом канале **включён**. "
                    "Отправляй `.tgs` файлы — получишь GIF и эмодзи на сервер."
                )
                logger.info("Мониторинг включён для канала %s", cid)
            return

        # Обработка TGS вложений в мониторируемых каналах
        if message.channel.id not in self._monitored_channels:
            return
        if not message.attachments:
            return

        for attachment in message.attachments:
            if not attachment.filename.lower().endswith(".tgs"):
                continue
            await self._handle_tgs_attachment(message, attachment)

    async def _handle_tgs_attachment(
        self, message: discord.Message, attachment: discord.Attachment
    ):
        loop = asyncio.get_running_loop()
        status = await message.channel.send(f"Конвертирую `{attachment.filename}`...")
        try:
            tgs_bytes = await attachment.read()
            with tempfile.NamedTemporaryFile(suffix=".tgs", delete=False) as tmp:
                tmp.write(tgs_bytes)
                tmp_path = tmp.name
            try:
                # subprocess с timeout=45 — убивает процесс если rlottie завис
                gif_bytes = await asyncio.wait_for(
                    loop.run_in_executor(None, _convert_tgs, tmp_path, DEFAULT_SIZE, DEFAULT_SIZE),
                    timeout=45,
                )
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

            await status.delete()

            # Отправить GIF в канал
            await message.channel.send(
                file=discord.File(io.BytesIO(gif_bytes), filename="sticker.gif")
            )

            # Загрузить как эмодзи на сервер, где отправлено сообщение
            if message.guild:
                emoji = await self._upload_emoji(message.guild, gif_bytes)
                if emoji:
                    await message.channel.send(f"Эмодзи добавлен: <:{emoji.name}:{emoji.id}>")
                else:
                    await message.channel.send("Не удалось создать эмодзи (слишком большой или нет прав).")

        except asyncio.TimeoutError:
            logger.error("TGS conversion timed out for %s", attachment.filename)
            await status.edit(content="Конвертация зависла (таймаут 45 сек). Попробуй другой файл.")
        except Exception as e:
            logger.error("_handle_tgs_attachment: %s", e, exc_info=True)
            await status.edit(content=f"Ошибка конвертации: ```{e}```")

    async def _process_queue(self):
        global emoji_server_id
        await self.wait_until_ready()
        while True:
            item: dict = await gif_queue.get()
            gif_bytes: bytes = item["gif"]
            filename: str    = item.get("filename", "sticker.gif")
            notify_cb        = item.get("notify")   # async callable(text) → sends TG reply

            # 1. Отправить GIF в канал
            channel = self.get_channel(DISCORD_CHANNEL_ID)
            if channel:
                try:
                    await channel.send(
                        file=discord.File(io.BytesIO(gif_bytes), filename=filename)
                    )
                    logger.info("GIF отправлен в Discord-канал")
                except discord.HTTPException as e:
                    logger.error("Ошибка отправки в канал: %s", e)
            else:
                logger.error("Канал %s не найден", DISCORD_CHANNEL_ID)

            # 2. Загрузить как эмодзи (если сервер задан)
            if emoji_server_id is not None:
                guild = self.get_guild(emoji_server_id)
                if guild:
                    emoji = await self._upload_emoji(guild, gif_bytes)
                    if notify_cb:
                        if emoji:
                            await notify_cb(f"Эмодзи создан на сервере {guild.name}: :{emoji.name}:")
                        else:
                            await notify_cb("Не удалось создать эмодзи (слишком большой даже в 32px или нет прав).")
                else:
                    logger.error("Сервер %s не найден", emoji_server_id)
                    if notify_cb:
                        await notify_cb(f"Сервер {emoji_server_id} не найден — проверь ID.")

    async def _upload_emoji(
        self, guild: discord.Guild, gif_bytes: bytes
    ) -> Optional[discord.Emoji]:
        """Пытается загрузить эмодзи, поэтапно снижая качество до тех пор, пока Discord не примет.

        Стратегия: начинаем с 5% от оригинального размера GIF (как на скриншоте ezgif),
        затем при необходимости снижаем дальше.
        """
        name = f"stic_{int(time.time())}"
        loop = asyncio.get_running_loop()

        # Вычисляем 5% от оригинального размера GIF
        try:
            src = Image.open(io.BytesIO(gif_bytes))
            orig_w, orig_h = src.size
            target_size = max(16, round((orig_w + orig_h) / 2 * 0.05))
            logger.info("Оригинальный размер GIF: %dx%d, цель эмодзи (5%%): %dpx", orig_w, orig_h, target_size)
        except Exception:
            target_size = 26  # fallback: 5% от 512px

        # Строим список кандидатов начиная с target_size, затем меньше
        def _make_candidates(start: int) -> list[tuple[int, int]]:
            sizes = sorted({start, start - 4, 32, 24, 16}, reverse=True)
            result = []
            for s in sizes:
                if s < 16:
                    continue
                result.append((s, 1))
                if s >= 24:
                    result.append((s, 2))
            return result

        candidates = _make_candidates(target_size)

        for size, step in candidates:
            data = await loop.run_in_executor(
                None, _prepare_emoji_gif, gif_bytes, size, step
            )
            label = f"{size}px step={step}"
            if len(data) > EMOJI_LIMIT:
                logger.info("Эмодзи %s = %d KB > лимит, пробуем меньше", label, len(data) // 1024)
                continue

            try:
                emoji = await guild.create_custom_emoji(name=name, image=data)
                logger.info("Эмодзи создан: :%s: (%s, %d KB)", name, label, len(data) // 1024)
                return emoji
            except discord.HTTPException as e:
                logger.warning("Discord отклонил %s: %s — пробуем меньше", label, e)
                continue

        logger.error("Не удалось создать эмодзи ни в каком варианте")
        return None


discord_client = DiscordClient()

# ── TG bot helpers ────────────────────────────────────────────────────────────

def _get_user_size(context: ContextTypes.DEFAULT_TYPE) -> tuple[int, int]:
    s = context.user_data.get("size", DEFAULT_SIZE)
    return s if isinstance(s, tuple) else (s, s)


HELP_TEXT = (
    "Отправь стикер или сообщение с кастомным эмодзи — получи GIF в Discord.\n\n"
    "Команды:\n"
    "  /size         — текущий размер GIF\n"
    "  /size 256     — квадрат 256×256 px\n"
    "  /size 320x240 — произвольный размер\n"
    "  /server <ID>  — загружать эмодзи на Discord-сервер\n"
    "  /server off   — отключить загрузку эмодзи\n\n"
    f"Диапазон: {MIN_SIZE}–{MAX_SIZE} px   |   по умолчанию: {DEFAULT_SIZE}×{DEFAULT_SIZE} px"
)

# ── TG handlers ───────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    w, h = _get_user_size(context)
    await update.message.reply_text(f"Привет!\n\n{HELP_TEXT}\n\nТекущий размер: {w}×{h} px")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


async def cmd_size(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        w, h = _get_user_size(context)
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
        await update.message.reply_text("Неверный формат. Пример: /size 256   или   /size 320x240")
        return

    if not (MIN_SIZE <= w <= MAX_SIZE and MIN_SIZE <= h <= MAX_SIZE):
        await update.message.reply_text(f"Размер должен быть от {MIN_SIZE} до {MAX_SIZE} px.")
        return

    context.user_data["size"] = (w, h)
    await update.message.reply_text(f"Размер установлен: {w}×{h} px")


async def cmd_server(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global emoji_server_id

    if not context.args:
        if emoji_server_id:
            await update.message.reply_text(
                f"Текущий сервер для эмодзи: {emoji_server_id}\n"
                "Сменить: /server <ID>   |   Отключить: /server off"
            )
        else:
            await update.message.reply_text(
                "Сервер для эмодзи не задан.\nУкажи: /server <Discord Server ID>"
            )
        return

    arg = context.args[0].strip().lower()
    if arg == "off":
        emoji_server_id = None
        _save_state()
        await update.message.reply_text("Загрузка эмодзи отключена.")
        return

    try:
        sid = int(arg)
    except ValueError:
        await update.message.reply_text("Неверный ID. Пример: /server 1234567890123456789")
        return

    # Проверим, знает ли Discord-бот этот сервер
    guild = discord_client.get_guild(sid)
    if guild:
        emoji_server_id = sid
        _save_state()
        await update.message.reply_text(
            f"Сервер установлен: {guild.name} ({sid})\n"
            "Каждый следующий стикер будет загружаться как эмодзи."
        )
    else:
        emoji_server_id = sid   # Устанавливаем в любом случае — бот может ещё не загрузить кэш
        _save_state()
        await update.message.reply_text(
            f"Сервер {sid} установлен.\n"
            "(Сервер не найден в кэше Discord-бота — убедись, что бот добавлен на этот сервер.)"
        )


async def _convert_sticker_obj(sticker, w: int, h: int) -> tuple[Optional[bytes], str]:
    """Download and convert any Sticker object → (gif_bytes, kind_label)."""
    loop = asyncio.get_running_loop()
    logger.info("sticker: is_video=%s is_animated=%s file_id=%s", sticker.is_video, sticker.is_animated, sticker.file_id)
    with tempfile.TemporaryDirectory() as tmpdir:
        logger.info("Скачиваю файл стикера...")
        file_obj = await sticker.get_file()
        if sticker.is_video:
            path = os.path.join(tmpdir, "s.webm")
            await file_obj.download_to_drive(path)
            logger.info("Скачан .webm (%d bytes), конвертирую...", os.path.getsize(path))
            gif = await loop.run_in_executor(None, _convert_webm, path, w, h)
            kind = "видео-стикер"
        elif sticker.is_animated:
            path = os.path.join(tmpdir, "s.tgs")
            await file_obj.download_to_drive(path)
            logger.info("Скачан .tgs (%d bytes), конвертирую...", os.path.getsize(path))
            try:
                gif = await loop.run_in_executor(None, _convert_tgs, path, w, h)
            except RuntimeError as e:
                logger.error("TGS failed: %s", e)
                gif = None
            kind = "анимированный стикер"
        else:
            path = os.path.join(tmpdir, "s.webp")
            await file_obj.download_to_drive(path)
            logger.info("Скачан .webp (%d bytes), конвертирую...", os.path.getsize(path))
            gif = await loop.run_in_executor(None, _convert_webp, path, w, h)
            kind = "статичный стикер"
    logger.info("Конвертация завершена: gif=%s kind=%s", "OK" if gif else "None", kind)
    return gif, kind


async def _send_gif(update: Update, gif: bytes, kind: str, w: int, h: int) -> None:
    """Send GIF back to TG user and put it in Discord queue."""
    await update.message.reply_document(
        io.BytesIO(gif),
        filename="sticker.gif",
        caption=f"{kind} → GIF ({w}×{h} px)",
    )
    msg = update.message

    async def notify(text: str):
        try:
            await msg.reply_text(text)
        except Exception:
            pass

    await gif_queue.put({"gif": gif, "filename": "sticker.gif", "notify": notify})


async def handle_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sticker = update.message.sticker
    w, h    = _get_user_size(context)
    status  = await update.message.reply_text("Конвертирую...")

    gif, kind = await _convert_sticker_obj(sticker, w, h)
    await status.delete()

    if not gif:
        await update.message.reply_text(f"Не удалось конвертировать {kind}.")
        return

    await _send_gif(update, gif, kind, w, h)


async def handle_custom_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle messages that contain Telegram custom emoji."""
    from telegram import MessageEntity

    message  = update.message
    # Проверяем entities и caption_entities (для сообщений с подписью)
    entities = list(message.entities or []) + list(message.caption_entities or [])

    emoji_ids = list({
        e.custom_emoji_id
        for e in entities
        if e.type == MessageEntity.CUSTOM_EMOJI
    })
    if not emoji_ids:
        return

    logger.info("Кастомные эмодзи: %s", emoji_ids)
    w, h   = _get_user_size(context)
    status = await message.reply_text(
        f"Конвертирую {len(emoji_ids)} эмодзи..." if len(emoji_ids) > 1 else "Конвертирую эмодзи..."
    )

    try:
        stickers = await context.bot.get_custom_emoji_stickers(emoji_ids)
        logger.info("Получено стикеров: %d", len(stickers))
    except Exception as e:
        await status.delete()
        logger.error("get_custom_emoji_stickers: %s", e)
        await message.reply_text(f"Ошибка при получении эмодзи: {e}")
        return

    await status.delete()

    if not stickers:
        await message.reply_text("Telegram не вернул стикеры для этих эмодзи.")
        return

    for sticker in stickers:
        try:
            gif, kind = await _convert_sticker_obj(sticker, w, h)
        except Exception as e:
            logger.error("_convert_sticker_obj: %s", e)
            await message.reply_text(f"Ошибка конвертации: {e}")
            continue

        if gif:
            await _send_gif(update, gif, f"кастомный эмодзи ({kind})", w, h)
        else:
            await message.reply_text(f"Не удалось конвертировать эмодзи ({kind}).")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("PTB error: %s", context.error, exc_info=context.error)
    if isinstance(update, Update) and update.message:
        try:
            await update.message.reply_text(f"Внутренняя ошибка: {context.error}")
        except Exception:
            pass

# ── Entry point ───────────────────────────────────────────────────────────────

async def run_tg(app: Application) -> None:
    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("Telegram бот запущен")
        await asyncio.Event().wait()   # ждём бесконечно


async def main() -> None:
    global gif_queue

    if not TG_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в .env")
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN не задан в .env")

    gif_queue = asyncio.Queue()
    _load_state()

    tg_app = Application.builder().token(TG_TOKEN).build()
    tg_app.add_handler(CommandHandler("start",  cmd_start))
    tg_app.add_handler(CommandHandler("help",   cmd_help))
    tg_app.add_handler(CommandHandler("size",   cmd_size))
    tg_app.add_handler(CommandHandler("server", cmd_server))
    tg_app.add_handler(MessageHandler(filters.Sticker.ALL, handle_sticker))
    tg_app.add_handler(MessageHandler(filters.Entity("custom_emoji"), handle_custom_emoji))
    tg_app.add_error_handler(error_handler)

    try:
        await asyncio.gather(
            run_tg(tg_app),
            discord_client.start(DISCORD_TOKEN),
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if not discord_client.is_closed():
            await discord_client.close()


if __name__ == "__main__":
    asyncio.run(main())
