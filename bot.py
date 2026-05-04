"""
Telegram-бот — личный Platrum-ассистент для руководителей.
Архитектура: Claude с tool use + история разговора на пользователя.
"""

import os
import json
import ssl
import socket
import re
import logging
import tempfile
import subprocess
import urllib.parse
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
MAX_HISTORY   = 20   # messages per user to keep

WAITING_KEY = 1

# Словарь сотрудников компании: фамилия → Platrum user_id
USERS = {
    'белов':           '77b396c84ea1b2cc02265335f8030b97',
    'голицын':         'b7f1c9768b7abdd0f62336f8a065b6cc',
    'митасов':         '0c1fbc0b5af1e27f03ae70eea6739d32',
    'кулешов':         '95f7ae5c723cea0cee906fe7ac15f9ab',
    'иванов иван к':   '0881b82513dc98290cbbf1aab8996309',
    'кукушкин':        '6e4a6da36e202c35120b1d546ddb2c68',
    'семенова':        'a40e2e4cd256a2ebbae2f142b502256c',
    'бизяев':          'b80344ba9ca6f374cf02448214719f45',
    'гаврилов':        '8669e3b12949c4ae7af00218ff5d7fc3',
    'манукян':         'a4bab15349d3a3a0af3fccf7dcc3f295',
    'зенько':          '6f20d54e55135d3885883b447d200e86',
    'овчинников':      '4e9044a80baf834ad80dfa5f2463c872',
    'старцев':         '0d33e9fda313751ce8860a4efe9c2d51',
    'нагимов':         'b5544889b47c490277fa6269c12b50e3',
    'шиманский':       '6058ad85d55465f544de689afaa2845e',
    'савинков':        'c0d5ce73ecbf439d27a3bbbde9cdb296',
    'арбузов':         '1981272c6f92a9b90fe21fb03e6cf631',
    'ивин':            'f90618dd151b6356cd646bbe9f5ffce1',
    'удовидчик':       'd7e9948d56de32bf33ababf07c04cdc9',
    'земсков':         '2c87d4ba4f89b5be2bf2145c1c48a2db',
    'ващенко':         'd879c3cdc0ed31ef92167c130e11180a',
    'фомин':           '41be7997009a2fc1bc7694f7a3a33103',
    'михалкин':        '06694ff13a17e76baf49090c4bd2b461',
    'молодыченко':     '936f3a7dfd7049a38cb6e649226e4d9a',
    'верзаков':        '37da25c9146902318218a0cbf94116a3',
    'деминский':       'e0611c5fdc16b4c3f953dfd24014b7e7',
    'воеводов':        'eabedd764f1601982493c0ee0d823ed8',
    'кин':             '21244a43083be900e89111535ac11ebb',
    'банщиков':        '01831b6b848ddac92dddd053a2680d84',
    'гелаш':           '31f63717756921b24231ae6e9393d807',
    'арефьев':         'd6b713772c40ff506db1c9f175ddc314',
    'иванов виктор':   'd1bb3c2c6bdbbe56a268b582b211ec58',
}

def find_user_id(name: str) -> str | None:
    """Ищет user_id по части имени/фамилии (нечёткий поиск)."""
    if not name:
        return None
    name_lower = name.lower().strip()
    for key, uid in USERS.items():
        if key in name_lower or name_lower in key:
            return uid
    return None

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
        log.info("Loading Whisper model...")
        _whisper_model = WhisperModel('base', device='cpu', compute_type='int8')
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

def get_history(user_id: int) -> list:
    user = get_user(user_id)
    return user.get('history', []) if user else []

def save_history(user_id: int, history: list):
    users = load_users()
    uid = str(user_id)
    if uid in users:
        users[uid]['history'] = history[-MAX_HISTORY:]
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

