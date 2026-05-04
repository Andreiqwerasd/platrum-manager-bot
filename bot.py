"""
Telegram-бот — личный Platrum-ассистент для руководителей.
Голос/текст/видеокружки → задача в Platrum от имени руководителя.
"""

import os
import json
import ssl
import socket
import re
import logging
import tempfile
import subprocess
from pathlib import Path
from datetime import datetime

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)
import anthropic

# ─────────────── CONFIG ───────────────
BOT_TOKEN     = os.environ['BOT_TOKEN']
ANTHROPIC_KEY = os.environ['ANTHROPIC_API_KEY']
PLATRUM_HOST  = 'a96a08a.platrum.ru'
DATA_DIR      = Path(os.environ.get('DATA_DIR', '/app/data'))
DATA_FILE     = DATA_DIR / 'users.json'

WAITING_KEY = 1

DATA_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# Whisper model (loaded once on first use)
_whisper_model = None

def get_whisper():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        log.info("Loading Whisper model 'base'...")
        _whisper_model = WhisperModel('base', device='cpu', compute_type='int8')
        log.info("Whisper model loaded.")
    return _whisper_model

# ─────────────── USER STORAGE ───────────────
def load_users() -> dict:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {}

def save_users(users: dict):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding='utf-8')

def get_user(user_id: int) -> dict | None:
    return load_users().get(str(user_id))

def set_user(user_id: int, data: dict):
    users = load_users()
    users[str(user_id)] = data
    save_users(users)

# ─────────────── PLATRUM API ───────────────
def _platrum_post(path: str, body: dict, api_key: str) -> dict:
    payload = json.dumps(body, ensure_ascii=False).encode('utf-8')
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = (
        f'POST {path} HTTP/1.1\r\n'
        f'Host: {PLATRUM_HOST}\r\n'
        f'Api-key: {api_key}\r\n'
        f'Content-Type: application/json\r\n'
        f'Content-Length: {len(payload)}\r\n'
        f'Connection: close\r\n\r\n'
    ).encode() + payload

    with socket.create_connection((PLATRUM_HOST, 443), timeout=30) as sock:
        with ctx.wrap_socket(sock, server_hostname=PLATRUM_HOST) as ssock:
            ssock.sendall(req)
            resp = b''
            while True:
                chunk = ssock.recv(8192)
                if not chunk:
                    break
                resp += chunk

    _, _, raw = resp.partition(b'\r\n\r\n')
    chunks = re.findall(rb'[0-9a-f]+\r\n(.+?)\r\n', raw, re.DOTALL)
    return json.loads(b''.join(chunks).decode() if chunks else raw.decode())

def verify_platrum_key(api_key: str) -> dict:
    """Verify API key by creating a test task; returns task data to get owner_user_id."""
    data = _platrum_post(
        '/tasks/api/task/create',
        {'name': '🤖 Тест подключения бота (можно удалить)'},
        api_key
    )
    if data.get('status') != 'success':
        raise ValueError(data.get('error_message') or data.get('error') or 'Неверный API-ключ')
    task = data['data']
    return {
        'owner_user_id': task['owner_user_id'],
        'test_task_id': task['id'],
    }

def create_platrum_task(task: dict, api_key: str) -> dict:
    """Create a task. Platrum auto-fills owner from API key."""
    body = {'name': task['name']}
    if task.get('description'):
        body['description'] = task['description']
    if task.get('finish_date'):
        body['finish_date'] = task['finish_date']
    if task.get('is_important'):
        body['is_important'] = True

    data = _platrum_post('/tasks/api/task/create', body, api_key)
    if data.get('status') != 'success':
        raise RuntimeError(data.get('error_message') or data.get('error') or 'Ошибка создания задачи')
    return data['data']

# ─────────────── TRANSCRIPTION ───────────────
def transcribe_audio(file_path: str) -> str:
    path = Path(file_path)
    mp3_path = path.with_suffix('.mp3')

    subprocess.run(
        ['ffmpeg', '-y', '-i', str(path), '-ar', '16000', '-ac', '1', str(mp3_path)],
        capture_output=True, check=True
    )

    model = get_whisper()
    segments, _ = model.transcribe(str(mp3_path), language='ru', beam_size=1)
    text = ' '.join(s.text for s in segments).strip()
    mp3_path.unlink(missing_ok=True)
    return text

# ─────────────── TASK PARSING ───────────────
def parse_task(text: str, user_name: str) -> dict:
    today = datetime.now().strftime('%Y-%m-%d')
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    msg = client.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=400,
        messages=[{'role': 'user', 'content': (
            f'Ты — помощник руководителя в компании по продаже б/у автозапчастей.\n'
            f'Пользователь: {user_name}. Сегодня: {today}.\n\n'
            f'Из текста извлеки задачу для CRM. Верни JSON:\n'
            f'{{"name":"краткое название (до 100 символов)",'
            f'"description":"подробности или null",'
            f'"finish_date":"YYYY-MM-DD или null",'
            f'"is_important":true/false}}\n\n'
            f'Текст: {text}\n\nВерни только JSON.'
        )}]
    )
    raw = msg.content[0].text.strip()
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    return json.loads(m.group() if m else raw)

