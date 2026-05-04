"""
Telegram-бот — личный Platrum-ассистент для руководителей.
Голос/текст/видеокружки → умная обработка: задачи, поиск, комментарии, статусы, структура компании.
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
    data = _platrum_post(
        '/tasks/api/task/create',
        {'name': '🤖 Тест подключения бота (можно удалить)'},
        api_key
    )
    if data.get('status') != 'success':
        raise ValueError(data.get('error_message') or data.get('error') or 'Неверный API-ключ')
    task = data['data']
    return {'owner_user_id': task['owner_user_id'], 'test_task_id': task['id']}

def create_platrum_task(task: dict, api_key: str) -> dict:
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

def search_platrum_tasks(query: str, api_key: str) -> list:
    data = _platrum_post('/tasks/api/task/list', {'search': query}, api_key)
    if data.get('status') != 'success':
        return []
    result = data.get('data', [])
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return result.get('items', result.get('tasks', []))
    return []

def get_platrum_task(task_id: int, api_key: str) -> dict | None:
    data = _platrum_post('/tasks/api/task/get', {'id': task_id}, api_key)
    if data.get('status') != 'success':
        return None
    return data.get('data')

def add_platrum_comment(task_id: int, text: str, api_key: str) -> dict:
    data = _platrum_post('/tasks/api/tasks/comment/save', {'task_id': task_id, 'text': text}, api_key)
    if data.get('status') != 'success':
        raise RuntimeError(data.get('error_message') or 'Ошибка добавления комментария')
    return data['data']

def update_platrum_task_status(task_id: int, status_key: str, api_key: str) -> dict:
    data = _platrum_post('/tasks/api/task/update', {'id': task_id, 'status_key': status_key}, api_key)
    if data.get('status') != 'success':
        raise RuntimeError(data.get('error_message') or 'Ошибка изменения статуса')
    return data.get('data', {})

def get_company_structure(api_key: str) -> str:
    """Load org chart: blocks + active workers + profiles → formatted text."""
    blocks_resp = _platrum_post('/orgschema/api/block/list', {}, api_key)
    workers_resp = _platrum_post('/orgschema/api/worker/list-active', {}, api_key)
    profiles_resp = _platrum_post('/user/api/profile/list', {}, api_key)

    if blocks_resp.get('status') != 'success':
        raise RuntimeError('Не удалось загрузить структуру компании')

    blocks = blocks_resp.get('data', [])
    workers = workers_resp.get('data', []) if workers_resp.get('status') == 'success' else []
    profiles = profiles_resp.get('data', {}) if profiles_resp.get('status') == 'success' else {}

    # block_id → [names]
    block_people: dict[str, list] = {}
    for w in workers:
        bid = str(w.get('block_id', ''))
        uid = str(w.get('user_id', ''))
        p = profiles.get(uid, {})
        name = p.get('name') or p.get('user_name') or uid
        block_people.setdefault(bid, []).append(name)

    block_map = {str(b['id']): b for b in blocks}
    root_blocks = [b for b in blocks if not b.get('parent_id') or str(b.get('parent_id')) not in block_map]

    lines: list[str] = ['🏢 *Структура компании:*\n']

    def render(block, depth=0):
        prefix = '  ' * depth + ('• ' if depth > 0 else '')
        title = block.get('position') or block.get('name', '?')
        people = block_people.get(str(block['id']), [])
        person_str = ': ' + ', '.join(people) if people else ''
        lines.append(f'{prefix}*{title}*{person_str}')
        children = sorted(
            [b for b in blocks if str(b.get('parent_id')) == str(block['id'])],
            key=lambda b: b.get('order', 0)
        )
        for child in children:
            render(child, depth + 1)

    for block in sorted(root_blocks, key=lambda b: b.get('order', 0)):
        render(block)

    return '\n'.join(lines)

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

# ─────────────── INTENT CLASSIFICATION ───────────────
def classify_intent(text: str, user_name: str) -> dict:
    today = datetime.now().strftime('%Y-%m-%d')
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    msg = client.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=700,
        messages=[{'role': 'user', 'content': (
            f'Ты — помощник руководителя в компании по продаже б/у автозапчастей.\n'
            f'Пользователь: {user_name}. Сегодня: {today}.\n\n'
            f'Определи что хочет пользователь и верни JSON:\n'
            f'{{\n'
            f'  "intent": "create_task" | "find_task" | "add_comment" | "change_status" | "company_info" | "chat",\n'
            f'  "task_query": "название или ключевые слова задачи (для find_task, add_comment, change_status)",\n'
            f'  "task_id": число или null (если назвал конкретный номер задачи),\n'
            f'  "comment_text": "текст комментария (только для add_comment)",\n'
            f'  "new_status": "open" | "in_progress" | "done" | "cancelled" (только для change_status),\n'
            f'  "company_query": "что именно хочет узнать о структуре (для company_info)",\n'
            f'  "chat_response": "краткий ответ (только для chat)",\n'
            f'  "task": {{"name": "до 100 символов", "description": "подробности или null", "finish_date": "YYYY-MM-DD или null", "is_important": true/false}}\n'
            f'}}\n\n'
            f'Правила:\n'
            f'- create_task: нужно СДЕЛАТЬ что-то ("позвонить X", "проверить Y", "написать Z")\n'
            f'- find_task: НАЙТИ задачу / получить ссылку / узнать статус ("пришли ссылку", "найди задачу", "какой статус")\n'
            f'- add_comment: добавить КОММЕНТАРИЙ к задаче ("напиши в задачу", "добавь комментарий")\n'
            f'- change_status: изменить СТАТУС задачи ("отметь как выполненную", "переведи в работу", "закрой задачу", "отмени")\n'
            f'- company_info: структура компании, кто занимает какую должность ("кто руководитель", "покажи структуру", "кто менеджер по продажам")\n'
            f'- chat: вопрос, разговор, всё остальное\n\n'
            f'Текст: {text}\n\nВерни только JSON.'
        )}]
    )
    raw = msg.content[0].text.strip()
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    return json.loads(m.group() if m else raw)

STATUS_LABELS = {
    'open': 'Открыта',
    'in_progress': 'В работе',
    'done': 'Выполнена',
    'cancelled': 'Отменена',
    'closed': 'Закрыта',
}

# ─────────────── HANDLERS ───────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    user = get_user(user_id)

    if user:
        await update.message.reply_text(
            f"Привет, {user['name']}! 👋\n\n"
            "Что умею:\n"
            "• 📋 Поставить задачу в Platrum\n"
            "• 🔍 Найти задачу и прислать ссылку\n"
            "• 💬 Добавить комментарий к задаче\n"
            "• ✅ Изменить статус задачи\n"
            "• 🏢 Показать структуру компании\n\n"
            "Говори голосом, видеокружком или пиши текст.\n\n"
            "/reset — сменить API-ключ  /help — справка"
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "👋 Привет! Я твой личный Platrum-ассистент.\n\n"
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
            "Общайся со мной как с ассистентом по Platrum.\n"
            "Говори голосом или пиши — пойму.\n\n"
            f"_(Создана тестовая задача #{info['test_task_id']} — можно удалить)_",
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

        await msg.edit_text(f"📝 _{text}_\n\n⏳ Обрабатываю...", parse_mode='Markdown')
        await _handle_message(msg, text, user)

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
    msg = await update.message.reply_text("⏳ Обрабатываю...")
    await _handle_message(msg, text, user)

async def _handle_message(msg, text: str, user: dict):
    try:
        intent_data = classify_intent(text, user['name'])
        intent = intent_data.get('intent', 'chat')

        if intent == 'create_task':
            task = intent_data.get('task', {})
            if not task or not task.get('name'):
                await msg.edit_text("❓ Не понял задачу. Скажи что нужно сделать.")
                return
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

        elif intent == 'find_task':
            await msg.edit_text("🔍 Ищу задачу...")
            task = _resolve_task(intent_data, user['api_key'])
            if task:
                url = f"https://a96a08a.platrum.ru/tasks?taskId={task['id']}"
                status = STATUS_LABELS.get(task.get('status_key', ''), task.get('status_key', '—'))
                await msg.edit_text(
                    f"*{task['name']}*\nСтатус: {status}\n[Открыть в Platrum]({url})",
                    parse_mode='Markdown'
                )
            else:
                await msg.edit_text("❌ Задача не найдена. Уточни название или номер.")

        elif intent == 'add_comment':
            comment_text = intent_data.get('comment_text', '').strip()
            if not comment_text:
                await msg.edit_text("❓ Не понял текст комментария.")
                return
            await msg.edit_text("💬 Ищу задачу...")
            task = _resolve_task(intent_data, user['api_key'])
            if task:
                add_platrum_comment(task['id'], comment_text, user['api_key'])
                url = f"https://a96a08a.platrum.ru/tasks?taskId={task['id']}"
                await msg.edit_text(
                    f"✅ Комментарий добавлен в *{task['name']}*\n[Открыть в Platrum]({url})",
                    parse_mode='Markdown'
                )
                log.info(f"Comment added to task {task['id']} by {user['name']}")
            else:
                await msg.edit_text("❌ Задача не найдена. Уточни название или номер.")

        elif intent == 'change_status':
            new_status = intent_data.get('new_status', '').strip()
            if not new_status:
                await msg.edit_text("❓ Не понял новый статус. Например: «выполнена», «в работе», «отменена».")
                return
            await msg.edit_text("🔍 Ищу задачу...")
            task = _resolve_task(intent_data, user['api_key'])
            if task:
                update_platrum_task_status(task['id'], new_status, user['api_key'])
                url = f"https://a96a08a.platrum.ru/tasks?taskId={task['id']}"
                label = STATUS_LABELS.get(new_status, new_status)
                await msg.edit_text(
                    f"✅ Статус задачи *{task['name']}* изменён на: {label}\n[Открыть в Platrum]({url})",
                    parse_mode='Markdown'
                )
                log.info(f"Task {task['id']} status → {new_status} by {user['name']}")
            else:
                await msg.edit_text("❌ Задача не найдена. Уточни название или номер.")

        elif intent == 'company_info':
            await msg.edit_text("🏢 Загружаю структуру компании...")
            structure = get_company_structure(user['api_key'])
            query = intent_data.get('company_query', '')
            if query:
                # Let Claude filter the structure to answer the specific question
                client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
                answer = client.messages.create(
                    model='claude-haiku-4-5-20251001',
                    max_tokens=400,
                    messages=[{'role': 'user', 'content': (
                        f'Структура компании:\n{structure}\n\n'
                        f'Вопрос: {query}\n\n'
                        f'Ответь кратко и конкретно по данным выше.'
                    )}]
                )
                await msg.edit_text(answer.content[0].text.strip())
            else:
                await msg.edit_text(structure, parse_mode='Markdown')

        else:  # chat
            response = intent_data.get('chat_response') or 'Чем могу помочь?'
            await msg.edit_text(response)

    except Exception as e:
        log.exception(f"Handle message error for {user.get('name')}")
        await msg.edit_text(f"❌ Ошибка: {e}")

def _resolve_task(intent_data: dict, api_key: str) -> dict | None:
    task_id = intent_data.get('task_id')
    if task_id:
        try:
            task = get_platrum_task(int(task_id), api_key)
            if task:
                return task
        except Exception:
            pass
    query = intent_data.get('task_query', '').strip()
    if query:
        tasks = search_platrum_tasks(query, api_key)
        if tasks:
            return tasks[0]
    return None

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Что умею:\n\n"
        "📋 *Задачи*\n"
        "— Поставить задачу (голос/текст)\n"
        "— Найти задачу, получить ссылку\n"
        "— Добавить комментарий\n"
        "— Изменить статус (выполнена / в работе / отменена)\n\n"
        "🏢 *Компания*\n"
        "— Показать структуру компании\n"
        "— Кто занимает какую должность\n\n"
        "💬 *Общение*\n"
        "— Ответить на вопрос\n\n"
        "Команды: /start /reset /help",
        parse_mode='Markdown'
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