def _platrum_upload_file(file_bytes: bytes, filename: str, content_type: str, api_key: str) -> str:
    """Upload raw file to Platrum, returns file_id."""
    path = f'/file/upload?format=json&file_name={urllib.parse.quote(filename)}&module=tasks'
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = (
        f'POST {path} HTTP/1.1\r\n'
        f'Host: {PLATRUM_HOST}\r\n'
        f'Api-key: {api_key}\r\n'
        f'Content-Type: {content_type}\r\n'
        f'Content-Length: {len(file_bytes)}\r\n'
        f'Connection: close\r\n\r\n'
    ).encode() + file_bytes

    with socket.create_connection((PLATRUM_HOST, 443), timeout=60) as sock:
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
    data = json.loads(b''.join(chunks).decode() if chunks else raw.decode())
    if data.get('status') != 'success':
        raise ValueError(data.get('error_message') or data.get('error') or 'File upload failed')
    return data['data']['id']

def verify_platrum_key(api_key: str) -> dict:
    data = _platrum_post('/tasks/api/task/create',
                         {'name': '🤖 Тест подключения бота (можно удалить)'}, api_key)
    if data.get('status') != 'success':
        raise ValueError(data.get('error_message') or data.get('error') or 'Неверный API-ключ')
    task = data['data']
    return {'owner_user_id': task['owner_user_id'], 'test_task_id': task['id']}

def _create_task(inp: dict, api_key: str) -> str:
    body = {'name': inp['name']}
    if inp.get('description'):
        body['description'] = inp['description']
    if inp.get('finish_date'):
        body['finish_date'] = inp['finish_date']
    if inp.get('is_important'):
        body['is_important'] = True

    responsible_name = inp.get('responsible', '').strip()
    responsible_note = ''
    if responsible_name:
        uid = find_user_id(responsible_name)
        if uid:
            body['responsible_user_ids'] = [uid]
            responsible_note = f"\nИсполнитель: {responsible_name}"
        else:
            responsible_note = f"\n⚠️ Исполнитель «{responsible_name}» не найден в системе"

    data = _platrum_post('/tasks/api/task/create', body, api_key)
    if data.get('status') != 'success':
        return f"Ошибка: {data.get('error_message') or data.get('error')}"
    task = data['data']
    url = f"https://a96a08a.platrum.ru/tasks?taskId={task['id']}"
    return f"Задача создана: #{task['id']}\nНазвание: {task['name']}{responsible_note}\nСсылка: {url}"

def _find_task(inp: dict, api_key: str) -> str:
    task_id = inp.get('task_id')
    if task_id:
        data = _platrum_post('/tasks/api/task/get', {'id': int(task_id)}, api_key)
        if data.get('status') == 'success':
            t = data['data']
            url = f"https://a96a08a.platrum.ru/tasks?taskId={t['id']}"
            status_map = {'open': 'Открыта', 'in_progress': 'В работе', 'done': 'Выполнена', 'cancelled': 'Отменена'}
            return f"Задача #{t['id']}: {t['name']}\nСтатус: {status_map.get(t.get('status_key',''), t.get('status_key',''))}\nСсылка: {url}"
    query = inp.get('query', '').strip()
    if query:
        data = _platrum_post('/tasks/api/task/list', {'search': query}, api_key)
        if data.get('status') == 'success':
            tasks = data.get('data', [])
            if isinstance(tasks, dict):
                tasks = tasks.get('items', tasks.get('tasks', []))
            if tasks:
                t = tasks[0]
                url = f"https://a96a08a.platrum.ru/tasks?taskId={t['id']}"
                status_map = {'open': 'Открыта', 'in_progress': 'В работе', 'done': 'Выполнена', 'cancelled': 'Отменена'}
                return f"Задача #{t['id']}: {t['name']}\nСтатус: {status_map.get(t.get('status_key',''), t.get('status_key',''))}\nСсылка: {url}"
            return "Задача не найдена по запросу: " + query
    return "Не удалось найти задачу — укажи название или ID."