# ─────────────── HANDLERS ───────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    user = get_user(user_id)

    if user:
        await update.message.reply_text(
            f"Привет, {user['name']}! 👋\n\n"
            "Отправь голосовое, видеокружок или текст — создам задачу в Platrum.\n\n"
            "/reset — сменить Platrum API-ключ\n"
            "/help — справка"
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "👋 Привет! Я создаю задачи в Platrum по голосу.\n\n"
        "Для начала нужен твой Platrum API-ключ.\n\n"
        "Как получить:\n"
        "1. Открой Platrum → Настройки (шестерёнка) → Интеграции и API → API ключи\n"
        "2. Нажми *«+ Добавить»*, назови «Бот» и сохрани\n"
        "3. Скопируй ключ и вставь его сюда",
        parse_mode='Markdown'
    )
    return WAITING_KEY

async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Введи новый Platrum API-ключ:")
    return WAITING_KEY

async def receive_api_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    key = update.message.text.strip()
    user_id = update.effective_user.id
    tg_name = update.effective_user.full_name

    if len(key) < 20 or ' ' in key:
        await update.message.reply_text("Это не похоже на API-ключ. Скопируй его из настроек Platrum и вставь сюда.")
        return WAITING_KEY

    msg = await update.message.reply_text("🔑 Проверяю ключ...")

    try:
        info = verify_platrum_key(key)
        set_user(user_id, {
            'api_key': key,
            'owner_user_id': info['owner_user_id'],
            'name': tg_name,
            'registered_at': datetime.now().isoformat(),
        })
        await msg.edit_text(
            f"✅ Готово, {tg_name}!\n\n"
            "Теперь отправляй голосовые, видеокружки или текст — "
            "буду создавать задачи в Platrum от твоего имени.\n\n"
            f"_(В Platrum создана тестовая задача #{info['test_task_id']} — можно удалить)_",
            parse_mode='Markdown'
        )
        log.info(f"User registered: {user_id} ({tg_name})")
    except Exception as e:
        await msg.edit_text(f"❌ Ключ не подошёл: {e}\n\nПроверь ключ и попробуй ещё раз.")
        return WAITING_KEY

    return ConversationHandler.END

async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user:
        await update.message.reply_text("Сначала введи /start и добавь Platrum API-ключ.")
        return

    msg = await update.message.reply_text("🎙 Распознаю...")

    try:
        voice = update.message.voice or update.message.video_note
        voice_file = await ctx.bot.get_file(voice.file_id)

        with tempfile.NamedTemporaryFile(suffix='.oga', delete=False, dir='/tmp') as f:
            tmp_path = f.name
        await voice_file.download_to_drive(tmp_path)

        text = transcribe_audio(tmp_path)
        Path(tmp_path).unlink(missing_ok=True)

        if not text.strip():
            await msg.edit_text("❌ Не удалось распознать речь. Попробуй ещё раз.")
            return

        await msg.edit_text(f"📝 Распознано: _{text}_\n\n⏳ Создаю задачу...", parse_mode='Markdown')
        await _create_and_reply(msg, text, user)

    except Exception as e:
        log.exception(f"Voice error {user_id}")
        await msg.edit_text(f"❌ Ошибка: {e}")

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user:
        await update.message.reply_text("Сначала введи /start и добавь Platrum API-ключ.")
        return

    text = update.message.text.strip()
    msg = await update.message.reply_text("⏳ Создаю задачу...")
    await _create_and_reply(msg, text, user)

async def _create_and_reply(msg, text: str, user: dict):
    try:
        task = parse_task(text, user['name'])
        result = create_platrum_task(task, user['api_key'])
        task_id = result['id']
        url = f"https://a96a08a.platrum.ru/tasks?taskId={task_id}"

        parts = [f"✅ *{task['name']}*"]
        if task.get('finish_date'):
            parts.append(f"📅 {task['finish_date']}")
        if task.get('is_important'):
            parts.append("🔴 Срочная")
        parts.append(f"[Открыть в Platrum]({url})")

        await msg.edit_text('\n'.join(parts), parse_mode='Markdown')
        log.info(f"Task {task_id} created by {user['name']}")
    except Exception as e:
        log.exception(f"Task error for {user.get('name')}")
        await msg.edit_text(f"❌ Не удалось создать задачу: {e}")

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Как пользоваться:\n\n"
        "• 🎙 Голосовое → задача\n"
        "• 🎥 Видеокружок → задача\n"
        "• 💬 Текст → задача\n\n"
        "Команды:\n"
        "/start — начало / статус\n"
        "/reset — сменить API-ключ\n"
        "/help — эта справка"
    )

# ─────────────── MAIN ───────────────
def main():
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler('start', cmd_start),
            CommandHandler('reset', cmd_reset),
        ],
        states={
            WAITING_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_api_key)],
        },
        fallbacks=[CommandHandler('start', cmd_start)],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler('help', cmd_help))
    app.add_handler(MessageHandler(filters.VOICE | filters.VIDEO_NOTE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info('Bot started')
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