def _add_comment(inp: dict, api_key: str) -> str:
    task = _resolve_task_from_input(inp, api_key)
    if not task:
        return "Задача не найдена. Укажи название или ID."
    comment = inp.get('comment', '').strip()
    if not comment:
        return "Не указан текст комментария."
    data = _platrum_post('/tasks/api/tasks/comment/save', {'task_id': task['id'], 'text': comment}, api_key)
    if data.get('status') != 'success':
        return f"Ошибка добавления комментария: {data.get('error_message')}"
    url = f"https://a96a08a.platrum.ru/tasks?taskId={task['id']}"
    return f"Комментарий добавлен в задачу \"{task['name']}\"\nСсылка: {url}"

def _change_status(inp: dict, api_key: str) -> str:
    task = _resolve_task_from_input(inp, api_key)
    if not task:
        return "Задача не найдена. Укажи название или ID."
    new_status = inp.get('status', '').strip()
    if not new_status:
        return "Не указан новый статус."
    data = _platrum_post('/tasks/api/task/update', {'id': task['id'], 'status_key': new_status}, api_key)
    if data.get('status') != 'success':
        return f"Ошибка изменения статуса: {data.get('error_message')}"
    status_map = {'open': 'Открыта', 'in_progress': 'В работе', 'done': 'Выполнена', 'cancelled': 'Отменена'}
    url = f"https://a96a08a.platrum.ru/tasks?taskId={task['id']}"
    return f"Статус задачи \"{task['name']}\" изменён на: {status_map.get(new_status, new_status)}\nСсылка: {url}"

def _get_company_structure(api_key: str) -> str:
    blocks_r = _platrum_post('/orgschema/api/block/list', {}, api_key)
    workers_r = _platrum_post('/orgschema/api/worker/list-active', {}, api_key)
    profiles_r = _platrum_post('/user/api/profile/list', {}, api_key)

    if blocks_r.get('status') != 'success':
        return "Не удалось загрузить структуру компании."

    blocks = blocks_r.get('data', [])
    workers = workers_r.get('data', []) if workers_r.get('status') == 'success' else []
    profiles = profiles_r.get('data', {}) if profiles_r.get('status') == 'success' else {}

    block_people: dict[str, list] = {}
    for w in workers:
        bid = str(w.get('block_id', ''))
        uid = str(w.get('user_id', ''))
        p = profiles.get(uid, {})
        name = p.get('name') or p.get('user_name') or uid
        block_people.setdefault(bid, []).append(name)

    block_map = {str(b['id']): b for b in blocks}
    root_blocks = [b for b in blocks if not b.get('parent_id') or str(b.get('parent_id')) not in block_map]

    lines: list[str] = ['Структура компании:\n']

    def render(block, depth=0):
        prefix = '  ' * depth + '• '
        title = block.get('position') or block.get('name', '?')
        people = block_people.get(str(block['id']), [])
        person_str = ': ' + ', '.join(people) if people else ''
        lines.append(f'{prefix}{title}{person_str}')
        children = sorted(
            [b for b in blocks if str(b.get('parent_id')) == str(block['id'])],
            key=lambda b: b.get('order', 0)
        )
        for child in children:
            render(child, depth + 1)

    for block in sorted(root_blocks, key=lambda b: b.get('order', 0)):
        render(block)

    return '\n'.join(lines)

def _attach_file_to_task(inp: dict, api_key: str) -> str:
    task = _resolve_task_from_input(inp, api_key)
    if not task:
        return "Задача не найдена. Укажи название или ID."
    file_id = inp.get('file_id', '').strip()
    if not file_id:
        return "Не передан file_id."
    data = _platrum_post('/tasks/api/task/update', {'id': task['id'], 'file_ids': [file_id]}, api_key)
    if data.get('status') != 'success':
        return f"Ошибка прикрепления файла: {data.get('error_message')}"
    url = f"https://a96a08a.platrum.ru/tasks?taskId={task['id']}"
    return f"Файл прикреплён к задаче \"{task['name']}\"\nСсылка: {url}"

def _resolve_task_from_input(inp: dict, api_key: str) -> dict | None:
    task_id = inp.get('task_id')
    if task_id:
        data = _platrum_post('/tasks/api/task/get', {'id': int(task_id)}, api_key)
        if data.get('status') == 'success':
            return data.get('data')
    query = (inp.get('task_query') or inp.get('query') or '').strip()
    if query:
        data = _platrum_post('/tasks/api/task/list', {'search': query}, api_key)
        if data.get('status') == 'success':
            tasks = data.get('data', [])
            if isinstance(tasks, dict):
                tasks = tasks.get('items', tasks.get('tasks', []))
            if tasks:
                return tasks[0]
    return None

# ─────────────── CLAUDE TOOLS DEFINITION ───────────────
TOOLS = [
    {
        "name": "create_task",
        "description": "Создать задачу в Platrum CRM от имени пользователя. Вызывай ТОЛЬКО когда пользователь явно хочет поставить задачу.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Краткое название задачи (до 100 символов)"},
                "description": {"type": "string", "description": "Описание, детали, контекст задачи"},
                "finish_date": {"type": "string", "description": "Дедлайн в формате YYYY-MM-DD"},
                "is_important": {"type": "boolean", "description": "True если задача срочная или важная"},
                "responsible": {"type": "string", "description": "Исполнитель задачи — фамилия или часть имени сотрудника (например: Голицын, Митасов, Кулешов)"}
            },
            "required": ["name"]
        }
    },
    {
        "name": "find_task",
        "description": "Найти задачу в Platrum по названию или ID. Возвращает название, статус и ссылку.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Название или ключевые слова для поиска"},
                "task_id": {"type": "integer", "description": "Точный ID задачи если известен"}
            }
        }
    },
    {
        "name": "add_comment",
        "description": "Добавить комментарий к существующей задаче в Platrum.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_query": {"type": "string", "description": "Название задачи для поиска"},
                "task_id": {"type": "integer", "description": "ID задачи если известен"},
                "comment": {"type": "string", "description": "Текст комментария"}
            },
            "required": ["comment"]
        }
    },
    {
        "name": "change_task_status",
        "description": "Изменить статус задачи в Platrum.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_query": {"type": "string", "description": "Название задачи"},
                "task_id": {"type": "integer", "description": "ID задачи"},
                "status": {
                    "type": "string",
                    "enum": ["open", "in_progress", "done", "cancelled"],
                    "description": "Новый статус: open=открыта, in_progress=в работе, done=выполнена, cancelled=отменена"
                }
            },
            "required": ["status"]
        }
    },
    {
        "name": "get_company_structure",
        "description": "Показать организационную структуру компании: должности и сотрудники.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "attach_file_to_task",
        "description": "Прикрепить загруженный файл/фото к существующей задаче в Platrum.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "description": "ID файла в Platrum (передаётся системой при загрузке фото)"},
                "task_query": {"type": "string", "description": "Название задачи для поиска"},
                "task_id": {"type": "integer", "description": "ID задачи если известен"}
            },
            "required": ["file_id"]
        }
    }
]

def execute_tool(name: str, inp: dict, api_key: str) -> str:
    try:
        if name == 'create_task':
            return _create_task(inp, api_key)
        elif name == 'find_task':
            return _find_task(inp, api_key)
        elif name == 'add_comment':
            return _add_comment(inp, api_key)
        elif name == 'change_task_status':
            return _change_status(inp, api_key)
        elif name == 'get_company_structure':
            return _get_company_structure(api_key)
        elif name == 'attach_file_to_task':
            return _attach_file_to_task(inp, api_key)
        else:
            return f"Неизвестный инструмент: {name}"
    except Exception as e:
        return f"Ошибка при выполнении {name}: {e}"

def _serialize_content(content) -> list:
    """Convert Anthropic content blocks to JSON-serializable dicts for history storage."""
    result = []
    for block in (content if isinstance(content, list) else [content]):
        if isinstance(block, dict):
            result.append(block)
        elif hasattr(block, 'type'):
            if block.type == 'text':
                result.append({"type": "text", "text": block.text})
            elif block.type == 'tool_use':
                result.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
            else:
                result.append({"type": block.type})
    return result

# ─────────────── AGENTIC LOOP ───────────────
def chat_with_claude(user_text: str, user: dict, history: list) -> tuple[str, list]:
    today = datetime.now().strftime('%Y-%m-%d')
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    system = (
        f"Ты — личный Platrum-ассистент руководителя {user['name']} в компании по продаже б/у автозапчастей.\n"
        f"Сегодня: {today}.\n\n"
        "Отвечай по-русски, кратко и по делу — как опытный бизнес-ассистент.\n"
        "Используй инструменты ТОЛЬКО когда пользователь явно просит создать/найти/изменить задачу или узнать структуру.\n"
        "Если это просто вопрос или разговор — отвечай напрямую без инструментов.\n"
        "При создании задачи улучшай формулировку: делай название конкретным и action-oriented.\n"
        "После выполнения инструмента дай краткий содержательный ответ, не пересказывай технические детали."
    )

    messages = list(history) + [{"role": "user", "content": user_text}]

    for _ in range(5):  # max 5 tool call rounds
        response = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=1000,
            system=system,
            tools=TOOLS,
            messages=messages
        )

        serialized = _serialize_content(response.content)
        messages.append({"role": "assistant", "content": serialized})

        if response.stop_reason == 'end_turn':
            text = ' '.join(b.get('text', '') for b in serialized if b.get('type') == 'text')
            return text.strip() or '✓', messages

        if response.stop_reason == 'tool_use':
            tool_results = []
            for block in serialized:
                if block.get('type') == 'tool_use':
                    log.info(f"Tool call: {block['name']}({block['input']})")
                    result = execute_tool(block['name'], block['input'], user['api_key'])
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block['id'],
                        "content": result
                    })
            messages.append({"role": "user", "content": tool_results})

    return "Не смог обработать запрос. Попробуй ещё раз.", messages

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

# ─────────────── TELEGRAM HANDLERS ───────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    user = get_user(user_id)

    if user:
        await update.message.reply_text(
            f"Привет, {user['name']}! 👋\n\n"
            "Говори голосом, видеокружком или пиши — помогу с Platrum.\n"
            "Понимаю контекст разговора, создаю задачи, ищу, комментирую, меняю статусы.\n\n"
            "/reset — сменить API-ключ  /clear — очистить историю  /help — справка"
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "👋 Привет! Я твой личный Platrum-ассистент.\n\n"
        "Для начала нужен твой Platrum API-ключ.\n\n"
        "Как получить:\n"
        "1. Открой Platrum → Настройки (шестерёнка) → Интеграции и API → API ключи\n"
        "2. Нажми «+ Добавить», назови «Бот» и сохрани\n"
        "3. Скопируй ключ и вставь его сюда",
    )
    return WAITING_KEY

async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Введи новый Platrum API-ключ:")
    return WAITING_KEY

async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    users = load_users()
    uid = str(user_id)
    if uid in users:
        users[uid]['history'] = []
        save_users(users)
    await update.message.reply_text("История разговора очищена.")

async def receive_api_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    key = update.message.text.strip()
    user_id = update.effective_user.id
    tg_name = update.effective_user.full_name

    if len(key) < 20 or ' ' in key:
        await update.message.reply_text("Это не похоже на API-ключ. Скопируй из настроек Platrum.")
        return WAITING_KEY

    msg = await update.message.reply_text("🔑 Проверяю ключ...")

    try:
        info = verify_platrum_key(key)
        set_user(user_id, {
            'api_key': key,
            'owner_user_id': info['owner_user_id'],
            'name': tg_name,
            'registered_at': datetime.now().isoformat(),
            'history': [],
        })
        await msg.edit_text(
            f"✅ Готово, {tg_name}!\n\n"
            "Общайся как с ассистентом — голосом, видеокружком или текстом.\n"
            "Понимаю контекст, ставлю задачи, ищу, комментирую.\n\n"
            f"_(Создана тестовая задача #{info['test_task_id']} — можно удалить)_",
            parse_mode='Markdown'
        )
        log.info(f"User registered: {user_id} ({tg_name})")
    except Exception as e:
        await msg.edit_text(f"❌ Ключ не подошёл: {e}\n\nПроверь и попробуй ещё раз.")
        return WAITING_KEY

    return ConversationHandler.END

async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user:
        await update.message.reply_text("Введи /start и добавь API-ключ.")
        return

    msg = await update.message.reply_text("🎙 Распознаю...")

    tmp_path = None
    try:
        voice = update.message.voice or update.message.video_note
        voice_file = await ctx.bot.get_file(voice.file_id)

        with tempfile.NamedTemporaryFile(suffix='.oga', delete=False, dir='/tmp') as f:
            tmp_path = f.name
        await voice_file.download_to_drive(tmp_path)

        text = transcribe_audio(tmp_path)

        if not text.strip():
            await msg.edit_text("❌ Не удалось распознать речь.")
            return

        await msg.edit_text(f"📝 _{text}_\n\n⏳ Думаю...", parse_mode='Markdown')
        await _process(msg, text, user, user_id)

    except Exception as e:
        log.exception(f"Voice error {user_id}")
        await msg.edit_text(f"❌ Ошибка: {e}")
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user:
        await update.message.reply_text("Введи /start и добавь API-ключ.")
        return

    msg = await update.message.reply_text("📷 Загружаю фото в Platrum...")
    tmp_path = None
    try:
        photo = update.message.photo[-1]  # largest resolution
        caption = update.message.caption or ''

        photo_file = await ctx.bot.get_file(photo.file_id)
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False, dir='/tmp') as f:
            tmp_path = f.name
        await photo_file.download_to_drive(tmp_path)

        file_bytes = Path(tmp_path).read_bytes()
        filename = f"photo_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
        file_id = _platrum_upload_file(file_bytes, filename, 'image/jpeg', user['api_key'])

        prompt = f"[Система: пользователь отправил фото, оно загружено в Platrum, file_id={file_id}]"
        if caption:
            prompt += f" Подпись: {caption}"
        else:
            prompt += " Уточни у пользователя, к какой задаче прикрепить это фото, затем вызови attach_file_to_task."

        await msg.edit_text("📷 Фото загружено. Думаю...")
        await _process(msg, prompt, user, user_id)

    except Exception as e:
        log.exception(f"Photo error {user_id}")
        await msg.edit_text(f"❌ Ошибка загрузки фото: {e}")
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user:
        await update.message.reply_text("Введи /start и добавь API-ключ.")
        return

    text = update.message.text.strip()
    msg = await update.message.reply_text("⏳ Думаю...")
    await _process(msg, text, user, user_id)

async def _process(msg, text: str, user: dict, user_id: int):
    try:
        history = get_history(user_id)
        response, new_history = chat_with_claude(text, user, history)
        save_history(user_id, new_history)
        await msg.edit_text(response)
    except Exception as e:
        log.exception(f"Process error for {user.get('name')}")
        await msg.edit_text(f"❌ Ошибка: {e}")

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Что умею:\n\n"
        "📋 Поставить задачу в Platrum\n"
        "🔍 Найти задачу, прислать ссылку\n"
        "💬 Добавить комментарий\n"
        "✅ Изменить статус задачи\n"
        "🏢 Показать структуру компании\n"
        "🗣 Ответить на вопрос\n\n"
        "Помню контекст разговора — можно говорить как с человеком.\n\n"
        "/clear — сбросить историю разговора\n"
        "/reset — сменить API-ключ"
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
    app.add_handler(CommandHandler('clear', cmd_clear))
    app.add_handler(MessageHandler(filters.VOICE | filters.VIDEO_NOTE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info('Bot started')
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
