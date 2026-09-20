"""
🤖 TELEGRAM БОТ ДЛЯ ЗАЯВОК МЕХАНИЧЕСКОЙ ГРУППЫ ЗАВОДА
Три интерфейса: Оператор → Слесарная гр. → Админ
"""

import os
import re
import html as html_lib
import json
import uuid
import io
import csv

# Папка где лежит сам бот — все файлы ищем рядом с ним
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
from datetime import datetime, timedelta, time as dtime
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    Defaults,
    ApplicationHandlerStop,
)
from zoneinfo import ZoneInfo
import time
import logging
import gspread
# (nest_asyncio убран — ломался на новых версиях Python; run_polling сам управляет циклом)

# ============================================================================
# ПОДКЛЮЧЕНИЕ К GOOGLE SHEETS
# ============================================================================

SPREADSHEET_NAME = os.environ.get("SPREADSHEET_NAME", "")   # имя Google-таблицы (менять только вместе с таблицей!)

WORKSHOP_SHEET_NAMES = {}
SHEET_HEADERS = [
    "ID заявки", "Цех", "Отделение", "Оборудование", "Срочность",
    "Оператор", "Время подачи", "Слесарь", "Время выполнения",
    "Статус", "Фото поломки", "Комментарий слесаря", "Фото выполнения"
]

SA_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE") or os.path.join(BASE_DIR, "service_account.json")

def _make_sheets_retry():
    """Повторы с нарастающей паузой (~1,2,4,8 сек) — чтобы короткие обрывы
    интернета на заводе не роняли запись в Google Sheets. Повторяем и POST/PUT,
    иначе записи (обновление ячеек) по умолчанию не переотправляются."""
    from urllib3.util.retry import Retry
    kwargs = dict(
        total=5, connect=5, read=5,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        raise_on_status=False,
    )
    try:
        return Retry(allowed_methods=None, **kwargs)    # urllib3 2.x
    except TypeError:
        return Retry(method_whitelist=None, **kwargs)   # urllib3 1.x

def _gs_client():
    """Возвращает gspread клиент через Service Account (не истекает).
    На сетевую сессию навешиваем повторные попытки, чтобы моргания связи
    не приводили к потере данных в таблице."""
    from requests.adapters import HTTPAdapter
    client = gspread.service_account(filename=SA_FILE)
    try:
        adapter = HTTPAdapter(max_retries=_make_sheets_retry())
        client.http_client.session.mount('https://', adapter)
        client.http_client.session.mount('http://', adapter)
    except Exception as e:
        logger.warning(f"Не удалось включить повторы для Sheets: {e}")
    return client

def _resolve_sheet_name(workshop_code='bread', workshop_name=None) -> str:
    """Имя листа Sheets для цеха. Устойчиво к удалению цеха и не делает мусорных вкладок."""
    if workshop_code in WORKSHOP_SHEET_NAMES:
        return WORKSHOP_SHEET_NAMES[workshop_code]
    # Имя берём: из переданного (сохранённого в заявке) → из инвентаря → код.
    raw = workshop_name or WORKSHOPS.get(workshop_code, '') or workshop_code
    name = re.sub(r'^[\U00010000-\U0010ffff☀-➿\U0001F300-\U0001FAFF\s]+', '', raw).strip()
    # Не создаём мусорные вкладки из внутренних ID вида w_xxxxxxxx / s_xxxxxxxx
    if not name or re.fullmatch(r'[ws]_[0-9a-f]{8}', name):
        return "Прочее (без цеха)"
    return name

def get_sheet(workshop_code='bread', workshop_name=None):
    client = _gs_client()
    spreadsheet = client.open(SPREADSHEET_NAME)
    sheet_name = _resolve_sheet_name(workshop_code, workshop_name)
    try:
        return spreadsheet.worksheet(sheet_name)
    except Exception:
        # Листа нет — создаём с теми же колонками
        sheet = spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=13)
        sheet.append_row(SHEET_HEADERS)
        return sheet

def get_ppr_sheet():
    """Получить (или создать) лист 'ППР' в Google Sheets."""
    client = _gs_client()
    spreadsheet = client.open(SPREADSHEET_NAME)
    try:
        return spreadsheet.worksheet("ППР")
    except Exception:
        sheet = spreadsheet.add_worksheet(title="ППР", rows=1000, cols=10)
        sheet.append_row([
            "Дата", "День недели", "Цех", "Оборудование", "Задача",
            "Плановое время", "Статус", "Слесарь", "Время ответа", "Примечание"
        ])
        return sheet

OT_SHEET_HEADERS = [
    "ID задачи", "Дата создания", "Кто поставил",
    "Назначен слесарю", "Место / Куда", "Вид работы", "Описание"
]

def get_other_tasks_sheet():
    """Получить (или создать) лист 'Прочие работы' в Google Sheets."""
    client = _gs_client()
    spreadsheet = client.open(SPREADSHEET_NAME)
    try:
        return spreadsheet.worksheet("Прочие работы")
    except Exception:
        sheet = spreadsheet.add_worksheet(title="Прочие работы", rows=1000, cols=7)
        sheet.append_row(OT_SHEET_HEADERS)
        return sheet

def log_ppr_to_sheets(ppr: dict, date_str: str, status: str, mechanic: str,
                      response_time: str, note: str = ''):
    """Записать строку ППР в Google Sheets."""
    try:
        sheet = get_ppr_sheet()
        weekday_num = datetime.strptime(date_str, '%d.%m.%Y').weekday()
        sheet.append_row([
            date_str,
            DAY_NAMES.get(weekday_num, ''),
            WORKSHOPS.get(ppr.get('workshop', ''), ppr.get('workshop', '')),
            ppr.get('equipment', ''),
            ppr.get('task', ''),
            ppr.get('time', ''),
            status,
            mechanic,
            response_time,
            note,
        ])
    except Exception as e:
        logger.error(f"Ошибка записи ППР в Sheets: {e}")

def _get_spreadsheet():
    return _gs_client().open(SPREADSHEET_NAME)

def get_attendance_sheet(shift_key: str):
    """Получить (или создать) лист явки для смены (дневная/суточная), матрица имя×дата."""
    title = SHIFTS[shift_key]['sheet']
    spreadsheet = _get_spreadsheet()
    try:
        return spreadsheet.worksheet(title)
    except Exception:
        sheet = spreadsheet.add_worksheet(title=title, rows=200, cols=100)
        sheet.update_cell(1, 1, "Имя")
        return sheet

def _set_attendance_cell(shift_key: str, name: str, date_str: str, value: str):
    """Записать value в ячейку [name][date_str] листа явки данной смены.
    value == "" — очистить ячейку (без создания новых строк/колонок ради пустоты)."""
    try:
        sheet = get_attendance_sheet(shift_key)
        data  = sheet.get_all_values()
        if not data:
            sheet.update_cell(1, 1, "Имя")
            data = [["Имя"]]

        header = data[0]                   # строка 1: ["Имя", "16.06.2026", ...]
        names  = [row[0] for row in data]  # столбец A

        # Найти или создать колонку для даты
        if date_str in header:
            date_col = header.index(date_str) + 1
        elif value == "":
            return  # нечего очищать — колонки этой даты ещё нет
        else:
            date_col = len(header) + 1
            sheet.update_cell(1, date_col, date_str)

        # Найти или создать строку для имени
        if name in names:
            name_row = names.index(name) + 1
        elif value == "":
            return  # нечего очищать — строки этого имени ещё нет
        else:
            name_row = len(data) + 1
            sheet.update_cell(name_row, 1, name)

        sheet.update_cell(name_row, date_col, value)
    except Exception as e:
        logger.error(f"Ошибка записи явки ({shift_key}): {e}")

def mark_attendance(name: str, date_str: str, shift_key: str):
    """Отметить явку: ✅ в листе выбранной смены, очистить отметку в листе другой смены
    (человек всегда числится ровно на одной смене за день)."""
    _set_attendance_cell(shift_key, name, date_str, "✅")
    for other in SHIFTS:
        if other != shift_key:
            _set_attendance_cell(other, name, date_str, "")

def get_report_sheet():
    """Получить (или создать) лист 'Отчёт дня'."""
    spreadsheet = _get_spreadsheet()
    try:
        return spreadsheet.worksheet("Отчёт дня")
    except Exception:
        sheet = spreadsheet.add_worksheet(title="Отчёт дня", rows=1000, cols=6)
        sheet.append_row(["Дата", "Слесарь", "Принято", "Закрыто", "Отложено", "Итого"])
        return sheet

# ============================================================================
# МЕСЯЧНАЯ АРХИВАЦИЯ ЛИСТОВ ЦЕХОВ
# ============================================================================

def _archive_request_row(r: dict) -> list:
    """Строка заявки в формате колонок A..M (как в finalize_request)."""
    section_name = WORKSHOP_SECTIONS.get(r.get('workshop', ''), {}).get(r.get('section', ''), '')
    status_text = {
        'new':         'Новая',
        'in_progress': f"В работе (ETA: {r.get('eta', '')})",
        'postponed':   f"Отложена: {r.get('postpone_reason', '')}",
        'done':        'Выполнена',
        'cancelled':   'Отменена',
    }.get(r.get('status', ''), r.get('status', ''))
    return [
        r.get('id', ''),
        WORKSHOPS.get(r.get('workshop', ''), r.get('workshop_name') or r.get('workshop', '')),
        section_name,
        r.get('problem', ''),
        URGENCY_LEVELS.get(r.get('urgency', ''), r.get('urgency', '')),
        r.get('user_name', ''),
        r.get('timestamp', ''),
        r.get('mechanic_name') or '',
        r.get('done_time') or r.get('accept_time') or '',
        status_text,
        r.get('photo_channel_link') or '',
        r.get('done_comment') or '',
        r.get('done_photo_channel_link') or '',
    ]

def _workshop_sheet_names() -> set:
    """Имена всех листов-цехов, подлежащих архивации (без ППР/Явки/Отчёта/Прочих работ)."""
    names = set(WORKSHOP_SHEET_NAMES.values())
    for code, disp in WORKSHOPS.items():
        if code not in WORKSHOP_SHEET_NAMES:
            names.add(_resolve_sheet_name(code, disp))
    names.add("Прочее (без цеха)")
    return names

def archive_workshops(month_dt) -> tuple:
    """Переименовывает листы цехов в «<имя> (<месяц> <год>)» и создаёт свежие пустые.
    Открытые заявки (новые/в работе/отложенные) переносятся в новый лист.
    Возвращает (список_заархивированных_имён, метка_месяца)."""
    label = f"{MONTHS_RU[month_dt.month]} {month_dt.year}"
    ss = _get_spreadsheet()
    existing = {ws.title for ws in ss.worksheets()}
    archived = []
    for name in _workshop_sheet_names():
        if name not in existing:
            continue  # такого листа нет — нечего архивировать
        archive_title = f"{name} ({label})"
        if archive_title in existing:
            logger.info(f"Архив «{archive_title}» уже существует — пропускаю")
            continue
        try:
            ws = ss.worksheet(name)
            ws.update_title(archive_title)                      # старый -> архивный
            fresh = ss.add_worksheet(title=name, rows=1000, cols=13)  # новый пустой
            fresh.append_row(SHEET_HEADERS)
            # Перенести открытые заявки этого цеха, чтобы их можно было дозакрыть
            open_rows = [
                _archive_request_row(r) for r in REQUESTS.values()
                if r.get('status') in ('new', 'in_progress', 'postponed')
                and _resolve_sheet_name(r.get('workshop', 'bread'), r.get('workshop_name')) == name
            ]
            for row in open_rows:
                fresh.append_row(row)
            archived.append(name)
            logger.info(f"Архивирован «{name}» -> «{archive_title}», перенесено открытых: {len(open_rows)}")
        except Exception as e:
            logger.error(f"Ошибка архивации листа «{name}»: {e}")
    return archived, label

# ============================================================================
# НАСТРОЙКИ
# ============================================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
USERS_FILE        = os.path.join(BASE_DIR, "users.json")
REQUESTS_FILE     = os.path.join(BASE_DIR, "requests.json")
PPR_FILE          = os.path.join(BASE_DIR, "ppr.json")
OT_FILE           = os.path.join(BASE_DIR, "other_tasks.json")
DAILY_STATE_FILE  = os.path.join(BASE_DIR, "daily_state.json")
LAST_CLEANUP_FILE = os.path.join(BASE_DIR, "last_cleanup.json")
INVENTORY_FILE    = os.path.join(BASE_DIR, "inventory.json")
ETA_NOW_TEXT = "сейчас, уже иду"   # значение ETA для кнопки «🚀 Уже иду / начал»
# Сколько минут обещает слесарь по каждой кнопке ETA (для напоминания и очков)
ETA_CODE_MINUTES = {'now': 0, '15': 15, '30': 30, '60': 60, '120': 120}
ETA_REMIND_GRACE_MINUTES = 15   # через сколько минут ПОСЛЕ обещанного срока мягко напомнить
ETA_KEEP_GRACE_MINUTES   = 90   # люфт на саму работу: уложился в срок+люфт → очко за честный ETA
POINTS_ETA_KEPT          = 1    # очки за сдержанное обещание по времени

def eta_line(eta_text: str, second_person: bool = False) -> str:
    """Строка про ETA для сообщений. Для «сейчас» — своя формулировка, чтобы читалось нормально."""
    if eta_text == ETA_NOW_TEXT:
        return "🚀 Уже в пути — приступаешь сейчас" if second_person else "🚀 Слесарь уже идёт — приступает сейчас"
    return f"⏱ Приступишь через: {eta_text}" if second_person else f"⏱ Приступит через: {eta_text}"

OVERDUE_MINUTES = 15          # через сколько минут пинговать механиков если заявку не приняли
OVERDUE_INPROGRESS_HOURS = 2  # через сколько часов пинговать если заявка висит "в работе"
PHOTO_CHANNEL_ID = int(os.environ.get("PHOTO_CHANNEL_ID", "0"))   # канал завода (Karavay), куда уходят все заявки
PPR_NOTIFY_TIME = "08:00"     # время ежедневной рассылки ППР (ЧЧ:ММ)
RATING_MORNING_TIME = "07:30" # время ежедневной утренней рассылки рейтинга слесарям (ЧЧ:ММ)
TIMEZONE = ZoneInfo("Asia/Almaty")  # местная таймзона (UTC+5) для всех задач по расписанию

# ============================================================================
# ХРАНИЛИЩЕ ЗАЯВОК
# ============================================================================

REQUESTS: dict = {}
REQUEST_COUNTER = [0]
PPR_STATUS_TODAY: dict = {}   # { "ppr_id_YYYYMMDD": {status, mechanic, time, note} }
ATTENDANCE_TODAY: dict = {}   # { user_id: 'day' | 'sutki' } — кто и на какой смене отметился сегодня

# Смены: ключ -> отображение. Метка (emoji+название) пишется в ячейку Google Sheets.
SHIFTS = {
    'day':   {'name': 'Дневная',  'emoji': '☀️', 'sheet': 'Явка дневная'},
    'sutki': {'name': 'Суточная', 'emoji': '🌙', 'sheet': 'Явка суточная'},
}

def shift_mark(shift_key: str) -> str:
    """«☀️ Дневная» / «🌙 Суточная» — что пишем в ячейку явки."""
    s = SHIFTS.get(shift_key)
    return f"{s['emoji']} {s['name']}" if s else "✅"

OTHER_TASKS: dict  = {}       # { "OT-0001": {...} }
OT_COUNTER  = [0]

def new_request_id() -> str:
    REQUEST_COUNTER[0] += 1
    return f"REQ-{REQUEST_COUNTER[0]:04d}"

def new_ot_id() -> str:
    OT_COUNTER[0] += 1
    return f"OT-{OT_COUNTER[0]:04d}"

# ============================================================================
# ЛОГИРОВАНИЕ
# ============================================================================

from logging.handlers import RotatingFileHandler

LOG_FILE = os.path.join(BASE_DIR, "bot_log.txt")
_log_fmt = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# В консоль (чёрное окно) и одновременно в файл с авто-ограничением размера
_console_h = logging.StreamHandler()
_console_h.setFormatter(_log_fmt)
_file_h = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding='utf-8')
_file_h.setFormatter(_log_fmt)

logging.basicConfig(level=logging.INFO, handlers=[_console_h, _file_h])

# Приглушаем «шум» сторонних библиотек (опрос getUpdates каждые 10 сек, планировщик и т.п.)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ============================================================================
# СЛОВАРИ ДАННЫХ
# ============================================================================

WORKSHOPS = {}

WORKSHOP_SECTIONS = {}

SECTION_EQUIPMENT = {}

URGENCY_LEVELS = {
    'low':      '🟢 Низкая (можно завтра)',
    'medium':   '🟡 Средняя (на протяжении дня)',
    'high':     '🔴 Срочно (сейчас!)',
    'critical': '⚫ Критическая (остановка цеха!)'
}

URGENCY_EMOJI = {
    'low':      '🟢',
    'medium':   '🟡',
    'high':     '🔴',
    'critical': '⚫'
}

STATUS_LABELS = {
    'new':         '🆕 Новая',
    'in_progress': '⏳ В работе',
    'postponed':   '⏸ Отложена',
    'done':        '✅ Выполнена',
    'cancelled':   '❌ Отменена'
}

ROLE_NAMES = {
    'operator': '👷 Оператор',
    'mechanic': '🔧 Слесарная гр.',
    'brigadir': '🧑‍🏭 Бригадир',
    'admin':    '👨‍💼 Админ'
}

DAY_NAMES = {0:'Понедельник', 1:'Вторник', 2:'Среда', 3:'Четверг', 4:'Пятница', 5:'Суббота', 6:'Воскресенье'}
DAY_SHORT  = {0:'Пн', 1:'Вт', 2:'Ср', 3:'Чт', 4:'Пт', 5:'Сб', 6:'Вс'}
DAY_CODES  = {'понедельник':0,'вторник':1,'среда':2,'четверг':3,'пятница':4,'суббота':5,'воскресенье':6,
              'пн':0,'вт':1,'ср':2,'чт':3,'пт':4,'сб':5,'вс':6}


# ============================================================================
# ПЕРСИСТЕНТНОЕ ХРАНИЛИЩЕ ЗАЯВОК
# ============================================================================

def _atomic_write_json(path: str, data) -> None:
    """Безопасная запись JSON: пишем во временный файл и атомарно заменяем исходный.
    Если процесс упадёт во время записи — старый файл останется целым."""
    tmp = f"{path}.tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)  # атомарная замена в пределах одной ФС

def load_requests():
    global REQUESTS, REQUEST_COUNTER

    # Загружаем локальный JSON
    if os.path.exists(REQUESTS_FILE):
        with open(REQUESTS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            REQUESTS.update(data)

    # Счётчик из локального JSON
    local_max = 0
    if REQUESTS:
        local_max = max(int(k.split('-')[1]) for k in REQUESTS.keys() if '-' in k)

    # Счётчик из Google Sheets (защита от потери requests.json).
    # Сканируем ВСЕ листы (включая архивы месяцев) — чтобы не выдать дубль ID.
    sheets_max = 0
    try:
        ss = _get_spreadsheet()
        service_sheets = {'ППР', 'Явка', 'Отчёт дня', 'Прочие работы',
                          'Явка дневная', 'Явка суточная'}
        for ws in ss.worksheets():
            if ws.title in service_sheets:
                continue
            try:
                for cell in ws.col_values(1):  # все ID в столбце A
                    if cell.startswith('REQ-'):
                        try:
                            num = int(cell.split('-')[1])
                            if num > sheets_max:
                                sheets_max = num
                        except ValueError:
                            pass
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"Не удалось прочитать счётчик из Sheets: {e}")

    REQUEST_COUNTER[0] = max(local_max, sheets_max)
    logger.info(f"Загружено {len(REQUESTS)} заявок. Счётчик REQ: {REQUEST_COUNTER[0]}")

def save_requests():
    _atomic_write_json(REQUESTS_FILE, REQUESTS)

def save_other_tasks():
    _atomic_write_json(OT_FILE, OTHER_TASKS)

def load_other_tasks():
    global OTHER_TASKS, OT_COUNTER
    if os.path.exists(OT_FILE):
        with open(OT_FILE, 'r', encoding='utf-8') as f:
            OTHER_TASKS.update(json.load(f))
    if OTHER_TASKS:
        nums = []
        for k in OTHER_TASKS:
            try:
                nums.append(int(k.split('-')[1]))
            except Exception:
                pass
        OT_COUNTER[0] = max(nums) if nums else 0
    logger.info(f"Загружено {len(OTHER_TASKS)} прочих задач. Счётчик OT: {OT_COUNTER[0]}")

# ============================================================================
# ДНЕВНОЕ СОСТОЯНИЕ (явка + ППР-статусы) — сохраняем чтобы не терять при рестарте
# ============================================================================

def load_daily_state():
    """Загружает ATTENDANCE_TODAY и PPR_STATUS_TODAY из файла (если данные за сегодня)."""
    global ATTENDANCE_TODAY, PPR_STATUS_TODAY
    today_str = datetime.now().strftime('%d.%m.%Y')
    if not os.path.exists(DAILY_STATE_FILE):
        return
    try:
        with open(DAILY_STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if data.get('date') != today_str:
            return  # данные за другой день — не загружаем
        for uid_str, val in data.get('attendance', {}).items():
            ATTENDANCE_TODAY[int(uid_str)] = val
        PPR_STATUS_TODAY.update(data.get('ppr_status', {}))
        logger.info(f"Дневное состояние восстановлено: явка {len(ATTENDANCE_TODAY)} чел., ППР {len(PPR_STATUS_TODAY)} статусов")
    except Exception as e:
        logger.error(f"Ошибка загрузки daily_state: {e}")

def save_daily_state():
    """Сохраняет ATTENDANCE_TODAY и PPR_STATUS_TODAY в файл."""
    today_str = datetime.now().strftime('%d.%m.%Y')
    data = {
        'date': today_str,
        'attendance': {str(k): v for k, v in ATTENDANCE_TODAY.items()},
        'ppr_status': PPR_STATUS_TODAY,
    }
    try:
        _atomic_write_json(DAILY_STATE_FILE, data)
    except Exception as e:
        logger.error(f"Ошибка сохранения daily_state: {e}")

# ============================================================================
# ГЕЙМИФИКАЦИЯ — ОЧКИ И РЕЙТИНГ СЛЕСАРЕЙ
# ============================================================================

# Очки настраиваются здесь. Принцип: КАЧЕСТВО важнее объёма.
POINTS_URGENCY      = {'critical': 4, 'high': 3, 'medium': 2, 'low': 1}  # за выполнение по срочности
POINTS_RATING       = {'good': 6, 'ok': 2, 'bad': -6}                    # оценка оператора (главный вес)
POINTS_FAST_ACCEPT  = 1        # бонус, если принял заявку быстро
FAST_ACCEPT_MINUTES = 15       # «быстро» = принял в течение стольких минут после создания
POINTS_ATTENDANCE   = 1        # за отметку явки (один раз в день)
POINTS_PPR          = 3        # слесарю за ППР, подтверждённый бригадиром «пришёл и сделал»

SCORES_FILE = os.path.join(BASE_DIR, "scores.json")
# { "week_id": "2026-W30", "mechanics": { "<uid>": {name, week:{...}, all:{...}} } }
SCORES: dict = {"week_id": "", "mechanics": {}}

def _week_id(dt=None) -> str:
    dt = dt or datetime.now()
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"

def _blank_counters() -> dict:
    return {"points": 0, "done": 0, "good": 0, "ok": 0, "bad": 0, "shifts": 0}

def load_scores():
    global SCORES
    if not os.path.exists(SCORES_FILE):
        SCORES = {"week_id": _week_id(), "mechanics": {}}
        return
    try:
        with open(SCORES_FILE, 'r', encoding='utf-8') as f:
            SCORES = json.load(f)
        SCORES.setdefault("mechanics", {})
        SCORES.setdefault("week_id", _week_id())
        logger.info(f"Очки загружены: {len(SCORES['mechanics'])} слесарей, неделя {SCORES['week_id']}")
    except Exception as e:
        logger.error(f"Ошибка загрузки scores.json: {e}")
        SCORES = {"week_id": _week_id(), "mechanics": {}}

def save_scores():
    try:
        _atomic_write_json(SCORES_FILE, SCORES)
    except Exception as e:
        logger.error(f"Ошибка сохранения scores.json: {e}")

def _roll_week_if_needed():
    """Начался новый ISO-неделя — снимаем итоги прошлой недели (для объявления победителя),
    затем обнуляем недельные счётчики. Всё-время не трогаем."""
    now_wk = _week_id()
    prev = SCORES.get("week_id")
    if prev and prev != now_wk:
        items = sorted(SCORES.get("mechanics", {}).items(),
                       key=lambda kv: kv[1]["week"]["points"], reverse=True)
        SCORES["last_week"] = {
            "week_id": prev,
            "ranking": [(m.get("name", ""), m["week"]["points"]) for _, m in items],
            "announced": False,
        }
        for _, m in SCORES.get("mechanics", {}).items():
            m["week"] = _blank_counters()
    SCORES["week_id"] = now_wk

def _mech_entry(mech_id, name):
    uid = str(mech_id)
    m = SCORES["mechanics"].get(uid)
    if not m:
        m = {"name": name or uid, "week": _blank_counters(), "all": _blank_counters()}
        SCORES["mechanics"][uid] = m
    elif name:
        m["name"] = name  # держим имя свежим
    m.setdefault("week", _blank_counters())
    m.setdefault("all", _blank_counters())
    return m

def add_score(mech_id, name, points=0, done=0, good=0, ok=0, bad=0, shifts=0):
    """Начислить очки/счётчики слесарю (в неделю И во всё-время). Возвращает (очки_недели, место_недели)."""
    if not mech_id:
        return None
    _roll_week_if_needed()
    m = _mech_entry(mech_id, name)
    for scope in ("week", "all"):
        c = m[scope]
        c["points"] += points; c["done"] += done
        c["good"]   += good;   c["ok"]   += ok; c["bad"] += bad
        c["shifts"] += shifts
    save_scores()
    return m

def _accepted_fast(req) -> bool:
    """True, если заявку приняли в течение FAST_ACCEPT_MINUTES после создания."""
    try:
        t0 = datetime.strptime(req.get('timestamp', ''), '%d.%m.%Y %H:%M')
        t1 = datetime.strptime(req.get('accept_time', ''), '%d.%m.%Y %H:%M')
        return (t1 - t0) <= timedelta(minutes=FAST_ACCEPT_MINUTES)
    except Exception:
        return False

def _eta_promise_kept(req) -> bool:
    """Слесарь сдержал слово по времени: завершил не позже обещанного старта + люфт на работу.
    Свободный текст ETA («после обеда») не оценивается — там нечего мерить."""
    due = req.get('eta_due')
    done = req.get('done_time')
    if not due or not done:
        return False
    try:
        due_dt  = datetime.strptime(due, '%d.%m.%Y %H:%M')
        done_dt = datetime.strptime(done, '%d.%m.%Y %H:%M')
    except Exception:
        return False
    return done_dt <= due_dt + timedelta(minutes=ETA_KEEP_GRACE_MINUTES)

def award_completion(req):
    """Очки за выполнение заявки (+ бонус за быстрый приём)."""
    base = POINTS_URGENCY.get(req.get('urgency'), 1)
    fast = POINTS_FAST_ACCEPT if _accepted_fast(req) else 0
    add_score(req.get('mechanic_id'), req.get('mechanic_name'), points=base + fast, done=1)
    return base + fast

def award_rating(req, value):
    """Очки за оценку оператора. Возвращает начисленные очки."""
    pts = POINTS_RATING.get(value, 0)
    counters = {'good': 0, 'ok': 0, 'bad': 0}
    if value in counters:
        counters[value] = 1
    add_score(req.get('mechanic_id'), req.get('mechanic_name'), points=pts, **counters)
    return pts

def get_ratings_sheet():
    """Лист-журнал «Оценки» (создаётся при первой записи)."""
    ss = _get_spreadsheet()
    try:
        return ss.worksheet("Оценки")
    except Exception:
        sheet = ss.add_worksheet(title="Оценки", rows=2000, cols=9)
        sheet.append_row(["Дата", "Заявка", "Цех", "Слесарь", "Оценка",
                          "Очки", "Оператор", "Комментарий слесаря"])
        return sheet

def log_rating_to_sheets(req, value, points, operator_name):
    """Записать оценку в лист «Оценки»."""
    try:
        sheet = get_ratings_sheet()
        sheet.append_row([
            datetime.now().strftime('%d.%m.%Y %H:%M'),
            req.get('id', ''),
            WORKSHOPS.get(req.get('workshop', ''), req.get('workshop', '')),
            req.get('mechanic_name') or '',
            RATING_LABELS.get(value, value),
            points,
            operator_name or '',
            req.get('done_comment') or '',
        ])
    except Exception as e:
        logger.error(f"Ошибка записи оценки в Sheets: {e}")

def _sorted_mechanics(by="week") -> list:
    """Список (uid, запись) по убыванию очков (by='week'|'all'), тай-брейк по другому периоду."""
    _roll_week_if_needed()
    other = "all" if by == "week" else "week"
    items = list(SCORES.get("mechanics", {}).items())
    items.sort(key=lambda kv: (kv[1][by]["points"], kv[1][other]["points"]), reverse=True)
    return items

def mechanic_chase_text(mech_id) -> str:
    """Строка «твоё место + кого догонять» (по очкам недели)."""
    items = _sorted_mechanics(by="week")
    if not items:
        return "🎮 Рейтинг пока пуст — будь первым!"
    uid = str(mech_id)
    pos = next((i for i, (u, _) in enumerate(items, 1) if u == uid), None)
    if pos is None:
        return "🎮 Ты ещё не в рейтинге — сделай первую заявку!"
    me = items[pos - 1][1]["week"]["points"]
    if pos == 1:
        if len(items) > 1:
            second = items[1][1]
            return (f"🥇 Ты 1-й на неделе — {me} очк.! "
                    f"Позади {second['name']} ({second['week']['points']}). Держи темп!")
        return f"🥇 Ты 1-й на неделе — {me} очк.! Так держать!"
    ahead = items[pos - 2][1]
    gap = ahead["week"]["points"] - me
    tail = "почти догнал! 🔥" if gap <= 3 else "жми — обгонишь! 💪"
    return (f"📍 Ты {pos}-й на неделе — {me} очк.\n"
            f"🎯 Впереди {ahead['name']} ({ahead['week']['points']}). Разрыв {gap} — {tail}")

def _week_order() -> list:
    """uid'ы по убыванию очков недели — для детекта обгона."""
    return [u for u, _ in _sorted_mechanics(by="week")]

def completion_game_line(mech_id, earned, before_order) -> str:
    """Строка геймификации на экране завершения: очки + (обгон, если был) + место/догонялка."""
    line = f"🎮 <b>+{earned} очк.</b>"
    after_order = _week_order()
    uid = str(mech_id)
    if uid in before_order and uid in after_order:
        pos_b, pos_a = before_order.index(uid), after_order.index(uid)
        if pos_a < pos_b and pos_a + 1 < len(after_order):
            below = after_order[pos_a + 1]
            if below in before_order and before_order.index(below) < pos_b:
                nm = SCORES["mechanics"].get(below, {}).get("name", "")
                line += f" · 🔥 {nm} позади — ты {pos_a + 1}-й!"
    return line + "\n" + mechanic_chase_text(mech_id)

def rating_board_text(mech_id) -> str:
    """Текст экрана «🏆 Рейтинг» для слесаря: топ-3 недели + своя строка/догонялка."""
    items = _sorted_mechanics(by="week")
    lines = [f"🏆 <b>РЕЙТИНГ НЕДЕЛИ</b>  ({SCORES.get('week_id', '')})\n"]
    if not items:
        lines.append("Пока пусто — прими и выполни заявку, стань первым! 🚀")
        return "\n".join(lines)
    medals = ["🥇", "🥈", "🥉"]
    for i, (uid, mm) in enumerate(items[:3], 1):
        lines.append(f"{medals[i - 1]} {mm['name']} — <b>{mm['week']['points']}</b> очк.")
    lines.append("")
    lines.append(mechanic_chase_text(mech_id))
    # своя строка «за всё время»
    me = SCORES.get("mechanics", {}).get(str(mech_id))
    if me:
        lines.append(f"\n🏅 Твои очки за всё время: <b>{me['all']['points']}</b>")
    return "\n".join(lines)

def get_rating_board_sheet():
    """Лист-лидерборд «Рейтинг» (создаётся при первом обновлении)."""
    ss = _get_spreadsheet()
    try:
        return ss.worksheet("Рейтинг")
    except Exception:
        return ss.add_worksheet(title="Рейтинг", rows=200, cols=12)

def rebuild_rating_board():
    """Полностью перерисовать лист «Рейтинг» из SCORES (сортировка по очкам недели)."""
    try:
        sheet = get_rating_board_sheet()
        mechs = _sorted_mechanics(by="week")
        header = ["Место", "Слесарь", "Очки недели", "Очки всего", "Выполнено",
                  "👍", "😐", "👎", "Качество %", "Смен"]
        rows = [header]
        for i, (uid, mm) in enumerate(mechs, 1):
            w, a = mm["week"], mm["all"]
            rated = a["good"] + a["ok"] + a["bad"]
            quality = round((a["good"] * 100 + a["ok"] * 50) / rated) if rated else ""
            rows.append([i, mm.get("name", ""), w["points"], a["points"], a["done"],
                         a["good"], a["ok"], a["bad"], quality, a["shifts"]])
        sheet.clear()
        sheet.update(range_name="A1", values=rows)
    except Exception as e:
        logger.error(f"Ошибка обновления листа «Рейтинг»: {e}")

# ============================================================================
# УПРАВЛЕНИЕ ИМУЩЕСТВОМ — персистентность
# ============================================================================

def load_inventory():
    """Загружает WORKSHOPS/WORKSHOP_SECTIONS/SECTION_EQUIPMENT из inventory.json.
    Если файл не существует — создаёт его из текущих дефолтных значений."""
    global WORKSHOPS, WORKSHOP_SECTIONS, SECTION_EQUIPMENT
    if not os.path.exists(INVENTORY_FILE):
        save_inventory()  # сохранить дефолты при первом запуске
        return
    try:
        with open(INVENTORY_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        WORKSHOPS.clear()
        WORKSHOPS.update(data.get('workshops', {}))
        WORKSHOP_SECTIONS.clear()
        WORKSHOP_SECTIONS.update(data.get('sections', {}))
        SECTION_EQUIPMENT.clear()
        for key_str, equip_list in data.get('equipment', {}).items():
            w, s = key_str.split('|', 1)
            SECTION_EQUIPMENT[(w, s)] = equip_list
    except Exception as e:
        logger.error(f"Ошибка загрузки inventory: {e}")

def save_inventory():
    """Сохраняет WORKSHOPS/WORKSHOP_SECTIONS/SECTION_EQUIPMENT в inventory.json."""
    data = {
        'workshops': dict(WORKSHOPS),
        'sections':  dict(WORKSHOP_SECTIONS),
        'equipment': {
            f"{k[0]}|{k[1]}": v
            for k, v in SECTION_EQUIPMENT.items()
        }
    }
    try:
        _atomic_write_json(INVENTORY_FILE, data)
    except Exception as e:
        logger.error(f"Ошибка сохранения inventory: {e}")

def _inv_new_id(prefix: str) -> str:
    """Генерирует короткий уникальный ID: 'w_a1b2c3d4' или 's_e5f6g7h8'."""
    return f"{prefix}_{str(uuid.uuid4())[:8]}"

# ============================================================================
# ХРАНИЛИЩЕ ППР
# ============================================================================

def _valid_hhmm(value: str) -> bool:
    """Проверка времени ЧЧ:ММ с диапазонами (0-23 / 0-59) — «25:99» не пройдёт."""
    if not re.match(r'^\d{1,2}:\d{2}$', value):
        return False
    h, m = map(int, value.split(':'))
    return 0 <= h <= 23 and 0 <= m <= 59

def load_ppr() -> list:
    if os.path.exists(PPR_FILE):
        try:
            with open(PPR_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            # Повреждённый файл не должен ронять бота при старте/в полночь
            logger.error(f"Ошибка чтения ppr.json: {e}")
            return []
    return []

def save_ppr(data: list):
    _atomic_write_json(PPR_FILE, data)

# --- Пауза ППР по цеху (уведомления не рассылаются, задачи не удаляются) ---
PPR_PAUSED_FILE = os.path.join(BASE_DIR, "ppr_paused.json")
PPR_PAUSED_WORKSHOPS: set = set()   # коды цехов, у которых ППР на паузе

def load_ppr_paused():
    global PPR_PAUSED_WORKSHOPS
    if not os.path.exists(PPR_PAUSED_FILE):
        PPR_PAUSED_WORKSHOPS = set(); return
    try:
        with open(PPR_PAUSED_FILE, 'r', encoding='utf-8') as f:
            PPR_PAUSED_WORKSHOPS = set(json.load(f))
        if PPR_PAUSED_WORKSHOPS:
            logger.info(f"ППР на паузе для цехов: {sorted(PPR_PAUSED_WORKSHOPS)}")
    except Exception as e:
        logger.error(f"Ошибка загрузки ppr_paused.json: {e}")
        PPR_PAUSED_WORKSHOPS = set()

def save_ppr_paused():
    try:
        _atomic_write_json(PPR_PAUSED_FILE, sorted(PPR_PAUSED_WORKSHOPS))
    except Exception as e:
        logger.error(f"Ошибка сохранения ppr_paused.json: {e}")

def _ppr_muted(ppr) -> bool:
    """True, если ППР этого цеха на паузе — уведомления слать не надо."""
    return ppr.get('workshop') in PPR_PAUSED_WORKSHOPS

# --- Цеха бригадира: за какие цеха он отвечает (пусто = за все) ---
BRIG_SHOPS_FILE = os.path.join(BASE_DIR, "brig_shops.json")
BRIG_SHOPS: dict = {}   # { "<uid>": ["bread", "bun"] }

def load_brig_shops():
    global BRIG_SHOPS
    if not os.path.exists(BRIG_SHOPS_FILE):
        BRIG_SHOPS = {}; return
    try:
        with open(BRIG_SHOPS_FILE, 'r', encoding='utf-8') as f:
            BRIG_SHOPS = json.load(f)
    except Exception as e:
        logger.error(f"Ошибка загрузки brig_shops.json: {e}")
        BRIG_SHOPS = {}

def save_brig_shops():
    try:
        _atomic_write_json(BRIG_SHOPS_FILE, BRIG_SHOPS)
    except Exception as e:
        logger.error(f"Ошибка сохранения brig_shops.json: {e}")

def brig_shops_of(uid) -> set:
    """Коды цехов, за которые отвечает бригадир. Пустой набор = за все цеха."""
    return set(BRIG_SHOPS.get(str(uid), []))

def brig_sees_workshop(uid, workshop_code) -> bool:
    """Видит ли бригадир ППР этого цеха: да, если цех его или он не выбрал ни одного (все)."""
    shops = brig_shops_of(uid)
    return (not shops) or (workshop_code in shops)

# ============================================================================
# УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ
# ============================================================================

def load_users() -> dict:
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    default = {str(ADMIN_ID): {"role": "admin", "name": "Главный Админ"}}
    save_users(default)
    return default

def save_users(users: dict):
    _atomic_write_json(USERS_FILE, users)

def get_user_role(user_id: int) -> str | None:
    users = load_users()
    user = users.get(str(user_id))
    return user['role'] if user else None

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID or get_user_role(user_id) == 'admin'

def get_all_mechanics() -> list:
    users = load_users()
    return [int(uid) for uid, info in users.items() if info['role'] in ('mechanic', 'admin')]

def get_all_brigadiers() -> list:
    users = load_users()
    return [int(uid) for uid, info in users.items() if info.get('role') == 'brigadir']

# ============================================================================
# УВЕДОМЛЕНИЯ
# ============================================================================

async def send_to_channel(bot, text: str, file_id: str = None) -> str | None:
    """Отправляет сообщение (с фото или без) в канал. Возвращает ссылку."""
    if not PHOTO_CHANNEL_ID:
        return None
    try:
        if file_id:
            msg = await bot.send_photo(chat_id=PHOTO_CHANNEL_ID, photo=file_id, caption=text, parse_mode='HTML')
        else:
            msg = await bot.send_message(chat_id=PHOTO_CHANNEL_ID, text=text, parse_mode='HTML')
        channel_id_short = str(PHOTO_CHANNEL_ID).replace('-100', '')
        return f"https://t.me/c/{channel_id_short}/{msg.message_id}"
    except Exception as e:
        logger.error(f"Ошибка отправки в канал: {e}")
        return None

# Оставляем для совместимости
async def send_photo_to_channel(bot, file_id: str, caption: str) -> str | None:
    return await send_to_channel(bot, caption, file_id)

async def notify_user(bot, user_id: int, text: str):
    try:
        await bot.send_message(chat_id=user_id, text=text, parse_mode='HTML')
    except Exception as e:
        logger.error(f"Ошибка уведомления пользователя {user_id}: {e}")

async def notify_admins(bot, text: str):
    """Служебное уведомление всем админам из users.json (не только жёсткому ADMIN_ID)."""
    admins = [int(uid) for uid, info in load_users().items() if info.get('role') == 'admin']
    if not admins:
        admins = [ADMIN_ID]  # запасной вариант
    for uid in admins:
        await notify_user(bot, uid, text)

async def edit_or_send(update, context, text: str, keyboard: list, parse_mode='HTML'):
    """Редактирует последнее меню-сообщение бота вместо отправки нового."""
    chat_id = update.effective_chat.id
    last_id = context.user_data.get('last_menu_msg_id')
    markup  = InlineKeyboardMarkup(keyboard)
    try:
        if not last_id:
            raise ValueError("no last menu")
        msg = await context.bot.edit_message_text(
            chat_id=chat_id, message_id=last_id,
            text=text, reply_markup=markup, parse_mode=parse_mode
        )
    except Exception:
        msg = await context.bot.send_message(
            chat_id=chat_id, text=text, reply_markup=markup, parse_mode=parse_mode
        )
    context.user_data['last_menu_msg_id'] = msg.message_id
    return msg

# ============================================================================
# КОМАНДЫ АДМИНА
# ============================================================================

async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Только для админов.")
        return

    args = context.args
    if len(args) != 2:
        await update.message.reply_text(
            "⚠️ Неправильный формат!\n\n"
            "Используй так:\n"
            "<code>/adduser USER_ID operator</code>\n\n"
            "Доступные роли:\n"
            "• <code>operator</code> — оператор цеха\n"
            "• <code>mechanic</code> — слесарная гр.\n"
            "• <code>brigadir</code> — бригадир (заявки + контроль ППР)\n"
            "• <code>admin</code> — администратор",
            parse_mode='HTML'
        )
        return

    target_id, role = args[0], args[1].lower()

    if not target_id.isdigit():
        await update.message.reply_text("❌ ID должен быть числом.")
        return

    if role not in ROLE_NAMES:
        await update.message.reply_text(
            f"❌ Неизвестная роль: <b>{role}</b>\n"
            "Доступные: operator, mechanic, brigadir, admin",
            parse_mode='HTML'
        )
        return

    users = load_users()
    users[target_id] = {"role": role, "name": f"Пользователь {target_id}"}
    save_users(users)

    await update.message.reply_text(
        f"✅ Пользователь <code>{target_id}</code> добавлен как {ROLE_NAMES[role]}\n\n"
        f"Пусть напишет боту /start",
        parse_mode='HTML'
    )

async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переименовать пользователя: /rename <id> <новое имя>."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Только для админов.")
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "⚠️ Формат:\n<code>/rename USER_ID Новое имя (бригадир)</code>",
            parse_mode='HTML')
        return
    target_id = args[0]
    new_name  = ' '.join(args[1:]).strip()
    if not target_id.isdigit():
        await update.message.reply_text("❌ ID должен быть числом.")
        return
    users = load_users()
    if target_id not in users:
        await update.message.reply_text(f"❌ Пользователь <code>{target_id}</code> не найден.", parse_mode='HTML')
        return
    old = users[target_id].get('name', '')
    users[target_id]['name'] = new_name
    save_users(users)
    await update.message.reply_text(
        f"✅ Имя обновлено:\n<b>{old}</b> → <b>{new_name}</b>", parse_mode='HTML')

async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Поиск заявки по ID, оборудованию, оператору или слесарю: /find <текст>."""
    role = get_user_role(update.effective_user.id)
    if role not in ('admin', 'mechanic'):
        await update.message.reply_text("❌ Команда доступна слесарям и админам.")
        return
    query = ' '.join(context.args).strip().lower()
    if not query:
        await update.message.reply_text(
            "🔎 Что искать?\nПример: <code>/find печь</code> или <code>/find REQ-12</code>",
            parse_mode='HTML')
        return
    matches = []
    for r in REQUESTS.values():
        hay = f"{r.get('id','')} {r.get('problem','')} {r.get('user_name','')} {r.get('mechanic_name') or ''}".lower()
        if query in hay:
            matches.append(r)
    if not matches:
        await update.message.reply_text(f"🔎 По запросу «{query}» ничего не найдено.")
        return

    def _num(r):
        try: return int(r['id'].split('-')[1])
        except (IndexError, ValueError): return 0
    matches.sort(key=_num, reverse=True)

    lines = [f"🔎 <b>Найдено: {len(matches)}</b>" + (" (показаны 20)" if len(matches) > 20 else "") + "\n"]
    for r in matches[:20]:
        status = STATUS_LABELS.get(r['status'], r['status'])
        entry = (f"<b>{r['id']}</b> | {WORKSHOPS.get(r['workshop'], r['workshop'])} | {status}\n"
                 f"   📝 {r.get('problem','')}\n"
                 f"   👷 {r.get('user_name','—')} | 📅 {r.get('timestamp','—')}")
        if r['status'] == 'done':
            entry += f"\n   ✅ {r.get('done_time','—')} | 🔧 {r.get('mechanic_name','—')}"
        lines.append(entry)
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n\n<i>... (обрезано)</i>"
    await update.message.reply_text(text, parse_mode='HTML')

# --- Резервное копирование данных ---

async def send_backup(bot, chat_id) -> int:
    """Отправляет JSON-файлы данных в указанный чат. Возвращает число отправленных."""
    files = [REQUESTS_FILE, USERS_FILE, OT_FILE, PPR_FILE]
    sent = 0
    for path in files:
        if not os.path.exists(path):
            continue
        try:
            with open(path, 'rb') as f:
                await bot.send_document(chat_id=chat_id, document=f, filename=os.path.basename(path))
            sent += 1
        except Exception as e:
            logger.error(f"Бэкап {path} не отправлен: {e}")
    return sent

async def daily_backup_job(context: ContextTypes.DEFAULT_TYPE):
    """Ежедневная авто-отправка бэкапа всем админам."""
    stamp = datetime.now().strftime('%d.%m.%Y %H:%M')
    admins = [int(uid) for uid, info in load_users().items() if info.get('role') == 'admin']
    for uid in admins:
        try:
            await context.bot.send_message(
                chat_id=uid, text=f"💾 <b>Ежедневный бэкап данных</b>\n🕐 {stamp}", parse_mode='HTML')
            n = await send_backup(context.bot, uid)
            logger.info(f"Бэкап отправлен админу {uid}: {n} файлов")
        except Exception as e:
            logger.error(f"Бэкап админу {uid} не отправлен: {e}")

async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Прислать бэкап данных прямо сейчас (админ)."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Только для админов.")
        return
    await update.message.reply_text("💾 Готовлю бэкап…")
    n = await send_backup(context.bot, update.effective_chat.id)
    await update.message.reply_text(f"✅ Бэкап отправлен ({n} файлов).")

async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Только для админов.")
        return

    args = context.args
    if len(args) != 1:
        await update.message.reply_text(
            "Используй: <code>/removeuser USER_ID</code>",
            parse_mode='HTML'
        )
        return

    target_id = args[0]
    if target_id == str(ADMIN_ID):
        await update.message.reply_text("❌ Нельзя удалить главного админа!")
        return

    users = load_users()
    if target_id not in users:
        await update.message.reply_text(f"❌ Пользователь <code>{target_id}</code> не найден.", parse_mode='HTML')
        return

    removed = users.pop(target_id)
    save_users(users)
    await update.message.reply_text(
        f"✅ Пользователь <code>{target_id}</code> удалён.\n"
        f"Была роль: {ROLE_NAMES.get(removed['role'], removed['role'])}",
        parse_mode='HTML'
    )

async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Только для админов.")
        return

    users = load_users()
    lines = ["<b>👥 СПИСОК ПОЛЬЗОВАТЕЛЕЙ</b>\n"]
    for uid, info in users.items():
        lines.append(f"• <code>{uid}</code> — {ROLE_NAMES.get(info['role'], info['role'])}")
    lines.append(f"\nВсего: {len(users)} чел.")
    await update.message.reply_text("\n".join(lines), parse_mode='HTML')

async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    role = get_user_role(user.id)
    role_label = ROLE_NAMES.get(role, "❌ Нет доступа") if role else "❌ Нет доступа"
    await update.message.reply_text(
        f"<b>Твой Telegram ID:</b> <code>{user.id}</code>\n"
        f"<b>Имя:</b> {user.first_name}\n"
        f"<b>Роль:</b> {role_label}\n\n"
        f"Скопируй ID и отправь администратору для получения доступа.",
        parse_mode='HTML'
    )

# ============================================================================
# ГЛАВНОЕ МЕНЮ
# ============================================================================

# --- Постоянная нижняя (reply) клавиатура — всегда закреплена внизу, не «уезжает» ---
MENU_BTN      = "📋 Меню"
BTN_NEW_REQ   = "➕ Новая заявка"
BTN_MY_REQ    = "📋 Мои заявки"
BTN_NEW_TASKS = "📬 Новые заявки"
BTN_CHECKIN   = "📍 Я на смене"
# Все ярлыки нижней клавиатуры — по ним handle_text распознаёт нажатие
REPLY_BUTTONS = {MENU_BTN, BTN_NEW_REQ, BTN_MY_REQ, BTN_NEW_TASKS, BTN_CHECKIN}

def menu_reply_kb(role: str) -> ReplyKeyboardMarkup:
    """Нижняя клавиатура под роль (частые действия + «Меню»)."""
    if role == 'operator':
        rows = [[KeyboardButton(BTN_NEW_REQ)], [KeyboardButton(BTN_MY_REQ), KeyboardButton(MENU_BTN)]]
    elif role == 'mechanic':
        rows = [[KeyboardButton(BTN_NEW_TASKS), KeyboardButton(BTN_CHECKIN)], [KeyboardButton(MENU_BTN)]]
    elif role == 'brigadir':
        rows = [[KeyboardButton(BTN_NEW_REQ)], [KeyboardButton(MENU_BTN)]]
    else:
        rows = [[KeyboardButton(MENU_BTN)]]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)

def checkin_label(user_id: int) -> str:
    """Подпись кнопки явки: если отмечен — показываем смену, иначе приглашение отметиться."""
    shift = ATTENDANCE_TODAY.get(user_id)
    if shift in SHIFTS:
        return f"☑️ Явка: {shift_mark(shift)}"
    return "📍 Я на смене"

def checkin_menu_keyboard():
    """Клавиатура выбора смены для отметки явки."""
    return [
        [InlineKeyboardButton(f"{SHIFTS['day']['emoji']} Дневная смена",  callback_data='checkin_day')],
        [InlineKeyboardButton(f"{SHIFTS['sutki']['emoji']} Суточная смена", callback_data='checkin_sutki')],
        [InlineKeyboardButton("🔙 В меню", callback_data='role_mechanic')],
    ]

async def _screen(update, context, text, keyboard=None, parse_mode='HTML'):
    """Показать экран: обновить инлайн-сообщение (если пришли по кнопке инлайн) либо
    отправить новое сообщение внизу (если пришли по нижней reply-кнопке)."""
    markup = InlineKeyboardMarkup(keyboard) if keyboard is not None else None
    q = update.callback_query
    if q:
        try:
            await q.edit_message_text(text, reply_markup=markup, parse_mode=parse_mode)
            return
        except Exception:
            pass
    await context.bot.send_message(chat_id=update.effective_chat.id, text=text,
                                   reply_markup=markup, parse_mode=parse_mode)

async def open_role_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Открывает свежее меню роли новым сообщением внизу (для кнопки «📋 Меню»)."""
    role = get_user_role(update.effective_user.id)
    if role == 'operator':
        await operator_quick_menu(update, context)
    elif role == 'mechanic':
        await mechanic_quick_menu(update, context)
    elif role in ('admin', 'brigadir'):
        text, markup = build_role_menu(update.effective_user.id, role)
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text=text,
            reply_markup=markup, parse_mode='HTML')
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="У тебя нет доступа. Напиши /start.")

def build_role_menu(user_id: int, role: str):
    """Собирает меню роли: возвращает (text, InlineKeyboardMarkup) или (None, None)."""
    if role == 'operator':
        keyboard = [
            [InlineKeyboardButton("➕ Новая заявка",  callback_data='operator_new_request')],
            [InlineKeyboardButton("📋 Мои заявки",    callback_data='operator_my_requests')],
        ]
        text = "<b>👷 Меню Оператора</b>\n\nПодай новую заявку или посмотри статус старых."

    elif role == 'brigadir':
        keyboard = [
            [InlineKeyboardButton("🗓 ППР сегодня",   callback_data='pprcheck')],
            [InlineKeyboardButton("🏭 Мои цеха",      callback_data='brig_shops')],
            [InlineKeyboardButton("➕ Новая заявка",  callback_data='operator_new_request')],
            [InlineKeyboardButton("📋 Мои заявки",    callback_data='operator_my_requests')],
        ]
        text = "<b>🧑‍🏭 Меню Бригадира</b>\n\nОтметь итоги ППР или подай заявку."

    elif role == 'mechanic':
        new_count       = len([r for r in REQUESTS.values() if r['status'] == 'new'])
        in_prog_count   = len([r for r in REQUESTS.values() if r['status'] == 'in_progress'])
        postponed_count = len([r for r in REQUESTS.values() if r['status'] == 'postponed'])
        keyboard = [
            [InlineKeyboardButton(checkin_label(user_id),               callback_data='checkin_today')],
            [InlineKeyboardButton(f"📬 Новые заявки ({new_count})",     callback_data='mechanic_new_requests')],
            [InlineKeyboardButton(f"⏳ В работе ({in_prog_count})",      callback_data='mechanic_in_progress')],
            [InlineKeyboardButton(f"⏸ Отложенные ({postponed_count})",  callback_data='mechanic_postponed')],
            [InlineKeyboardButton("✅ Завершенные",                       callback_data='mechanic_completed')],
            [InlineKeyboardButton("📝 Прочие работы",                    callback_data='mechanic_other_tasks')],
            [InlineKeyboardButton("🏆 Рейтинг", callback_data='mechanic_rating')],
        ]
        text = "<b>🔧 Меню Слесарной группы</b>\n\nВыбери раздел:"

    elif role == 'admin':
        keyboard = [
            [InlineKeyboardButton("📊 Статистика",            callback_data='admin_stats')],
            [InlineKeyboardButton("📋 Все заявки",            callback_data='admin_all_requests')],
            [InlineKeyboardButton("🏆 Аналитика",             callback_data='admin_analytics')],
            [InlineKeyboardButton("🏅 Слесари / премии",      callback_data='admin_mech_list')],
            [InlineKeyboardButton("📥 Экспорт в Excel",       callback_data='admin_export')],
            [InlineKeyboardButton("🗄 Архив месяца",           callback_data='admin_archive')],
            [InlineKeyboardButton("👥 Пользователи",          callback_data='admin_users_list')],
            [InlineKeyboardButton("📢 Рассылка",              callback_data='admin_broadcast')],
            [InlineKeyboardButton("🗓 График ППР",            callback_data='admin_ppr')],
            [InlineKeyboardButton("📝 Прочие задачи",         callback_data='admin_other_tasks')],
            [InlineKeyboardButton("🏭 Управление имуществом", callback_data='admin_inventory')],
            [InlineKeyboardButton("🗑 Очистить историю",      callback_data='admin_clear_menu')],
            [InlineKeyboardButton("🔙 Сменить меню",          callback_data='back_to_main')],
        ]
        text = "<b>👨‍💼 АДМИН ПАНЕЛЬ</b>\n\nУправление заявками и отчетами:"

    else:
        return None, None

    return text, InlineKeyboardMarkup(keyboard)

async def _send_role_menu(update, context):
    """Отправляет меню роли напрямую как новое сообщение (для /start и аналогов)."""
    user_id = update.effective_user.id
    role    = get_user_role(user_id)
    text, markup = build_role_menu(user_id, role)
    if text is None:
        return  # нет роли — не показываем ничего

    msg = await update.message.reply_text(text, reply_markup=markup, parse_mode='HTML')
    context.user_data['last_menu_msg_id'] = msg.message_id

async def send_startup_menus(context: ContextTypes.DEFAULT_TYPE):
    """При запуске бота отправляет каждому участнику его меню (чтобы оно было под рукой)."""
    users = load_users()
    sent = 0
    for uid, info in users.items():
        text, markup = build_role_menu(int(uid), info.get('role'))
        if text is None:
            continue
        try:
            await context.bot.send_message(
                chat_id=int(uid),
                text=f"🤖 <b>Бот перезапущен и готов к работе!</b>\n\n{text}",
                reply_markup=markup,
                parse_mode='HTML'
            )
            sent += 1
        except Exception as e:
            logger.warning(f"Не удалось отправить стартовое меню {uid}: {e}")
    logger.info(f"Стартовые меню разосланы: {sent} участникам")

def _sync_user_name(user) -> None:
    """Сохраняет настоящее имя из Telegram в users.json, если там ещё заглушка «Пользователь <id>»."""
    users = load_users()
    uid = str(user.id)
    info = users.get(uid)
    if not info:
        return
    real_name = (getattr(user, 'full_name', None) or user.first_name or '').strip()
    if not real_name:
        return
    current = (info.get('name') or '').strip()
    if current == '' or current == f'Пользователь {uid}':
        info['name'] = real_name
        users[uid] = info
        save_users(users)
        logger.info(f"Имя пользователя {uid} обновлено на «{real_name}»")

_last_callback = {}          # (user_id, callback_data) -> момент времени
CB_DEBOUNCE_SEC = 1.5        # окно подавления повторных нажатий одной и той же кнопки

async def debounce_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Глобальная защита от двойных нажатий: одно и то же действие, нажатое повторно
    в течение CB_DEBOUNCE_SEC секунд, гасится и дальше не обрабатывается."""
    q = update.callback_query
    if not q:
        return
    key = (update.effective_user.id, q.data)
    now = time.monotonic()
    if now - _last_callback.get(key, 0.0) < CB_DEBOUNCE_SEC:
        try:
            await q.answer("⏳ Секунду…")
        except Exception:
            pass
        raise ApplicationHandlerStop      # блокируем повторную обработку
    _last_callback[key] = now
    # периодически чистим старые записи, чтобы словарь не рос
    if len(_last_callback) > 500:
        old = [k for k, v in _last_callback.items() if now - v > 60]
        for k in old:
            _last_callback.pop(k, None)

# Запросы доступа (слесарь/админ), ожидающие одобрения: user_id -> {'name', 'role'}
ACCESS_REQUESTS = {}

async def _request_access(update: Update, context: ContextTypes.DEFAULT_TYPE, role: str):
    """Новый пользователь просит доступ слесаря/админа — уведомляем админов с кнопками."""
    user = update.effective_user
    uid  = user.id
    name = (user.full_name or user.first_name or '').strip() or f'Пользователь {uid}'
    role_label = 'Слесарь' if role == 'mechanic' else 'Админ'

    if uid in ACCESS_REQUESTS:
        await update.message.reply_text("⏳ Твой запрос уже на рассмотрении у администратора.")
        return
    ACCESS_REQUESTS[uid] = {'name': name, 'role': role}

    await update.message.reply_text(
        f"⏳ <b>Запрос на доступ ({role_label}) отправлен администратору.</b>\n"
        f"Когда одобрят — нажми /start.",
        parse_mode='HTML')

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Дать доступ", callback_data=f'acc_grant_{role}_{uid}')],
        [InlineKeyboardButton("❌ Отклонить",   callback_data=f'acc_deny_{uid}')],
    ])
    text = (f"🔐 <b>Запрос доступа</b>\n\n"
            f"👤 {name}\n🆔 <code>{uid}</code>\n"
            f"Хочет войти как: <b>{role_label}</b>\n\nВыдать доступ?")
    admins = [int(u) for u, i in load_users().items() if i.get('role') == 'admin'] or [ADMIN_ID]
    for aid in admins:
        try:
            await context.bot.send_message(chat_id=aid, text=text, reply_markup=kb, parse_mode='HTML')
        except Exception as e:
            logger.error(f"Не удалось отправить запрос доступа админу {aid}: {e}")

async def admin_grant_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ одобрил доступ. callback: acc_grant_{role}_{uid}."""
    query = update.callback_query
    if not is_admin(update.effective_user.id):
        await query.answer("❌ Только для админов", show_alert=True)
        return
    try:
        _, _, role, uid = query.data.split('_')   # acc_grant_{role}_{uid}
    except ValueError:
        await query.answer("❌ Ошибка данных", show_alert=True)
        return
    if role not in ('mechanic', 'admin'):
        await query.answer("❌ Неизвестная роль", show_alert=True)
        return
    await query.answer()

    req  = ACCESS_REQUESTS.pop(int(uid), None)
    name = req['name'] if req else f'Пользователь {uid}'
    users = load_users()
    users[uid] = {'role': role, 'name': name}
    save_users(users)
    role_label = 'Слесарь' if role == 'mechanic' else 'Админ'
    logger.info(f"Доступ выдан ({role}): {name} ({uid}), одобрил {update.effective_user.id}")

    try:
        await context.bot.send_message(
            chat_id=int(uid),
            text=f"✅ <b>Доступ выдан!</b> Твоя роль: {role_label}.\nНажми /start, чтобы начать.",
            parse_mode='HTML')
    except Exception as e:
        logger.error(f"Не удалось уведомить пользователя {uid} о доступе: {e}")

    await query.edit_message_text(
        f"✅ <b>Доступ выдан</b>\n👤 {name} — {role_label}\n"
        f"Одобрил: {update.effective_user.full_name or update.effective_user.first_name}",
        parse_mode='HTML')

async def admin_deny_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ отклонил доступ. callback: acc_deny_{uid}."""
    query = update.callback_query
    if not is_admin(update.effective_user.id):
        await query.answer("❌ Только для админов", show_alert=True)
        return
    await query.answer()
    uid = query.data.split('_')[2]   # acc_deny_{uid}
    req = ACCESS_REQUESTS.pop(int(uid), None)
    name = req['name'] if req else f'Пользователь {uid}'
    try:
        await context.bot.send_message(chat_id=int(uid),
            text="❌ Запрос на доступ отклонён администратором.")
    except Exception:
        pass
    await query.edit_message_text(f"❌ <b>Отклонено</b>\n👤 {name}", parse_mode='HTML')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    context.user_data['user_id'] = user_id
    context.user_data['user_name'] = user.first_name or "User"
    logger.info(f"Пользователь {user.first_name} ({user_id}) запустил бота")

    role = get_user_role(user_id)
    payload = (context.args[0].lower() if context.args else '')

    # QR оператора = авто-регистрация: не нужно ждать, пока админ добавит в users.json.
    # (QR слесаря и админ-доступ по-прежнему только через /adduser — там нужен контроль.)
    if not role and payload == 'operator':
        real_name = (user.full_name or user.first_name or '').strip() or f'Пользователь {user_id}'
        users = load_users()
        users[str(user_id)] = {"role": "operator", "name": real_name}
        save_users(users)
        role = 'operator'
        logger.info(f"Авто-регистрация оператора по QR: {real_name} ({user_id})")
        await notify_admins(
            context.bot,
            f"👷 <b>Новый оператор зарегистрировался по QR</b>\n\n"
            f"Имя: {real_name}\nID: <code>{user_id}</code>\n\n"
            f"Если это ошибка: /removeuser {user_id}"
        )

    # QR бригадира = авто-регистрация (как оператор), с уведомлением админов.
    if not role and payload == 'brigadir':
        real_name = (user.full_name or user.first_name or '').strip() or f'Пользователь {user_id}'
        users = load_users()
        users[str(user_id)] = {"role": "brigadir", "name": real_name}
        save_users(users)
        role = 'brigadir'
        logger.info(f"Авто-регистрация бригадира по QR: {real_name} ({user_id})")
        await notify_admins(
            context.bot,
            f"🧑‍🏭 <b>Новый бригадир зарегистрировался по QR</b>\n\n"
            f"Имя: {real_name}\nID: <code>{user_id}</code>\n\n"
            f"Если это ошибка: /removeuser {user_id}"
        )

    # Оператор оказался по факту бригадиром/технологом — QR бригадира апгрейдит его роль.
    # Только operator -> brigadir (слесаря/админа через QR никогда не понижаем/не меняем).
    if role == 'operator' and payload == 'brigadir':
        users = load_users()
        uid_str = str(user_id)
        real_name = users.get(uid_str, {}).get('name') or user.full_name or user.first_name or f'Пользователь {user_id}'
        users[uid_str] = {"role": "brigadir", "name": real_name}
        save_users(users)
        role = 'brigadir'
        logger.info(f"Роль обновлена по QR: {real_name} ({user_id}) оператор -> бригадир")
        await notify_admins(
            context.bot,
            f"🧑‍🏭 <b>Роль обновлена по QR</b>\n\n"
            f"{real_name} (<code>{user_id}</code>): оператор → бригадир\n\n"
            f"Если это ошибка: /adduser {user_id} operator"
        )

    # QR слесаря/админа — доступ только через одобрение админом
    if not role and payload in ('mechanic', 'admin'):
        await _request_access(update, context, payload)
        return

    if role:
        _sync_user_name(user)

    if not role:
        await update.message.reply_text(
            "👋 Привет!\n\n"
            "У тебя пока нет доступа к системе заявок.\n\n"
            "Отправь свой ID администратору:\n"
            f"<b>Твой ID: <code>{user_id}</code></b>",
            parse_mode='HTML'
        )
        return

    # Закрепить нижнюю клавиатуру (частые действия + «Меню») — чтобы меню не терялось
    try:
        await update.message.reply_text(
            "👇 Меню и частые действия теперь всегда внизу экрана.",
            reply_markup=menu_reply_kb(role), parse_mode='HTML')
    except Exception as e:
        logger.warning(f"Не удалось установить нижнюю клавиатуру: {e}")

    # Deep-link из QR-кода: /start operator  или  /start mechanic → сразу нужное меню.
    payload = (context.args[0].lower() if context.args else '')
    target = None
    if payload == 'operator' and role in ('operator', 'admin'):
        target = 'operator'
    elif payload == 'mechanic' and role in ('mechanic', 'admin'):
        target = 'mechanic'
    elif payload == 'brigadir' and role in ('brigadir', 'admin'):
        target = 'brigadir'

    if target:
        text, markup = build_role_menu(user_id, target)
        msg = await update.message.reply_text(text, reply_markup=markup, parse_mode='HTML')
        context.user_data['last_menu_msg_id'] = msg.message_id
        return

    await _send_role_menu(update, context)

# ============================================================================
# ИНТЕРФЕЙС ОПЕРАТОРА
# ============================================================================

async def operator_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    role = get_user_role(user_id)
    # Бригадир пользуется общим потоком заявок (Мои заявки/Новая), но «Назад» должен
    # возвращать его в меню бригадира, а не оператора.
    if role == 'brigadir':
        return await brigadir_menu(update, context)
    if role not in ('operator', 'admin'):
        await query.answer("❌ У тебя нет доступа!", show_alert=True)
        return
    await query.answer()
    context.user_data['role'] = 'operator'

    keyboard = [
        [InlineKeyboardButton("➕ Новая заявка", callback_data='operator_new_request')],
        [InlineKeyboardButton("📋 Мои заявки", callback_data='operator_my_requests')],
        [InlineKeyboardButton("🔙 Главное меню", callback_data='back_to_main')]
    ]
    await query.edit_message_text(
        "<b>👷 Меню Оператора</b>\n\n"
        "Подай новую заявку или посмотри статус старых.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def brigadir_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if get_user_role(update.effective_user.id) not in ('brigadir', 'admin'):
        await query.answer("❌ У тебя нет доступа!", show_alert=True)
        return
    await query.answer()
    context.user_data['role'] = 'brigadir'
    keyboard = [
        [InlineKeyboardButton("🗓 ППР сегодня",   callback_data='pprcheck')],
        [InlineKeyboardButton("🏭 Мои цеха",      callback_data='brig_shops')],
        [InlineKeyboardButton("➕ Новая заявка",  callback_data='operator_new_request')],
        [InlineKeyboardButton("📋 Мои заявки",    callback_data='operator_my_requests')],
        [InlineKeyboardButton("🔙 Главное меню",  callback_data='back_to_main')],
    ]
    await query.edit_message_text(
        "<b>🧑‍🏭 Меню Бригадира</b>\n\nОтметь итоги ППР или подай заявку.",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def brig_my_shops(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Экран выбора цехов бригадира: отмечает свои цеха галочкой."""
    query = update.callback_query
    if get_user_role(update.effective_user.id) not in ('brigadir', 'admin'):
        await query.answer("❌ У тебя нет доступа!", show_alert=True)
        return
    await query.answer()
    uid = update.effective_user.id
    mine = brig_shops_of(uid)
    lines = ["<b>🏭 МОИ ЦЕХА</b>\n",
             "Отметь цеха, за которые ты отвечаешь.",
             "В «🗓 ППР сегодня» и уведомлениях будут только они.\n"]
    if not mine:
        lines.append("Сейчас выбрано: <b>все цеха</b> (ничего не отмечено).")
    keyboard = []
    for code, name in WORKSHOPS.items():
        mark = "✅" if code in mine else "☐"
        keyboard.append([InlineKeyboardButton(f"{mark} {name}", callback_data=f'brigshop_{code}')])
    keyboard.append([InlineKeyboardButton("🔙 В меню", callback_data='role_brigadir')])
    await _screen(update, context, "\n".join(lines), keyboard)

async def brig_shop_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключить принадлежность цеха бригадиру."""
    query = update.callback_query
    if get_user_role(update.effective_user.id) not in ('brigadir', 'admin'):
        await query.answer("❌ Нет доступа", show_alert=True)
        return
    code = query.data.split('_', 1)[1]   # brigshop_{code}
    uid_str = str(update.effective_user.id)
    mine = set(BRIG_SHOPS.get(uid_str, []))
    if code in mine:
        mine.discard(code)
    else:
        mine.add(code)
    if mine:
        BRIG_SHOPS[uid_str] = sorted(mine)
    else:
        BRIG_SHOPS.pop(uid_str, None)   # пусто = все цеха
    save_brig_shops()
    await query.answer("Сохранено")
    await brig_my_shops(update, context)

async def operator_new_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    context.user_data['workshop'] = None
    context.user_data['problem'] = None
    context.user_data['urgency'] = None
    context.user_data['photo_file_id'] = None

    keyboard = [
        [InlineKeyboardButton(name, callback_data=f'workshop_{code}')]
        for code, name in WORKSHOPS.items()
    ]
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='role_operator')])
    await _screen(update, context, "<b>Шаг 1️⃣: Выбери свой цех</b>", keyboard)

async def workshop_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    workshop_code = query.data.split('workshop_', 1)[1]  # работает для любых ID цехов
    context.user_data['workshop'] = workshop_code

    sections = WORKSHOP_SECTIONS.get(workshop_code, {})
    keyboard = [
        [InlineKeyboardButton(name, callback_data=f'section_{code}')]
        for code, name in sections.items()
    ]
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='operator_new_request')])
    await query.edit_message_text(
        f"✅ Цех: <b>{WORKSHOPS[workshop_code]}</b>\n\n"
        f"<b>Шаг 2️⃣: Выбери отделение</b>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def section_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    section_code = query.data.split('_', 1)[1]
    context.user_data['section'] = section_code

    workshop_code = context.user_data.get('workshop', '')
    section_name = WORKSHOP_SECTIONS.get(workshop_code, {}).get(section_code, section_code)
    equipment_list = SECTION_EQUIPMENT.get((workshop_code, section_code), [])

    keyboard = []
    for i, eq in enumerate(equipment_list):
        keyboard.append([InlineKeyboardButton(eq, callback_data=f'equip_{i}')])
    keyboard.append([InlineKeyboardButton("✏️ Другое", callback_data='equip_other')])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data=f'workshop_{workshop_code}')])

    await query.edit_message_text(
        f"✅ Цех: <b>{WORKSHOPS[workshop_code]}</b>\n"
        f"✅ Отделение: <b>{section_name}</b>\n\n"
        f"<b>Шаг 3️⃣: Выбери оборудование</b>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def equipment_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    equip_part = query.data.split('_', 1)[1]
    workshop_code = context.user_data.get('workshop', '')
    section_code = context.user_data.get('section', '')
    equipment_list = SECTION_EQUIPMENT.get((workshop_code, section_code), [])

    equip_name = equipment_list[int(equip_part)]
    context.user_data['problem'] = equip_name

    keyboard = [
        [InlineKeyboardButton(name, callback_data=f'urgency_{code}')]
        for code, name in URGENCY_LEVELS.items()
    ]
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data=f'section_{section_code}')])
    await query.edit_message_text(
        f"✅ Оборудование: <b>{equip_name}</b>\n\n"
        f"<b>Шаг 4️⃣: Выбери срочность</b>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def equipment_other(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['waiting_for_problem'] = True
    context.user_data['last_menu_msg_id'] = query.message.message_id

    workshop_code = context.user_data.get('workshop', '')
    section_code = context.user_data.get('section', '')
    section_name = WORKSHOP_SECTIONS.get(workshop_code, {}).get(section_code, section_code)

    await query.edit_message_text(
        f"✅ Цех: <b>{WORKSHOPS.get(workshop_code, '')}</b>\n"
        f"✅ Отделение: <b>{section_name}</b>\n\n"
        f"<b>Шаг 3️⃣: Опиши проблему текстом</b>\n"
        f"Например: «Утечка масла», «Посторонний шум»\n\n"
        f"👇 Напиши в чат:",
        parse_mode='HTML'
    )

async def problem_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    problem_text = update.message.text
    context.user_data['problem'] = problem_text
    context.user_data['waiting_for_problem'] = False

    section_code = context.user_data.get('section', '')
    keyboard = [
        [InlineKeyboardButton(name, callback_data=f'urgency_{code}')]
        for code, name in URGENCY_LEVELS.items()
    ]
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data=f'section_{section_code}')])
    await edit_or_send(update, context,
        f"✅ Проблема записана: <b>{problem_text}</b>\n\n"
        f"<b>Шаг 4️⃣: Выбери срочность</b>",
        keyboard
    )

async def urgency_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    urgency_code = query.data.split('_')[1]
    context.user_data['urgency'] = urgency_code
    context.user_data['waiting_for_op_comment'] = True
    context.user_data['last_menu_msg_id'] = query.message.message_id

    keyboard = [[InlineKeyboardButton("⏭ Пропустить", callback_data='skip_op_comment')]]
    await query.edit_message_text(
        f"✅ Срочность: <b>{URGENCY_LEVELS[urgency_code]}</b>\n\n"
        f"<b>Шаг 5️⃣: Комментарий для слесаря</b> (необязательно)\n\n"
        f"💬 Напиши, что важно знать — например, какие инструменты взять "
        f"или в чём особенность поломки. Или нажми «Пропустить»:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def _ask_operator_photo(update, context):
    """Шаг 6: запросить фото поломки (после комментария)."""
    context.user_data['waiting_for_photo'] = True
    keyboard = [[InlineKeyboardButton("⏭ Пропустить", callback_data='skip_photo')]]
    await edit_or_send(update, context,
        "<b>Шаг 6️⃣: Прикрепи фото поломки</b> (необязательно)\n\n"
        "📸 Отправь фото в чат или нажми «Пропустить»:",
        keyboard
    )

async def operator_comment_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Оператор написал комментарий для слесаря."""
    context.user_data['waiting_for_op_comment'] = False
    context.user_data['op_comment'] = (update.message.text or '').strip()
    await _ask_operator_photo(update, context)

async def operator_skip_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['waiting_for_op_comment'] = False
    context.user_data['op_comment'] = ''
    await _ask_operator_photo(update, context)

async def operator_skip_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['waiting_for_photo'] = False
    context.user_data['photo_file_id'] = None
    await finalize_request(update, context, edit_message=True)

async def operator_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('waiting_for_photo'):
        await update.message.reply_text("⚠️ Нажми /start и подай заявку заново — сессия устарела.")
        return
    context.user_data['waiting_for_photo'] = False
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.document:
        file_id = update.message.document.file_id
    else:
        file_id = None
    context.user_data['photo_file_id'] = file_id
    await finalize_request(update, context, edit_message=False)

async def finalize_request(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_message: bool = True):
    # Защита от повторной подачи: если поток уже завершён/очищен — выходим (двойной тап, старая кнопка)
    if not context.user_data.get('workshop') or not context.user_data.get('urgency'):
        if update.callback_query:
            try:
                await update.callback_query.answer()
            except Exception:
                pass
        return

    user_id = update.effective_user.id
    user_name = update.effective_user.full_name or update.effective_user.first_name or 'Unknown'
    urgency_code = context.user_data['urgency']
    workshop_code = context.user_data['workshop']
    section_code = context.user_data.get('section', '')
    section_name = WORKSHOP_SECTIONS.get(workshop_code, {}).get(section_code, '')
    photo_file_id = context.user_data.get('photo_file_id')
    op_comment = (context.user_data.pop('op_comment', '') or '').strip()

    req_id = new_request_id()
    REQUESTS[req_id] = {
        'id': req_id,
        'user_id': user_id,
        'user_name': user_name,
        'workshop': workshop_code,
        'workshop_name': WORKSHOPS.get(workshop_code, workshop_code),  # для устойчивости к удалению цеха
        'section': section_code,
        'problem': context.user_data['problem'],
        'op_comment': op_comment,   # комментарий оператора слесарю (какие инструменты и т.п.)
        'urgency': urgency_code,
        'timestamp': datetime.now().strftime('%d.%m.%Y %H:%M'),
        'status': 'new',
        'mechanic_id': None,
        'mechanic_name': None,
        'photo_file_id': photo_file_id,
        'photo_channel_link': None,
        'done_comment': None,
        'done_photo_file_id': None,
        'done_photo_channel_link': None,
        'rating': None,        # оценка качества ремонта оператором: good/ok/bad
        'rating_time': None,
    }

    # Отправить заявку в канал (всегда, с фото или без)
    urgency_label = URGENCY_LEVELS.get(urgency_code, '')
    op_comment_line = f"\n💬 Коммент оператора: {op_comment}" if op_comment else ""
    channel_text = (
        f"🆕 <b>НОВАЯ ЗАЯВКА — {req_id}</b>\n"
        f"🏭 {WORKSHOPS.get(workshop_code, '')} | {section_name}\n"
        f"🔧 {context.user_data.get('problem', '')}\n"
        f"{urgency_label}\n"
        f"👷 Оператор: {user_name}"
        f"{op_comment_line}\n"
        f"🕐 {REQUESTS[req_id]['timestamp']}"
    )
    link = await send_to_channel(context.bot, channel_text, photo_file_id)
    REQUESTS[req_id]['photo_channel_link'] = link

    save_requests()

    logger.info(f"Новая заявка {req_id}")

    # Google Sheets
    # Колонки: A=ID, B=Цех, C=Отделение, D=Оборудование, E=Срочность,
    #          F=Оператор, G=Время подачи, H=Механик, I=Время выполнения,
    #          J=Статус, K=Фото поломки, L=Комментарий механика, M=Фото выполнения
    try:
        sheet = get_sheet(workshop_code, WORKSHOPS.get(workshop_code, workshop_code))
        sheet.append_row([
            req_id,
            WORKSHOPS.get(workshop_code, ''),
            section_name,
            context.user_data.get('problem', ''),
            URGENCY_LEVELS.get(urgency_code, ''),
            user_name,
            REQUESTS[req_id]['timestamp'],
            '',   # H — Механик
            '',   # I — Время выполнения
            'Новая',  # J — Статус
            REQUESTS[req_id].get('photo_channel_link', ''),  # K — Фото поломки
            '',   # L — Комментарий механика
            '',   # M — Фото выполнения
        ])
    except Exception as e:
        logger.error(f"Ошибка записи в Sheets: {e}")

    # Уведомить всех механиков о новой заявке
    mechanics = get_all_mechanics()
    emoji = URGENCY_EMOJI.get(urgency_code, '')
    notif_text = (
        f"{emoji} <b>НОВАЯ ЗАЯВКА {req_id}!</b>\n"
        f"🏭 {WORKSHOPS.get(workshop_code, workshop_code)} — {section_name}\n"
        f"📝 {context.user_data.get('problem', '—')}\n"
        f"🚨 {URGENCY_LEVELS.get(urgency_code, urgency_code)}"
        f"{op_comment_line}"
    )
    notif_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Принять заявку", callback_data=f'accept_{req_id}')],
        [InlineKeyboardButton("📬 Все новые заявки", callback_data='mechanic_new_requests')],
    ])
    for mech_id in mechanics:
        try:
            await context.bot.send_message(
                chat_id=mech_id,
                text=notif_text,
                parse_mode='HTML',
                reply_markup=notif_keyboard
            )
        except Exception as e:
            logger.error(f"Ошибка уведомления механика {mech_id}: {e}")
    # Запустить ПОВТОРЯЮЩЕЕСЯ напоминание: пинговать слесарей каждые OVERDUE_MINUTES,
    # пока заявку не примут (или не отменят). Останавливается само внутри check_overdue.
    if context.job_queue is not None:
        arm_overdue_reminder(context.job_queue, req_id)
        logger.info(f"⏰ Напоминание для {req_id} запланировано (каждые {OVERDUE_MINUTES} мин)")
    else:
        logger.warning(f"⚠️ JobQueue недоступен — напоминание для {req_id} НЕ запланировано! "
                       f"Установи: pip install 'python-telegram-bot[job-queue]'")

    photo_text = "📸 Фото прикреплено" if photo_file_id else "📸 Без фото"
    text = (
        f"<b>✅ ЗАЯВКА ПОДАНА!</b>\n\n"
        f"🆔 Номер: <b>{req_id}</b>\n"
        f"🏭 Цех: {WORKSHOPS.get(workshop_code, workshop_code)}\n"
        f"🔧 Отделение: {section_name}\n"
        f"⚠️ Оборудование/проблема: {context.user_data.get('problem', '—')}\n"
        f"🚨 Срочность: {URGENCY_LEVELS.get(urgency_code, urgency_code)}\n"
        f"{photo_text}\n"
        f"🕐 Время: {REQUESTS[req_id]['timestamp']}\n\n"
        f"Слесарная группа получит заявку в ближайшее время!"
    )
    keyboard = [[InlineKeyboardButton("✅ Вернуться в меню", callback_data='role_operator')]]

    try:
        if edit_message:
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        else:
            await edit_or_send(update, context, text, keyboard)
    except Exception as e:
        logger.error(f"Ошибка отправки подтверждения заявки: {e}", exc_info=True)
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

    # Очистить данные потока — чтобы повторный тап по старой кнопке не создал дубль заявки
    for _k in ('workshop', 'section', 'problem', 'urgency', 'photo_file_id',
               'waiting_for_photo', 'waiting_for_op_comment', 'op_comment'):
        context.user_data.pop(_k, None)

    # Автоматически скинуть меню оператора после подачи заявки
    await operator_quick_menu(update, context)

def _filter_requests_by_days(reqs: list, days: int) -> list:
    """Фильтрует заявки по дате — только за последние `days` дней."""
    cutoff = datetime.now() - timedelta(days=days)
    result = []
    for r in reqs:
        try:
            ts = datetime.strptime(r['timestamp'], '%d.%m.%Y %H:%M')
            if ts >= cutoff:
                result.append(r)
        except Exception:
            result.append(r)
    return result

async def operator_my_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    keyboard = [
        [
            InlineKeyboardButton("📅 Сегодня",  callback_data='op_period_1'),
            InlineKeyboardButton("📅 3 дня",    callback_data='op_period_3'),
            InlineKeyboardButton("📅 Неделя",   callback_data='op_period_7'),
        ],
        [InlineKeyboardButton("🔙 Назад", callback_data='role_operator')],
    ]
    await _screen(update, context, "📋 <b>Мои заявки</b>\n\nЗа какой период показать?", keyboard)

async def operator_my_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    days = int(query.data.split('_')[-1])
    period_label = {1: 'сегодня', 3: 'за 3 дня', 7: 'за неделю'}[days]

    user_id = update.effective_user.id
    all_reqs = sorted(
        [r for r in REQUESTS.values() if r['user_id'] == user_id],
        key=lambda x: x['id'], reverse=True
    )
    my_reqs = _filter_requests_by_days(all_reqs, days)

    if not my_reqs:
        text = f"📋 <b>Мои заявки ({period_label})</b>\n\nЗаявок за этот период нет."
        keyboard = [
            [InlineKeyboardButton("🔙 К выбору периода", callback_data='operator_my_requests')],
        ]
    else:
        lines = [f"📋 <b>МОИ ЗАЯВКИ — {period_label.upper()} ({len(my_reqs)})</b>\n"]
        keyboard = []
        for r in my_reqs:
            urgency_e = URGENCY_EMOJI.get(r['urgency'], '')
            status_e  = {'new': '🆕', 'in_progress': '⏳', 'postponed': '⏸', 'done': '✅', 'cancelled': '❌'}.get(r['status'], '❓')
            entry = f"{status_e}{urgency_e} <b>{r['problem']}</b> — {r['timestamp']}"
            if r['status'] == 'done':
                entry += f"\n   🔧 {r.get('mechanic_name', '—')} | ✅ {r.get('done_time', '—')}"
                if r.get('done_comment'):
                    entry += f"\n   💬 {r['done_comment']}"
            elif r['status'] == 'in_progress':
                entry += f"\n   🔧 {r.get('mechanic_name', '—')} взял в работу"
            elif r['status'] == 'postponed':
                entry += f"\n   ⏸ {r.get('postpone_reason', '—')}"
            elif r['status'] == 'cancelled':
                entry += "\n   <i>Отменена тобой</i>"
            lines.append(entry)
            if r['status'] == 'new':
                keyboard.append([InlineKeyboardButton(
                    f"❌ Отменить: {r['problem'][:25]}",
                    callback_data=f'op_cancel_{r["id"]}'
                )])
        keyboard.append([InlineKeyboardButton("🔙 К выбору периода", callback_data='operator_my_requests')])
        text = "\n\n".join(lines)
        if len(text) > 4000:
            text = text[:4000] + "\n\n<i>... (обрезано, слишком много заявок)</i>"

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def operator_cancel_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    req_id = query.data.split('op_cancel_')[1]
    user_id = update.effective_user.id

    req = REQUESTS.get(req_id)
    if not req or req['user_id'] != user_id:
        await query.answer("❌ Заявка не найдена!", show_alert=True)
        return
    if req['status'] != 'new':
        await query.answer("❌ Нельзя отменить — заявка уже в работе!", show_alert=True)
        return
    await query.answer(f"✅ Заявка {req_id} отменена", show_alert=True)

    req['status'] = 'cancelled'
    save_requests()

    # Остановить повторяющиеся напоминания «заявку не приняли»
    _remove_jobs(context, f'overdue_{req_id}')

    # Обновить статус в Google Sheets
    try:
        req_cancel = REQUESTS.get(req_id, {})
        sheet = get_sheet(req_cancel.get('workshop', 'bread'), req_cancel.get('workshop_name'))
        col_values = sheet.col_values(1)
        if req_id in col_values:
            row_num = col_values.index(req_id) + 1
            sheet.update_cell(row_num, 10, 'Отменена')
    except Exception as e:
        logger.error(f"Ошибка обновления Sheets при отмене: {e}")

    # Обновить список заявок
    my_reqs = sorted(
        [r for r in REQUESTS.values() if r['user_id'] == user_id],
        key=lambda x: x['id'], reverse=True
    )
    lines = [f"📋 <b>МОИ ЗАЯВКИ ({len(my_reqs)})</b>\n"]
    keyboard = []
    for r in my_reqs:
        urgency_e = URGENCY_EMOJI.get(r['urgency'], '')
        status_e  = {'new': '🆕', 'in_progress': '⏳', 'postponed': '⏸', 'done': '✅', 'cancelled': '❌'}.get(r['status'], '❓')
        entry = f"{status_e}{urgency_e} <b>{r['problem']}</b> — {r['timestamp']}"
        if r['status'] == 'done':
            entry += f"\n   🔧 {r.get('mechanic_name', '—')} | ✅ {r.get('done_time', '—')}"
        elif r['status'] == 'in_progress':
            entry += f"\n   🔧 {r.get('mechanic_name', '—')} взял в работу"
        elif r['status'] == 'postponed':
            entry += f"\n   ⏸ {r.get('postpone_reason', '—')}"
        elif r['status'] == 'cancelled':
            entry += "\n   <i>Отменена тобой</i>"
        lines.append(entry)
        if r['status'] == 'new':
            keyboard.append([InlineKeyboardButton(
                f"❌ Отменить: {r['problem'][:25]}",
                callback_data=f'op_cancel_{r["id"]}'
            )])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='role_operator')])
    cancel_text = "\n\n".join(lines)
    if len(cancel_text) > 4000:
        cancel_text = cancel_text[:4000] + "\n\n<i>... (обрезано)</i>"
    await query.edit_message_text(cancel_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

# ============================================================================
# ИНТЕРФЕЙС МЕХ ГРУППЫ
# ============================================================================

async def mechanic_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    role = get_user_role(user_id)
    if role not in ('mechanic', 'admin'):
        await query.answer("❌ У тебя нет доступа!", show_alert=True)
        return
    await query.answer()
    _sync_user_name(update.effective_user)
    context.user_data['role'] = 'mechanic'

    new_count       = len([r for r in REQUESTS.values() if r['status'] == 'new'])
    in_prog_count   = len([r for r in REQUESTS.values() if r['status'] == 'in_progress'])
    postponed_count = len([r for r in REQUESTS.values() if r['status'] == 'postponed'])
    keyboard = [
        [InlineKeyboardButton(checkin_label(user_id),                callback_data='checkin_today')],
        [InlineKeyboardButton(f"📬 Новые заявки ({new_count})",      callback_data='mechanic_new_requests')],
        [InlineKeyboardButton(f"⏳ В работе ({in_prog_count})",       callback_data='mechanic_in_progress')],
        [InlineKeyboardButton(f"⏸ Отложенные ({postponed_count})",   callback_data='mechanic_postponed')],
        [InlineKeyboardButton("✅ Завершенные",                        callback_data='mechanic_completed')],
        [InlineKeyboardButton("📝 Прочие работы",                     callback_data='mechanic_other_tasks')],
        [InlineKeyboardButton("🏆 Рейтинг", callback_data='mechanic_rating')],
        [InlineKeyboardButton("🔙 Главное меню",                      callback_data='back_to_main')]
    ]
    await query.edit_message_text(
        "<b>🔧 Меню Слесарной группы</b>\n\nВыбери раздел:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def mechanic_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Экран выбора смены для отметки явки (дневная / суточная)."""
    user_id = update.effective_user.id
    if update.callback_query:
        await update.callback_query.answer()

    current = ATTENDANCE_TODAY.get(user_id)
    if current in SHIFTS:
        text = (f"✅ <b>Явка уже отмечена</b>\n\n"
                f"Смена: {shift_mark(current)}\n\n"
                f"Если ошибся — выбери другую смену:")
    else:
        text = "📍 <b>Отметка явки</b>\n\nВыбери свою смену:"

    await _screen(update, context, text, checkin_menu_keyboard())

async def mechanic_checkin_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отметить явку по выбранной смене (callback checkin_day / checkin_sutki)."""
    user_id   = update.effective_user.id
    user_name = update.effective_user.full_name or update.effective_user.first_name
    today_str = datetime.now().strftime('%d.%m.%Y')

    q = update.callback_query
    shift_key = 'day' if (q and q.data == 'checkin_day') else 'sutki'
    if q:
        await q.answer()

    already = ATTENDANCE_TODAY.get(user_id)
    ATTENDANCE_TODAY[user_id] = shift_key
    save_daily_state()
    mark_attendance(user_name, today_str, shift_key)

    # Геймификация: очки за явку — только за первую отметку в день (не за смену смены)
    if already is None:
        add_score(user_id, user_name, points=POINTS_ATTENDANCE, shifts=1)

    changed = " (смена изменена)" if already in SHIFTS and already != shift_key else ""
    await _screen(update, context,
        f"✅ <b>Явка отмечена!</b>{changed}\n\n"
        f"👤 {user_name}\n"
        f"🕒 Смена: {shift_mark(shift_key)}\n"
        f"📅 {today_str}  {datetime.now().strftime('%H:%M')}\n\n"
        f"Записано в Google Sheets.",
        [[InlineKeyboardButton("🔙 В меню", callback_data='role_mechanic')]]
    )

async def mechanic_rating_board(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Экран «🏆 Рейтинг» для слесаря: топ-3 недели + своё место и кого догонять."""
    if update.callback_query:
        await update.callback_query.answer()
    text = rating_board_text(update.effective_user.id)
    await _screen(update, context, text,
                  [[InlineKeyboardButton("🔙 В меню", callback_data='role_mechanic')]])

# ============================================================================
# КОНТРОЛЬ ППР (роль «Бригадир»)
# ============================================================================

_PPR_VERDICT_LABEL = {
    'done':      '✅ Выполнено',
    'postponed': '⏸ Отложено',
    'noshow':    '❌ Не пришёл',
}

def _ppr_status_label(ppr_key: str) -> str:
    """Короткий статус ППР: сначала вердикт бригадира, иначе самоотметка слесаря."""
    st = PPR_STATUS_TODAY.get(ppr_key, {})
    v = st.get('verified')
    if v:
        return _PPR_VERDICT_LABEL.get(v['status'], v['status'])
    s = st.get('status')
    if s == 'confirmed':
        return "🔧 слесарь начал"
    if s == 'postponed':
        return "⏸ слесарь отложил"
    return "⏳ нет отметки"

async def ppr_check_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список ППР на сегодня со статусами + кнопки подтверждения (для бригадира)."""
    query = update.callback_query
    if query and get_user_role(update.effective_user.id) not in ('brigadir', 'admin'):
        await query.answer("❌ У тебя нет доступа!", show_alert=True)
        return
    if query:
        await query.answer()
    uid = update.effective_user.id
    today = datetime.now(); weekday = today.weekday(); ymd = today.strftime('%Y%m%d')
    todays = sorted([p for p in load_ppr()
                     if p.get('weekday') == weekday and brig_sees_workshop(uid, p.get('workshop'))],
                    key=lambda x: x.get('time', ''))
    if not todays:
        hint = "На сегодня ППР нет. 👍"
        if brig_shops_of(uid):
            hint += "\n\n<i>Показаны только твои цеха. Изменить — «🏭 Мои цеха».</i>"
        await _screen(update, context, f"🗓 <b>ППР сегодня</b>\n\n{hint}",
                      [[InlineKeyboardButton("🔙 В меню", callback_data='role_brigadir')]])
        return
    lines = ["🗓 <b>ППР СЕГОДНЯ</b>\n"]
    keyboard = []
    for p in todays:
        key = f"{p['id']}_{ymd}"
        st = PPR_STATUS_TODAY.get(key, {})
        lines.append(f"🕐 {p.get('time','')} · {p.get('equipment','')} — {_ppr_status_label(key)}")
        if st.get('mechanic'):
            lines.append(f"   🔧 {st['mechanic']}")
        keyboard.append([InlineKeyboardButton(
            f"📝 {p.get('time','')} {p.get('equipment','')[:20]}",
            callback_data=f"pprv_{p['id']}_{ymd}")])
    keyboard.append([InlineKeyboardButton("🔙 В меню", callback_data='role_brigadir')])
    await _screen(update, context, "\n".join(lines), keyboard)

async def ppr_check_verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Карточка одного ППР с выбором вердикта."""
    query = update.callback_query
    parts = query.data.split('_', 2)          # pprv_{id}_{ymd}
    ppr_id, ymd = parts[1], parts[2]
    await query.answer()
    ppr = next((p for p in load_ppr() if str(p['id']) == str(ppr_id)), None)
    if not ppr:
        await query.answer("❌ ППР не найден", show_alert=True)
        return
    key = f"{ppr_id}_{ymd}"
    st = PPR_STATUS_TODAY.get(key, {})
    who = st.get('mechanic') or 'слесарь не отметился'
    text = (f"🧑‍🏭 <b>Подтверди итог ППР</b>\n\n"
            f"🔧 {ppr.get('equipment','')}\n📋 {ppr.get('task','')}\n"
            f"🕐 {ppr.get('time','')}\n👤 Слесарь: {who}\n\nЧто по факту?")
    kb = [
        [InlineKeyboardButton("✅ Пришёл и сделал", callback_data=f"pprd_{ppr_id}_{ymd}_done")],
        [InlineKeyboardButton("⏸ Отложено",        callback_data=f"pprd_{ppr_id}_{ymd}_postponed")],
        [InlineKeyboardButton("❌ Не пришёл",        callback_data=f"pprd_{ppr_id}_{ymd}_noshow")],
        [InlineKeyboardButton("🔙 К списку",         callback_data='pprcheck')],
    ]
    await _screen(update, context, text, kb)

async def ppr_check_verdict(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Применить вердикт бригадира: запись в Sheets + очки слесарю за выполненный ППР."""
    query = update.callback_query
    parts = query.data.split('_')             # pprd_{id}_{ymd}_{verdict}
    ppr_id, ymd, verdict = parts[1], parts[2], parts[3]
    await query.answer()
    ppr = next((p for p in load_ppr() if str(p['id']) == str(ppr_id)), None)
    if not ppr:
        await query.answer("❌ ППР не найден", show_alert=True)
        return
    key = f"{ppr_id}_{ymd}"
    st = PPR_STATUS_TODAY.setdefault(key, {})
    brig_name = update.effective_user.full_name or update.effective_user.first_name
    now = datetime.now().strftime('%d.%m.%Y %H:%M')
    date_str = datetime.strptime(ymd, '%Y%m%d').strftime('%d.%m.%Y')
    mech_name = st.get('mechanic') or '—'
    mech_id = st.get('mechanic_id')

    st['verified'] = {'status': verdict, 'by': brig_name, 'time': now}
    save_daily_state()

    status_txt = f"{_PPR_VERDICT_LABEL.get(verdict, verdict)} (подтв. бригадир)"
    log_ppr_to_sheets(ppr, date_str, status_txt, mech_name, now, note=f"Подтвердил: {brig_name}")

    pts_line = ""
    if verdict == 'done' and mech_id:
        add_score(mech_id, mech_name, points=POINTS_PPR)
        rebuild_rating_board()
        pts_line = f"\n🎮 Слесарю +{POINTS_PPR} очк. за ППР"
        try:
            await context.bot.send_message(
                chat_id=mech_id, parse_mode='HTML',
                text=(f"🧑‍🏭 Бригадир подтвердил твой ППР:\n🔧 {ppr.get('equipment','')}\n"
                      f"🎮 <b>+{POINTS_PPR} очк.!</b>"))
        except Exception as e:
            logger.error(f"Не удалось уведомить слесаря об очках за ППР: {e}")

    await _screen(update, context,
        f"{_PPR_VERDICT_LABEL.get(verdict, verdict)}\n\n"
        f"🔧 {ppr.get('equipment','')}\n👤 Слесарь: {mech_name}\n🕐 {now}{pts_line}",
        [[InlineKeyboardButton("🔙 К списку ППР", callback_data='pprcheck')]])

async def mechanic_new_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()

    new_reqs = [r for r in REQUESTS.values() if r['status'] == 'new']

    if not new_reqs:
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data='role_mechanic')]]
        await _screen(update, context, "📬 <b>Новые заявки</b>\n\nНовых заявок нет. 👍", keyboard)
        return

    urgency_order = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
    new_reqs.sort(key=lambda x: urgency_order.get(x['urgency'], 9))

    lines = [f"📬 <b>НОВЫЕ ЗАЯВКИ ({len(new_reqs)})</b>\n"]
    for r in new_reqs:
        emoji = URGENCY_EMOJI.get(r['urgency'], '')
        photo_mark = ' 📸' if r.get('photo_file_id') else ''
        oc = r.get('op_comment')
        oc_line = f"\n   💬 {oc}" if oc else ""
        lines.append(
            f"{emoji} <b>{r['problem']}{photo_mark}</b>\n"
            f"   🏭 {WORKSHOPS.get(r['workshop'], r['workshop'])}\n"
            f"   {URGENCY_LEVELS.get(r['urgency'], '')} | 🕐 {r['timestamp']}\n"
            f"   👷 {r['user_name']}{oc_line}\n"
        )

    keyboard = [
        [InlineKeyboardButton(f"✅ Принять {r['id']}", callback_data=f'accept_{r["id"]}')]
        for r in new_reqs
    ]
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='role_mechanic')])

    new_text = "\n".join(lines)
    if len(new_text) > 4000:
        new_text = new_text[:4000] + "\n\n<i>... (обрезано, кнопки принятия ниже)</i>"
    await _screen(update, context, new_text, keyboard)

async def mechanic_accept_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    req_id = query.data.split('_', 1)[1]

    if req_id not in REQUESTS:
        await query.answer("❌ Заявка не найдена!", show_alert=True)
        return

    req = REQUESTS[req_id]
    if req['status'] not in ('new', 'postponed'):
        mname = req.get('mechanic_name') or '—'
        if req['status'] == 'in_progress':
            msg = f"❌ Уже принял: {mname}"
        elif req['status'] == 'done':
            msg = f"✅ Уже выполнена: {mname}"
        else:
            msg = f"⚠️ Заявка недоступна (статус: {req['status']})"
        await query.answer(msg, show_alert=True)
        return

    await query.answer()
    # Сохраняем req_id и ждём выбора ETA
    context.user_data['accepting_req_id'] = req_id
    keyboard = [
        [InlineKeyboardButton("🚀 Уже иду / начал", callback_data='eta_now')],
        [InlineKeyboardButton("⚡ 15 минут",  callback_data='eta_15')],
        [InlineKeyboardButton("🕐 30 минут",  callback_data='eta_30')],
        [InlineKeyboardButton("🕑 1 час",     callback_data='eta_60')],
        [InlineKeyboardButton("🕒 2 часа",    callback_data='eta_120')],
        [InlineKeyboardButton("✏️ Другое",    callback_data='eta_other')],
    ]
    oc = req.get('op_comment')
    oc_line = f"\n💬 Коммент оператора: {oc}" if oc else ""
    eta_text = (
        f"<b>📝 {req['problem']}</b>\n"
        f"🏭 {WORKSHOPS.get(req['workshop'], '')} | 👷 {req['user_name']}"
        f"{oc_line}\n\n"
        f"⏱ <b>Через сколько приступишь?</b>"
    )
    eta_markup = InlineKeyboardMarkup(keyboard)
    try:
        await query.edit_message_text(eta_text, reply_markup=eta_markup, parse_mode='HTML')
    except Exception as e:
        logger.warning(f"edit_message_text failed in mechanic_accept_request: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=eta_text,
            reply_markup=eta_markup,
            parse_mode='HTML'
        )

async def mechanic_eta_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    eta_code = query.data.split('_')[1]
    req_id = context.user_data.pop('accepting_req_id', None)

    if not req_id or req_id not in REQUESTS:
        await query.answer("❌ Сессия истекла, прими заявку заново.", show_alert=True)
        return

    await query.answer()

    if eta_code == 'other':
        context.user_data['accepting_req_id'] = req_id
        context.user_data['waiting_for_eta'] = True
        await query.edit_message_text(
            "✏️ Напиши через сколько приступишь:\n"
            "Например: «через 45 минут», «после обеда»\n\n"
            "👇 Напиши в чат:",
            parse_mode='HTML'
        )
        return

    eta_labels = {'now': ETA_NOW_TEXT, '15': '15 минут', '30': '30 минут',
                  '60': '1 час', '120': '2 часа'}
    eta_text = eta_labels.get(eta_code, eta_code)
    await finalize_accept(update, context, req_id, eta_text, edit_message=True,
                          eta_minutes=ETA_CODE_MINUTES.get(eta_code))

async def mechanic_eta_text_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req_id = context.user_data.pop('accepting_req_id', None)
    context.user_data['waiting_for_eta'] = False
    # Защита: сессия могла истечь (перезапуск бота, устаревшее состояние)
    if not req_id or req_id not in REQUESTS:
        await update.message.reply_text("❌ Сессия истекла, прими заявку заново.")
        return
    eta_text = update.message.text
    await finalize_accept(update, context, req_id, eta_text, edit_message=False)

async def finalize_accept(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          req_id: str, eta_text: str, edit_message: bool,
                          eta_minutes: int = None):
    req = REQUESTS[req_id]
    mechanic_name = update.effective_user.full_name or update.effective_user.first_name

    now_dt = datetime.now()
    accept_time = now_dt.strftime('%d.%m.%Y %H:%M')
    REQUESTS[req_id]['status']       = 'in_progress'
    REQUESTS[req_id]['mechanic_id']  = update.effective_user.id
    REQUESTS[req_id]['mechanic_name'] = mechanic_name
    REQUESTS[req_id]['eta']          = eta_text
    REQUESTS[req_id]['accept_time']  = accept_time
    # Обещанный момент старта (только для кнопок; свободный текст не парсим)
    REQUESTS[req_id]['eta_minutes'] = eta_minutes
    REQUESTS[req_id]['eta_due'] = (
        (now_dt + timedelta(minutes=eta_minutes)).strftime('%d.%m.%Y %H:%M')
        if eta_minutes is not None else None
    )
    save_requests()

    # Мягкое напоминание по обещанному времени (не жёсткий пинг админам)
    if eta_minutes is not None and context.job_queue is not None:
        _remove_jobs(context, f'etaprom_{req_id}')
        context.job_queue.run_once(
            check_eta_promise,
            when=timedelta(minutes=eta_minutes + ETA_REMIND_GRACE_MINUTES),
            data={'req_id': req_id, 'mechanic_id': update.effective_user.id},
            name=f'etaprom_{req_id}'
        )

    # Остановить повторяющиеся напоминания «заявку не приняли»
    _remove_jobs(context, f'overdue_{req_id}')

    # Обновить Google Sheets — механик, статус, ETA
    try:
        sheet = get_sheet(REQUESTS[req_id].get('workshop', 'bread'), REQUESTS[req_id].get('workshop_name'))
        col_values = sheet.col_values(1)
        if req_id in col_values:
            row_num = col_values.index(req_id) + 1
            sheet.update(range_name=f'H{row_num}:J{row_num}',
                         values=[[mechanic_name, accept_time, f'В работе (ETA: {eta_text})']])
    except Exception as e:
        logger.error(f"Ошибка обновления Sheets при принятии {req_id}: {e}")

    # Запустить проверку просрочки в работе
    if context.job_queue is not None:
        context.job_queue.run_once(
            check_inprogress_overdue,
            when=timedelta(hours=OVERDUE_INPROGRESS_HOURS),
            data={'req_id': req_id, 'mechanic_id': update.effective_user.id},
            name=f'inprogress_{req_id}'
        )

    # Уведомить оператора с ETA
    await notify_user(
        context.bot, req['user_id'],
        f"🔧 <b>Ваша заявка принята в работу!</b>\n"
        f"📝 {req['problem']}\n"
        f"🔧 Слесарь: {mechanic_name}\n"
        f"{eta_line(eta_text)}"
    )

    logger.info(f"Заявка {req_id} принята слесарем {mechanic_name}, ETA: {eta_text}")

    # Показать фото оператора если есть
    if req.get('photo_file_id'):
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=req['photo_file_id'],
            caption=f"📸 Фото от оператора — {req['problem']}"
        )

    oc = req.get('op_comment')
    oc_line = f"💬 Коммент оператора: {oc}\n" if oc else ""
    text = (
        f"<b>✅ ЗАЯВКА ПРИНЯТА В РАБОТУ</b>\n\n"
        f"📝 <b>{req['problem']}</b>\n"
        f"🏭 {WORKSHOPS.get(req['workshop'], req['workshop'])}\n"
        f"🚨 {URGENCY_LEVELS.get(req['urgency'], '')}\n"
        f"👷 От: {req['user_name']}\n"
        f"{oc_line}"
        f"{eta_line(eta_text, second_person=True)}\n\n"
        f"Когда выполнишь — нажми «Завершить»."
    )
    keyboard = [
        [InlineKeyboardButton(f"✅ Завершить", callback_data=f'done_{req_id}')],
        [InlineKeyboardButton(f"⏸ Отложить",  callback_data=f'postpone_{req_id}')],
        [InlineKeyboardButton("🔙 Меню",       callback_data='role_mechanic')]
    ]
    if edit_message:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

# --- ЗАВЕРШЕНИЕ ЗАЯВКИ: шаг 1 — запросить комментарий ---

async def mechanic_done_ask_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    req_id = query.data.split('_', 1)[1]
    if req_id not in REQUESTS:
        await query.answer("❌ Заявка не найдена!", show_alert=True)
        return

    # Защита от двойных нажатий: завершать можно только то, что в работе/отложено
    req_status = REQUESTS[req_id]['status']
    if req_status == 'done':
        await query.answer(
            f"✅ Заявка уже выполнена ({REQUESTS[req_id].get('mechanic_name') or '—'})",
            show_alert=True
        )
        return
    if req_status not in ('in_progress', 'postponed'):
        await query.answer(
            f"⚠️ Заявку нельзя завершить (статус: {STATUS_LABELS.get(req_status, req_status)})",
            show_alert=True
        )
        return

    await query.answer()

    context.user_data['done_req_id'] = req_id
    context.user_data['waiting_for_done_comment'] = True
    context.user_data['last_menu_msg_id'] = query.message.message_id

    req = REQUESTS[req_id]
    await query.edit_message_text(
        f"✅ Завершаем заявку <b>{req_id}</b>\n"
        f"📝 {req['problem']}\n\n"
        f"<b>Напиши что было сделано:</b>\n"
        f"Например: «Заменил ремень», «Перезапустил двигатель»\n\n"
        f"👇 Напиши в чат:",
        parse_mode='HTML'
    )

# --- ЗАВЕРШЕНИЕ ЗАЯВКИ: шаг 2 — комментарий получен, запросить фото ---

async def done_comment_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['waiting_for_done_comment'] = False
    context.user_data['done_comment'] = update.message.text
    context.user_data['waiting_for_done_photo'] = True

    keyboard = [[InlineKeyboardButton("⏭ Пропустить", callback_data='skip_done_photo')]]
    await edit_or_send(update, context,
        f"✅ Комментарий записан: <b>{update.message.text}</b>\n\n"
        f"<b>Прикрепи фото выполненной работы</b> (необязательно)\n\n"
        f"📸 Отправь фото или нажми «Пропустить»:",
        keyboard
    )

# --- ЗАВЕРШЕНИЕ ЗАЯВКИ: шаг 3а — пропустить фото ---

async def mechanic_done_skip_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['waiting_for_done_photo'] = False
    context.user_data['done_photo_file_id'] = None
    await finalize_done(update, context, edit_message=True)

# --- ЗАВЕРШЕНИЕ ЗАЯВКИ: шаг 3б — фото получено ---

async def mechanic_done_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('waiting_for_done_photo'):
        return
    context.user_data['waiting_for_done_photo'] = False
    photo = update.message.photo[-1]
    context.user_data['done_photo_file_id'] = photo.file_id
    await finalize_done(update, context, edit_message=False)

# --- ЗАВЕРШЕНИЕ ЗАЯВКИ: финализация ---

async def finalize_done(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_message: bool = True):
    req_id = context.user_data.pop('done_req_id', None)
    if not req_id or req_id not in REQUESTS:
        return

    # Защита от гонки: если заявку уже закрыли (двойной путь фото/пропуск) — выходим
    if REQUESTS[req_id]['status'] == 'done':
        logger.info(f"finalize_done: заявка {req_id} уже выполнена, пропускаем повторную финализацию")
        return

    done_time    = datetime.now().strftime('%d.%m.%Y %H:%M')
    mechanic_name = update.effective_user.full_name or update.effective_user.first_name
    done_comment  = context.user_data.pop('done_comment', '')
    done_photo    = context.user_data.pop('done_photo_file_id', None)

    REQUESTS[req_id]['status']            = 'done'
    REQUESTS[req_id]['done_time']         = done_time
    REQUESTS[req_id]['mechanic_name']     = mechanic_name
    REQUESTS[req_id]['done_comment']      = done_comment
    REQUESTS[req_id]['done_photo_file_id'] = done_photo

    # Отправить завершение в канал (всегда, с фото или без)
    req = REQUESTS[req_id]
    done_channel_text = (
        f"✅ <b>ВЫПОЛНЕНА — {req_id}</b>\n"
        f"🏭 {WORKSHOPS.get(req['workshop'], '')} | {req.get('problem', '')}\n"
        f"💬 {done_comment}\n"
        f"🔧 Слесарь: {mechanic_name} | ✅ {done_time}"
    )
    done_link = await send_to_channel(context.bot, done_channel_text, done_photo)
    REQUESTS[req_id]['done_photo_channel_link'] = done_link

    save_requests()

    req = REQUESTS[req_id]
    logger.info(f"Заявка {req_id} выполнена слесарем {mechanic_name}")

    # Геймификация: очки за выполнение (+ бонус за быстрый приём) и обновление лидерборда
    order_before = _week_order()          # снимок мест ДО начисления (для детекта обгона)
    earned_points = award_completion(req)
    # Бонус за честный ETA: уложился в обещанный срок + люфт на работу
    eta_kept = _eta_promise_kept(req)
    if eta_kept:
        add_score(req.get('mechanic_id'), req.get('mechanic_name'), points=POINTS_ETA_KEPT)
        earned_points += POINTS_ETA_KEPT
    rebuild_rating_board()

    # Обновить Google Sheets — одним батч-запросом
    try:
        sheet = get_sheet(req.get('workshop', 'bread'), req.get('workshop_name'))
        col_values = sheet.col_values(1)
        if req_id in col_values:
            row_num = col_values.index(req_id) + 1
            # H:J — механик, время, статус (K не трогаем — там фото поломки)
            sheet.update(
                range_name=f'H{row_num}:J{row_num}',
                values=[[mechanic_name, done_time, 'Выполнена']]
            )
            # L:M — комментарий механика и фото выполнения
            sheet.update(
                range_name=f'L{row_num}:M{row_num}',
                values=[[done_comment or '', REQUESTS[req_id].get('done_photo_channel_link', '')]]
            )
            logger.info(f"Sheets обновлён для {req_id}, механик: {mechanic_name}")
        else:
            logger.warning(f"Заявка {req_id} не найдена в Sheets")
    except Exception as e:
        logger.error(f"Ошибка обновления Sheets: {e}", exc_info=True)

    # Уведомить оператора + скинуть ему меню
    comment_line = f"\n💬 Что сделано: {done_comment}" if done_comment else ""
    await notify_user(
        context.bot, req['user_id'],
        f"✅ <b>Ваша заявка {req_id} выполнена!</b>\n"
        f"📝 {req['problem']}\n"
        f"🔧 Слесарь: {mechanic_name}\n"
        f"🕐 Время: {done_time}"
        f"{comment_line}"
    )
    # Попросить оператора оценить качество ремонта
    rate_keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("👍 Хорошо",  callback_data=f'rate_{req_id}_good'),
        InlineKeyboardButton("😐 Нормально", callback_data=f'rate_{req_id}_ok'),
        InlineKeyboardButton("👎 Плохо",   callback_data=f'rate_{req_id}_bad'),
    ]])
    try:
        await context.bot.send_message(
            chat_id=req['user_id'],
            text=(
                f"⭐ <b>Оцените качество ремонта по заявке {req_id}</b>\n"
                f"📝 {req['problem']}"
            ),
            reply_markup=rate_keyboard,
            parse_mode='HTML'
        )
    except Exception as e:
        logger.error(f"Ошибка отправки запроса оценки оператору: {e}")

    # Автоматически скинуть меню оператору после завершения его заявки
    op_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Новая заявка",  callback_data='operator_new_request')],
        [InlineKeyboardButton("📋 Мои заявки",    callback_data='operator_my_requests')],
    ])
    try:
        await context.bot.send_message(
            chat_id=req['user_id'],
            text="<b>👷 Меню Оператора</b>",
            reply_markup=op_keyboard,
            parse_mode='HTML'
        )
    except Exception as e:
        logger.error(f"Ошибка отправки меню оператору: {e}")

    photo_line = "\n📸 Фото прикреплено" if done_photo else ""
    eta_line_done = f"\n⏱ Слово сдержал (+{POINTS_ETA_KEPT}) 👍" if eta_kept else ""
    text = (
        f"<b>✅ ЗАЯВКА ВЫПОЛНЕНА!</b>\n\n"
        f"🆔 Номер: <b>{req_id}</b>\n"
        f"🏭 Цех: {WORKSHOPS.get(req['workshop'], req['workshop'])}\n"
        f"📝 Проблема: {req['problem']}\n"
        f"💬 Что сделано: {done_comment}\n"
        f"🕐 Подана: {req['timestamp']}\n"
        f"✅ Выполнена: {done_time}\n"
        f"🔧 Выполнил: {mechanic_name}"
        f"{photo_line}"
        f"{eta_line_done}"
        f"\n\n{completion_game_line(req.get('mechanic_id'), earned_points, order_before)}"
    )
    keyboard = [
        [InlineKeyboardButton("🏆 Рейтинг", callback_data='mechanic_rating')],
        [InlineKeyboardButton("🔙 Меню мех группы", callback_data='role_mechanic')],
    ]

    if edit_message:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    else:
        await edit_or_send(update, context, text, keyboard)
        if done_photo:
            await context.bot.send_photo(chat_id=update.effective_chat.id, photo=done_photo, caption=f"📸 Фото выполненной работы — {req_id}")

    # Автоматически скинуть свежее меню после завершения задачи
    await mechanic_quick_menu(update, context)

# --- ОЦЕНКА КАЧЕСТВА РЕМОНТА ОПЕРАТОРОМ ---

RATING_LABELS = {'good': '👍 Хорошо', 'ok': '😐 Нормально', 'bad': '👎 Плохо'}

async def operator_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    # callback_data: rate_<req_id>_<value>; req_id может содержать '_', берём value справа
    payload = query.data[len('rate_'):]
    try:
        req_id, value = payload.rsplit('_', 1)
    except ValueError:
        await query.answer("❌ Ошибка данных", show_alert=True)
        return

    if req_id not in REQUESTS:
        await query.answer("❌ Заявка не найдена", show_alert=True)
        return
    if value not in RATING_LABELS:
        await query.answer("❌ Неизвестная оценка", show_alert=True)
        return

    # Защита от двойных нажатий: оценить можно только один раз
    if REQUESTS[req_id].get('rating'):
        await query.answer(
            f"✅ Вы уже оценили: {RATING_LABELS.get(REQUESTS[req_id]['rating'], '')}",
            show_alert=True
        )
        return

    REQUESTS[req_id]['rating']      = value
    REQUESTS[req_id]['rating_time'] = datetime.now().strftime('%d.%m.%Y %H:%M')
    save_requests()
    logger.info(f"Заявка {req_id} оценена оператором: {value}")

    # Геймификация: очки за оценку + журнал в лист «Оценки»
    pts = award_rating(REQUESTS[req_id], value)
    operator_name = update.effective_user.full_name or update.effective_user.first_name
    log_rating_to_sheets(REQUESTS[req_id], value, pts, operator_name)
    rebuild_rating_board()

    await query.answer("Спасибо за оценку! 🙏")
    try:
        await query.edit_message_text(
            f"⭐ <b>Спасибо за оценку!</b>\n"
            f"Заявка {req_id}: {RATING_LABELS[value]}",
            parse_mode='HTML'
        )
    except Exception as e:
        logger.warning(f"edit_message_text failed in operator_rate: {e}")

    # Уведомить слесаря об оценке его работы
    mech_id = REQUESTS[req_id].get('mechanic_id')
    if mech_id:
        try:
            await context.bot.send_message(
                chat_id=mech_id,
                text=(
                    f"⭐ Оператор оценил вашу работу по заявке {req_id}: "
                    f"<b>{RATING_LABELS[value]}</b>\n"
                    f"🎮 {'+' if pts >= 0 else ''}{pts} очк. · {mechanic_chase_text(mech_id)}"
                ),
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"Ошибка уведомления слесаря об оценке: {e}")

# --- ОТЛОЖИТЬ ЗАЯВКУ ---

async def mechanic_postpone_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    req_id = query.data.split('_', 1)[1]
    if req_id not in REQUESTS:
        await query.answer("❌ Заявка не найдена!", show_alert=True)
        return

    req = REQUESTS[req_id]
    if req['status'] not in ('in_progress', 'new'):
        await query.answer("⚠️ Заявку нельзя отложить!", show_alert=True)
        return

    await query.answer()

    context.user_data['postpone_req_id'] = req_id
    context.user_data['waiting_for_postpone_reason'] = True
    context.user_data['last_menu_msg_id'] = query.message.message_id

    await query.edit_message_text(
        f"⏸ Откладываем заявку <b>{req_id}</b>\n"
        f"🏭 {WORKSHOPS.get(req['workshop'], '')} — {req['problem']}\n\n"
        f"<b>Напиши причину откладывания:</b>\n"
        f"👇 Просто напиши в чат:",
        parse_mode='HTML'
    )

async def postpone_reason_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req_id = context.user_data.pop('postpone_req_id', None)
    context.user_data['waiting_for_postpone_reason'] = False
    reason = update.message.text

    if not req_id or req_id not in REQUESTS:
        await update.message.reply_text("❌ Заявка не найдена.")
        return

    mechanic_name  = update.effective_user.full_name or update.effective_user.first_name
    postponed_time = datetime.now().strftime('%d.%m.%Y %H:%M')

    REQUESTS[req_id]['status']         = 'postponed'
    REQUESTS[req_id]['postpone_reason'] = reason
    REQUESTS[req_id]['postponed_by']   = mechanic_name
    REQUESTS[req_id]['postponed_time'] = postponed_time
    save_requests()

    req = REQUESTS[req_id]

    # Обновить Google Sheets
    try:
        sheet = get_sheet(req.get('workshop', 'bread'), req.get('workshop_name'))
        col_values = sheet.col_values(1)
        if req_id in col_values:
            row_num = col_values.index(req_id) + 1
            sheet.update_cell(row_num, 8,  mechanic_name)          # Слесарь
            sheet.update_cell(row_num, 10, f'Отложена: {reason}')  # Статус
    except Exception as e:
        logger.error(f"Ошибка обновления Sheets: {e}")

    # Уведомить оператора
    await notify_user(
        context.bot, req['user_id'],
        f"⏸ <b>Ваша заявка {req_id} отложена</b>\n"
        f"📝 {req['problem']}\n"
        f"🔧 Слесарь: {mechanic_name}\n"
        f"📋 Причина: {reason}"
    )

    keyboard = [[InlineKeyboardButton("🔙 Меню мех группы", callback_data='role_mechanic')]]
    await edit_or_send(update, context,
        f"⏸ <b>Заявка {req_id} отложена</b>\n\n"
        f"📝 Причина: {reason}",
        keyboard
    )

    # Автоматически скинуть свежее меню после откладывания
    await mechanic_quick_menu(update, context)

async def mechanic_postponed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    postponed = [r for r in REQUESTS.values() if r['status'] == 'postponed']

    if not postponed:
        text = "⏸ <b>Отложенные</b>\n\nОтложенных заявок нет."
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data='role_mechanic')]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        return

    lines = [f"⏸ <b>ОТЛОЖЕННЫЕ ({len(postponed)})</b>\n"]
    keyboard = []
    for r in postponed:
        emoji = URGENCY_EMOJI.get(r['urgency'], '')
        lines.append(
            f"{emoji} <b>[{r['id']}] {r['problem']}</b>\n"
            f"   🏭 {WORKSHOPS.get(r['workshop'], r['workshop'])}\n"
            f"   ⏸ Причина: {r.get('postpone_reason', '—')}\n"
            f"   🔧 {r.get('postponed_by', '—')} | 📅 {r['timestamp']}\n"
        )
        keyboard.append([InlineKeyboardButton(f"✅ Принять [{r['id']}] {r['problem'][:20]}", callback_data=f'accept_{r["id"]}')])

    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='role_mechanic')])
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def mechanic_in_progress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    in_prog = [r for r in REQUESTS.values() if r['status'] == 'in_progress']

    if not in_prog:
        text = "⏳ <b>В работе</b>\n\nНет заявок в работе."
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data='role_mechanic')]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        return

    lines = [f"⏳ <b>В РАБОТЕ ({len(in_prog)})</b>\n"]
    keyboard = []
    for r in in_prog:
        emoji = URGENCY_EMOJI.get(r['urgency'], '')
        lines.append(
            f"{emoji} <b>[{r['id']}] {r['problem']}</b>\n"
            f"   🏭 {WORKSHOPS.get(r['workshop'], r['workshop'])}\n"
            f"   🔧 {r.get('mechanic_name', '—')} | 🕐 {r['timestamp']}\n"
        )
        keyboard.append([
            InlineKeyboardButton(f"✅ Завершить {r['id']}", callback_data=f'done_{r["id"]}'),
            InlineKeyboardButton(f"⏸ Отложить {r['id']}",  callback_data=f'postpone_{r["id"]}'),
        ])

    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='role_mechanic')])
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def mechanic_completed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [
            InlineKeyboardButton("📅 Сегодня", callback_data='mech_done_1'),
            InlineKeyboardButton("📅 3 дня",   callback_data='mech_done_3'),
            InlineKeyboardButton("📅 Неделя",  callback_data='mech_done_7'),
        ],
        [InlineKeyboardButton("🔙 Назад", callback_data='role_mechanic')],
    ]
    await query.edit_message_text(
        "✅ <b>Завершённые заявки</b>\n\nЗа какой период показать?",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
    )

async def mechanic_done_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    days = int(query.data.split('_')[-1])
    period_label = {1: 'сегодня', 3: 'за 3 дня', 7: 'за неделю'}[days]

    all_done = sorted(
        [r for r in REQUESTS.values() if r['status'] == 'done'],
        key=lambda x: x.get('done_time', x['timestamp']), reverse=True
    )
    done_reqs = _filter_requests_by_days(all_done, days)

    if not done_reqs:
        text = f"✅ <b>Завершённые ({period_label})</b>\n\nЗа этот период нет завершённых заявок."
    else:
        lines = [f"✅ <b>ЗАВЕРШЁННЫЕ — {period_label.upper()} ({len(done_reqs)})</b>\n"]
        for r in done_reqs:
            urgency_e = URGENCY_EMOJI.get(r.get('urgency', ''), '')
            entry = (
                f"{urgency_e}✅ <b>{r['problem']}</b>\n"
                f"   🏭 {WORKSHOPS.get(r['workshop'], r['workshop'])}\n"
                f"   🔧 {r.get('mechanic_name', '—')} | ✅ {r.get('done_time', '—')}\n"
                f"   📅 Подана: {r['timestamp']}"
            )
            if r.get('done_comment'):
                entry += f"\n   💬 {r['done_comment']}"
            lines.append(entry)
        text = "\n\n".join(lines)
        if len(text) > 4000:
            text = text[:4000] + "\n\n<i>... (обрезано)</i>"

    keyboard = [
        [InlineKeyboardButton("🔙 К выбору периода", callback_data='mechanic_completed')],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

# ============================================================================
# ПРОСРОЧКА — проверка что заявку не приняли вовремя
# ============================================================================

def _remove_jobs(context: ContextTypes.DEFAULT_TYPE, name: str):
    """Снять запланированные задачи по имени (например, остановить напоминания)."""
    if context.job_queue is None:
        return
    for job in context.job_queue.get_jobs_by_name(name):
        job.schedule_removal()

def arm_overdue_reminder(job_queue, req_id: str):
    """Завести повторяющееся напоминание о непринятой заявке (если ещё не заведено)."""
    if job_queue is None:
        return
    name = f'overdue_{req_id}'
    # Не плодим дубликаты, если уже есть активное напоминание
    if job_queue.get_jobs_by_name(name):
        return
    job_queue.run_repeating(
        check_overdue,
        interval=timedelta(minutes=OVERDUE_MINUTES),
        first=timedelta(minutes=OVERDUE_MINUTES),
        data={'req_id': req_id, 'count': 0},
        name=name
    )

async def check_overdue(context: ContextTypes.DEFAULT_TYPE):
    data   = context.job.data
    req_id = data['req_id']

    req = REQUESTS.get(req_id)
    # Заявку приняли / отменили / удалили — прекращаем напоминания
    if not req or req['status'] != 'new':
        context.job.schedule_removal()
        return

    data['count'] = data.get('count', 0) + 1
    elapsed = OVERDUE_MINUTES * data['count']
    logger.info(f"⏰ Напоминание #{data['count']} по заявке {req_id} (не принята {elapsed} мин)")

    emoji = URGENCY_EMOJI.get(req['urgency'], '🔴')
    overdue_text = (
        f"⚠️ <b>ЗАЯВКА НЕ ПРИНЯТА УЖЕ {elapsed} МИН!</b>\n"
        f"🔁 Напоминание #{data['count']}\n\n"
        f"{emoji} <b>{req_id}</b> | {WORKSHOPS.get(req['workshop'], '')}\n"
        f"📝 {req['problem']}\n"
        f"🚨 {URGENCY_LEVELS.get(req['urgency'], '')}\n"
        f"👷 Оператор: {req['user_name']}"
    )
    overdue_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Принять заявку", callback_data=f'accept_{req_id}')],
    ])
    # Каждый раз берём актуальный список слесарей (вдруг добавили новых)
    for mech_id in get_all_mechanics():
        try:
            await context.bot.send_message(
                chat_id=mech_id,
                text=overdue_text,
                parse_mode='HTML',
                reply_markup=overdue_keyboard
            )
        except Exception as e:
            logger.error(f"Ошибка уведомления механика {mech_id}: {e}")

async def check_eta_promise(context: ContextTypes.DEFAULT_TYPE):
    """Мягкое напоминание слесарю по его же обещанию: «сказал N минут — как там?».
    Только слесарю, без пинга админам — это напоминание, а не жалоба."""
    data = context.job.data
    req_id = data['req_id']
    req = REQUESTS.get(req_id)
    if not req or req['status'] != 'in_progress':
        return  # уже завершена/отложена — молчим
    promised = req.get('eta') or '—'
    await notify_user(
        context.bot, data['mechanic_id'],
        f"⏱ <b>Как там заявка {req_id}?</b>\n\n"
        f"🔧 {req.get('problem', '')}\n"
        f"🗣 Ты обещал: <b>{promised}</b>\n\n"
        f"Если сделал — нажми «✅ Завершить», если нет — ничего страшного, "
        f"просто держим оператора в курсе."
    )

async def check_inprogress_overdue(context: ContextTypes.DEFAULT_TYPE):
    """Пинг если заявка висит в работе дольше OVERDUE_INPROGRESS_HOURS часов."""
    data = context.job.data
    req_id = data['req_id']
    mechanic_id = data['mechanic_id']

    req = REQUESTS.get(req_id)
    if not req or req['status'] != 'in_progress':
        return  # уже завершена или отложена

    emoji = URGENCY_EMOJI.get(req['urgency'], '🔴')
    msg = (
        f"⏰ <b>ЗАЯВКА В РАБОТЕ УЖЕ {OVERDUE_INPROGRESS_HOURS} ЧАСА!</b>\n\n"
        f"{emoji} <b>{req_id}</b> | {WORKSHOPS.get(req['workshop'], '')}\n"
        f"🔧 {req['problem']}\n"
        f"👷 Оператор: {req['user_name']}\n"
        f"🕐 Принята: {req.get('accept_time', '—')}"
    )
    # Пинг механику
    await notify_user(context.bot, mechanic_id, msg)
    # Пинг всем админам
    await notify_admins(context.bot, f"⚠️ <b>Длительная заявка у слесаря {req.get('mechanic_name', '—')}</b>\n\n" + msg)

# ============================================================================
# ППР — ТОЧНЫЕ УВЕДОМЛЕНИЯ, ПОДТВЕРЖДЕНИЕ, ОТКЛАДЫВАНИЕ
# ============================================================================

def schedule_ppr_today(job_queue, today: datetime = None):
    """Планирует run_once-задачи ППР на сегодня при старте или в полночь."""
    if today is None:
        today = datetime.now()
    weekday  = today.weekday()
    ymd      = today.strftime('%Y%m%d')
    date_str = today.strftime('%d.%m.%Y')
    ppr_list = load_ppr()
    today_ppr = [p for p in ppr_list if p.get('weekday') == weekday and not _ppr_muted(p)]
    now = datetime.now()
    scheduled = 0
    for ppr in today_ppr:
        try:
            h, m     = map(int, ppr['time'].split(':'))
            task_dt  = today.replace(hour=h, minute=m, second=0, microsecond=0)
            pre_dt   = task_dt - timedelta(minutes=30)
            job_data = {'ppr': ppr, 'date': date_str, 'ymd': ymd}
            pre_name = f'ppr_pre_{ppr["id"]}_{ymd}'
            on_name  = f'ppr_on_{ppr["id"]}_{ymd}'
            # Идемпотентность: убрать ранее поставленные задачи с теми же именами,
            # чтобы повторный вызов (после добавления/редактирования ППР) не задвоил уведомления
            for nm in (pre_name, on_name):
                for old in job_queue.get_jobs_by_name(nm):
                    old.schedule_removal()
            if pre_dt > now:
                job_queue.run_once(send_ppr_pre_notification,
                                   when=(pre_dt - now).total_seconds(), data=job_data, name=pre_name)
                scheduled += 1
            if task_dt > now:
                job_queue.run_once(send_ppr_on_time,
                                   when=(task_dt - now).total_seconds(), data=job_data, name=on_name)
                scheduled += 1
        except Exception as e:
            logger.error(f"Ошибка планирования ППР {ppr.get('id')}: {e}")
    logger.info(f"ППР сегодня: {len(today_ppr)} задач, запланировано {scheduled} уведомлений")

def _unschedule_ppr_today(job_queue, ppr_id):
    """Снять сегодняшние уведомления конкретного ППР (при редактировании/удалении)."""
    if job_queue is None:
        return
    ymd = datetime.now().strftime('%Y%m%d')
    for nm in (f'ppr_pre_{ppr_id}_{ymd}', f'ppr_on_{ppr_id}_{ymd}'):
        for job in job_queue.get_jobs_by_name(nm):
            job.schedule_removal()

def _resync_ppr_jobs(job_queue, ppr_id=None):
    """Перепланировать сегодняшние ППР-уведомления после изменения графика.
    Если указан ppr_id — сначала снять его старые задачи (мог смениться день/время)."""
    if job_queue is None:
        return
    try:
        if ppr_id is not None:
            _unschedule_ppr_today(job_queue, ppr_id)
        schedule_ppr_today(job_queue)  # идемпотентна — дублей не будет
    except Exception as e:
        logger.error(f"Ошибка перепланировки ППР: {e}")

async def ppr_midnight_setup(context: ContextTypes.DEFAULT_TYPE):
    """Каждую полночь: сбросить статусы, явку и запланировать новые уведомления."""
    PPR_STATUS_TODAY.clear()
    ATTENDANCE_TODAY.clear()
    save_daily_state()
    schedule_ppr_today(context.job_queue)

async def daily_report_job_evening(context: ContextTypes.DEFAULT_TYPE):
    """22:00 — итоговый отчёт дня в лист 'Отчёт дня' + сообщение админу."""
    today_str = datetime.now().strftime('%d.%m.%Y')
    # Собрать статистику по слесарям за сегодня
    stats: dict = {}  # {mechanic_name: {accepted, done, postponed}}
    for r in REQUESTS.values():
        mname = r.get('mechanic_name')
        if not mname:
            continue
        # Заявки принятые/завершённые/отложенные сегодня
        ts = r.get('timestamp', '')
        done_t = r.get('done_time', '')
        is_today_ts   = ts.startswith(today_str)
        is_today_done = done_t.startswith(today_str)
        if not (is_today_ts or is_today_done):
            continue
        if mname not in stats:
            stats[mname] = {'accepted': 0, 'done': 0, 'postponed': 0}
        if r['status'] in ('in_progress', 'done', 'postponed'):
            stats[mname]['accepted'] += 1
        if r['status'] == 'done' and is_today_done:
            stats[mname]['done'] += 1
        if r['status'] == 'postponed':
            stats[mname]['postponed'] += 1

    if not stats:
        await notify_admins(context.bot,
            f"📊 <b>Отчёт дня {today_str}</b>\n\nСегодня заявок не было.")
        return

    # Записать в Sheets
    try:
        sheet = get_report_sheet()
        for mname, s in stats.items():
            total = s['accepted']
            sheet.append_row([today_str, mname, s['accepted'], s['done'], s['postponed'], total])
    except Exception as e:
        logger.error(f"Ошибка записи отчёта дня: {e}")

    # Отправить сводку админу
    lines = [f"📊 <b>ИТОГ ДНЯ {today_str}</b>\n"]
    for mname, s in sorted(stats.items()):
        lines.append(
            f"👤 <b>{mname}</b>\n"
            f"   ✅ Закрыто: {s['done']}  |  📥 Принято: {s['accepted']}  |  ⏸ Отложено: {s['postponed']}"
        )
    await notify_admins(context.bot, "\n".join(lines))

async def _notify_brigadiers_ppr(context, ppr, ymd, when_label: str):
    """Уведомить бригадиров о ППР (heads-up + кнопка подтвердить итог)."""
    brigadiers = get_all_brigadiers()
    if not brigadiers:
        return
    text = (
        f"🧑‍🏭 <b>ППР {when_label}</b>\n\n"
        f"🏭 {WORKSHOPS.get(ppr['workshop'], ppr['workshop'])}\n"
        f"🔧 {ppr['equipment']}\n"
        f"📋 {ppr['task']}\n"
        f"🕐 Плановое время: {ppr['time']}\n\n"
        f"Проконтролируй и отметь итог 👇"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Подтвердить итог", callback_data=f"pprv_{ppr['id']}_{ymd}")],
    ])
    for uid in brigadiers:
        if not brig_sees_workshop(uid, ppr.get('workshop')):
            continue  # не его цех
        try:
            await context.bot.send_message(chat_id=uid, text=text,
                                           reply_markup=kb, parse_mode='HTML')
        except Exception as e:
            logger.error(f"ППР бригадиру {uid}: {e}")

async def send_ppr_pre_notification(context: ContextTypes.DEFAULT_TYPE):
    """Уведомление за 30 минут до ППР."""
    data = context.job.data
    ppr  = data['ppr']
    ymd  = data['ymd']
    ppr_key = f"{ppr['id']}_{ymd}"
    if PPR_STATUS_TODAY.get(ppr_key, {}).get('status') in ('confirmed', 'postponed'):
        return
    if _ppr_muted(ppr):   # цех на паузе — не рассылаем
        return
    mechanics = get_all_mechanics()
    text = (
        f"🔔 <b>ППР через 30 минут!</b>\n\n"
        f"🏭 {WORKSHOPS.get(ppr['workshop'], ppr['workshop'])}\n"
        f"🔧 {ppr['equipment']}\n"
        f"📋 {ppr['task']}\n"
        f"🕐 Плановое время: {ppr['time']}\n\n"
        f"Ты будешь выполнять?"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, начинаю",  callback_data=f'pprc_{ppr["id"]}_{ymd}')],
        [InlineKeyboardButton("⏸ Отложить",      callback_data=f'pprp_{ppr["id"]}_{ymd}')],
    ])
    for mech_id in mechanics:
        try:
            await context.bot.send_message(chat_id=mech_id, text=text,
                                           reply_markup=keyboard, parse_mode='HTML')
        except Exception as e:
            logger.error(f"ППР пре-уведомление {mech_id}: {e}")
    await _notify_brigadiers_ppr(context, ppr, ymd, "через 30 минут")

async def send_ppr_on_time(context: ContextTypes.DEFAULT_TYPE):
    """Уведомление в точное время ППР (если ещё не подтверждено)."""
    data = context.job.data
    ppr  = data['ppr']
    ymd  = data['ymd']
    ppr_key = f"{ppr['id']}_{ymd}"
    if PPR_STATUS_TODAY.get(ppr_key, {}).get('status') in ('confirmed', 'postponed'):
        return
    if _ppr_muted(ppr):   # цех на паузе — не рассылаем
        return
    mechanics = get_all_mechanics()
    text = (
        f"⏰ <b>ВРЕМЯ ППР!</b>\n\n"
        f"🏭 {WORKSHOPS.get(ppr['workshop'], ppr['workshop'])}\n"
        f"🔧 {ppr['equipment']}\n"
        f"📋 {ppr['task']}\n"
        f"🕐 {ppr['time']} — пора начинать!\n\n"
        f"Подтверди:"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Начинаю",  callback_data=f'pprc_{ppr["id"]}_{ymd}')],
        [InlineKeyboardButton("⏸ Отложить", callback_data=f'pprp_{ppr["id"]}_{ymd}')],
    ])
    for mech_id in mechanics:
        try:
            await context.bot.send_message(chat_id=mech_id, text=text,
                                           reply_markup=keyboard, parse_mode='HTML')
        except Exception as e:
            logger.error(f"ППР точное время {mech_id}: {e}")
    await _notify_brigadiers_ppr(context, ppr, ymd, "сейчас")

def _ppr_taken_by_other(ppr_key: str, user_id: int):
    """Если ППР уже взят другим слесарём — вернуть имя взявшего, иначе None."""
    cur = PPR_STATUS_TODAY.get(ppr_key)
    if cur and cur.get('status') == 'confirmed' and cur.get('mechanic_id') != user_id:
        return cur.get('mechanic') or 'другой слесарь'
    return None

async def ppr_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 1: слесарь нажал «Начинаю» → выбор «один / с напарником» (если ещё не занято)."""
    query = update.callback_query
    parts  = query.data.split('_', 2)          # pprc_{ppr_id}_{ymd}
    ppr_id, ymd = parts[1], parts[2]
    ppr_key = f"{ppr_id}_{ymd}"

    cur = PPR_STATUS_TODAY.get(ppr_key)
    if cur and cur.get('status') == 'confirmed':
        if cur.get('mechanic_id') == update.effective_user.id:
            await query.answer("Ты уже взял этот ППР", show_alert=True)
        else:
            await query.answer(f"❌ Уже взял: {cur.get('mechanic')}", show_alert=True)
        return

    ppr = next((p for p in load_ppr() if str(p['id']) == str(ppr_id)), None)
    if not ppr:
        await query.answer("❌ ППР не найден", show_alert=True)
        return
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("👤 Я один",        callback_data=f'pprsolo_{ppr_id}_{ymd}')],
        [InlineKeyboardButton("👥 С напарником",  callback_data=f'pprwith_{ppr_id}_{ymd}')],
    ]
    await query.edit_message_text(
        f"🔧 <b>{ppr['equipment']}</b>\n📋 {ppr['task']}\n\n"
        f"<b>Как выполняешь?</b>",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def _ppr_finalize(update, context, ppr_id, ymd, partner_id=None, partner_name=None):
    """Финальное подтверждение ППР (соло или с напарником). Блокирует ППР для остальных."""
    query = update.callback_query
    ppr_key = f"{ppr_id}_{ymd}"
    taken = _ppr_taken_by_other(ppr_key, update.effective_user.id)
    if taken:
        await query.answer(f"❌ Уже взял: {taken}", show_alert=True)
        return
    ppr = next((p for p in load_ppr() if str(p['id']) == str(ppr_id)), None)
    if not ppr:
        await query.answer("❌ ППР не найден", show_alert=True)
        return
    await query.answer()

    mechanic_name = update.effective_user.full_name or update.effective_user.first_name
    confirm_time  = datetime.now().strftime('%d.%m.%Y %H:%M')
    date_str      = datetime.strptime(ymd, '%Y%m%d').strftime('%d.%m.%Y')
    who = f"{mechanic_name} + {partner_name}" if partner_name else mechanic_name

    PPR_STATUS_TODAY[ppr_key] = {
        'status': 'confirmed', 'mechanic': who,
        'mechanic_id': update.effective_user.id,
        'partner_id': partner_id, 'time': confirm_time,
    }
    save_daily_state()
    log_ppr_to_sheets(ppr, date_str, 'Подтверждено', who, confirm_time)

    text = (
        f"✅ <b>ППР подтверждено!</b>\n\n"
        f"🏭 {WORKSHOPS.get(ppr['workshop'], ppr['workshop'])}\n"
        f"🔧 {ppr['equipment']}\n"
        f"📋 {ppr['task']}\n"
        f"👤 Выполняет: {who}\n"
        f"🕐 Начато: {confirm_time}"
    )
    try:
        await query.edit_message_text(text, parse_mode='HTML')
    except Exception:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, parse_mode='HTML')

    # Уведомить напарника
    if partner_id:
        try:
            await context.bot.send_message(
                chat_id=partner_id,
                text=(f"👥 <b>{mechanic_name}</b> записал вас напарником по ППР:\n"
                      f"🔧 {ppr['equipment']}\n📋 {ppr['task']}\n🕐 {confirm_time}"),
                parse_mode='HTML')
        except Exception as e:
            logger.error(f"Не удалось уведомить напарника {partner_id}: {e}")

    # Уведомить бригадиров: слесарь начал ППР → можно контролировать/подтвердить
    brig_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Подтвердить итог", callback_data=f"pprv_{ppr_id}_{ymd}")],
    ])
    for uid in get_all_brigadiers():
        if not brig_sees_workshop(uid, ppr.get('workshop')):
            continue  # не его цех
        try:
            await context.bot.send_message(
                chat_id=uid, parse_mode='HTML', reply_markup=brig_kb,
                text=(f"🔧 <b>{who}</b> начал ППР:\n"
                      f"🏭 {WORKSHOPS.get(ppr['workshop'], ppr['workshop'])}\n"
                      f"🔧 {ppr['equipment']}\n📋 {ppr['task']}\n🕐 {confirm_time}\n\n"
                      f"Проконтролируй и подтверди итог 👇"))
        except Exception as e:
            logger.error(f"Бригадиру о старте ППР {uid}: {e}")

    await mechanic_quick_menu(update, context)

async def ppr_confirm_solo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.callback_query.data.split('_', 2)   # pprsolo_{id}_{ymd}
    await _ppr_finalize(update, context, parts[1], parts[2])

async def ppr_confirm_with(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 2: выбор напарника из списка слесарей."""
    query = update.callback_query
    parts = query.data.split('_', 2)                   # pprwith_{id}_{ymd}
    ppr_id, ymd = parts[1], parts[2]
    taken = _ppr_taken_by_other(f"{ppr_id}_{ymd}", update.effective_user.id)
    if taken:
        await query.answer(f"❌ Уже взял: {taken}", show_alert=True)
        return
    await query.answer()
    me = update.effective_user.id
    buttons = [
        [InlineKeyboardButton(f"🔧 {nm}", callback_data=f'pprpart_{ppr_id}_{ymd}_{mid}')]
        for mid, nm in sorted(_mechanic_roster().items(), key=lambda kv: kv[1].lower())
        if mid != me
    ]
    if not buttons:
        # некого выбрать — оформляем как соло
        await _ppr_finalize(update, context, ppr_id, ymd)
        return
    buttons.append([InlineKeyboardButton("👤 Всё-таки один", callback_data=f'pprsolo_{ppr_id}_{ymd}')])
    await query.edit_message_text("👥 <b>Кто напарник?</b>",
        reply_markup=InlineKeyboardMarkup(buttons), parse_mode='HTML')

async def ppr_confirm_partner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.callback_query.data.split('_')      # pprpart_{id}_{ymd}_{partnerId}
    ppr_id, ymd, partner_id = parts[1], parts[2], int(parts[3])
    partner_name = _mechanic_roster().get(partner_id, f'Слесарь {partner_id}')
    await _ppr_finalize(update, context, ppr_id, ymd, partner_id, partner_name)

async def ppr_postpone_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Механик хочет отложить ППР — просим написать причину."""
    query = update.callback_query
    await query.answer()
    try:
        parts = query.data.split('_', 2)
        ppr_id = parts[1]
        ymd    = parts[2]
        ppr_list = load_ppr()
        ppr = next((p for p in ppr_list if str(p['id']) == str(ppr_id)), None)
        context.user_data['ppr_postpone_id']  = ppr_id
        context.user_data['ppr_postpone_ymd'] = ymd
        context.user_data['waiting_for_ppr_postpone'] = True
        context.user_data['last_menu_msg_id'] = query.message.message_id
        ppr_info = f"🔧 {ppr['equipment']} — {ppr['task']}" if ppr else ''
        text = (
            f"⏸ <b>Откладываем ППР</b>\n{ppr_info}\n\n"
            f"Напиши причину и когда выполнишь:\n"
            f"Например: <i>«В отъезде, выполню сегодня в 15:00»</i>\n\n"
            f"👇 Напиши в чат:"
        )
        try:
            await query.edit_message_text(text, parse_mode='HTML')
        except Exception:
            # edit не удалось — отправляем новым сообщением
            msg = await context.bot.send_message(
                chat_id=update.effective_chat.id, text=text, parse_mode='HTML'
            )
            context.user_data['last_menu_msg_id'] = msg.message_id
    except Exception as e:
        logger.error(f"Ошибка ppr_postpone_ask: {e}", exc_info=True)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ Ошибка. Попробуй ещё раз или напиши /start"
        )

async def ppr_postpone_reason_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получена причина откладывания ППР."""
    context.user_data['waiting_for_ppr_postpone'] = False
    reason = update.message.text
    ppr_id = context.user_data.pop('ppr_postpone_id', None)
    ymd    = context.user_data.pop('ppr_postpone_ymd', None)
    if not ppr_id or not ymd:
        return
    ppr_key  = f"{ppr_id}_{ymd}"
    date_str = datetime.strptime(ymd, '%Y%m%d').strftime('%d.%m.%Y')
    mechanic_name  = update.effective_user.full_name or update.effective_user.first_name
    postpone_time  = datetime.now().strftime('%d.%m.%Y %H:%M')
    ppr_list = load_ppr()
    ppr = next((p for p in ppr_list if str(p['id']) == str(ppr_id)), None)
    PPR_STATUS_TODAY[ppr_key] = {
        'status': 'postponed', 'mechanic': mechanic_name,
        'mechanic_id': update.effective_user.id,
        'time': postpone_time, 'note': reason,
    }
    save_daily_state()
    if ppr:
        log_ppr_to_sheets(ppr, date_str, 'Отложено', mechanic_name, postpone_time, reason)
    keyboard = [[InlineKeyboardButton("🔙 Меню", callback_data='role_mechanic')]]
    await edit_or_send(update, context,
        f"⏸ <b>ППР отложен</b>\n\n"
        f"📋 Причина: {reason}\n"
        f"🔧 Слесарь: {mechanic_name}\n"
        f"🕐 {postpone_time}",
        keyboard
    )
    # Уведомить всех админов
    ppr_info = f"🔧 {ppr['equipment']} — {ppr['task']}" if ppr else f"ID {ppr_id}"
    await notify_admins(
        context.bot,
        f"⏸ <b>ППР отложен слесарем {mechanic_name}</b>\n\n"
        f"{ppr_info}\n"
        f"📋 Причина: {reason}"
    )
    # Уведомить бригадиров (только по их цехам) — с причиной + кнопкой подтвердить итог
    if ppr:
        brig_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 Подтвердить итог", callback_data=f"pprv_{ppr_id}_{ymd}")],
        ])
        brig_text = (
            f"⏸ <b>{mechanic_name}</b> отложил ППР:\n"
            f"🏭 {WORKSHOPS.get(ppr.get('workshop',''), ppr.get('workshop',''))}\n"
            f"🔧 {ppr.get('equipment','')}\n📋 {ppr.get('task','')}\n"
            f"🕐 {postpone_time}\n\n"
            f"📋 Причина: {reason}"
        )
        for uid in get_all_brigadiers():
            if not brig_sees_workshop(uid, ppr.get('workshop')):
                continue
            try:
                await context.bot.send_message(chat_id=uid, text=brig_text,
                                               reply_markup=brig_kb, parse_mode='HTML')
            except Exception as e:
                logger.error(f"Бригадиру об откладывании ППР {uid}: {e}")

    await mechanic_quick_menu(update, context)

# ============================================================================
# ИНТЕРФЕЙС АДМИНА
# ============================================================================

async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await query.answer("❌ Нет доступа!", show_alert=True)
        return
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("📊 Статистика",       callback_data='admin_stats')],
        [InlineKeyboardButton("📋 Все заявки",       callback_data='admin_all_requests')],
        [InlineKeyboardButton("🏆 Аналитика",        callback_data='admin_analytics')],
        [InlineKeyboardButton("🏅 Слесари / премии", callback_data='admin_mech_list')],
        [InlineKeyboardButton("📥 Экспорт в Excel",  callback_data='admin_export')],
        [InlineKeyboardButton("🗄 Архив месяца",      callback_data='admin_archive')],
        [InlineKeyboardButton("👥 Пользователи",     callback_data='admin_users_list')],
        [InlineKeyboardButton("📢 Рассылка",         callback_data='admin_broadcast')],
        [InlineKeyboardButton("🗓 График ППР",       callback_data='admin_ppr')],
        [InlineKeyboardButton("📝 Прочие задачи",    callback_data='admin_other_tasks')],
        [InlineKeyboardButton("🏭 Управление имуществом", callback_data='admin_inventory')],
        [InlineKeyboardButton("🗑 Очистить историю",     callback_data='admin_clear_menu')],
        [InlineKeyboardButton("🔙 Главное меню",         callback_data='back_to_main')]
    ]
    await query.edit_message_text(
        "<b>👨‍💼 АДМИН ПАНЕЛЬ</b>\n\nУправление заявками и отчетами:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    total       = len(REQUESTS)
    new_c       = len([r for r in REQUESTS.values() if r['status'] == 'new'])
    in_prog_c   = len([r for r in REQUESTS.values() if r['status'] == 'in_progress'])
    postponed_c = len([r for r in REQUESTS.values() if r['status'] == 'postponed'])
    done_c      = len([r for r in REQUESTS.values() if r['status'] == 'done'])

    workshop_counts = {}
    for r in REQUESTS.values():
        w = WORKSHOPS.get(r['workshop'], r['workshop'])
        workshop_counts[w] = workshop_counts.get(w, 0) + 1

    urgency_counts = {}
    for r in REQUESTS.values():
        u = URGENCY_LEVELS.get(r['urgency'], r['urgency'])
        urgency_counts[u] = urgency_counts.get(u, 0) + 1

    lines = [
        "<b>📊 СТАТИСТИКА ЗАЯВОК</b>\n",
        f"📌 Всего заявок: <b>{total}</b>",
        f"🆕 Новые: <b>{new_c}</b>",
        f"⏳ В работе: <b>{in_prog_c}</b>",
        f"⏸ Отложенные: <b>{postponed_c}</b>",
        f"✅ Выполнено: <b>{done_c}</b>",
    ]

    if workshop_counts:
        lines.append("\n<b>По цехам:</b>")
        for w, cnt in workshop_counts.items():
            lines.append(f"  {w}: {cnt}")

    if urgency_counts:
        lines.append("\n<b>По срочности:</b>")
        for u, cnt in urgency_counts.items():
            lines.append(f"  {u}: {cnt}")

    if total == 0:
        lines.append("\n<i>Заявок пока нет</i>")

    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data='role_admin')]]
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def admin_analytics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    done_reqs = [r for r in REQUESTS.values() if r['status'] == 'done']
    lines = ["<b>🏆 АНАЛИТИКА</b>\n"]

    # Среднее время выполнения по механикам
    mechanic_times: dict = {}
    for r in done_reqs:
        if r.get('mechanic_name') and r.get('done_time') and r.get('timestamp'):
            try:
                t_start = datetime.strptime(r['timestamp'], '%d.%m.%Y %H:%M')
                t_done  = datetime.strptime(r['done_time'],  '%d.%m.%Y %H:%M')
                minutes = (t_done - t_start).total_seconds() / 60
                mname   = r['mechanic_name']
                mechanic_times.setdefault(mname, []).append(minutes)
            except Exception:
                pass

    if mechanic_times:
        lines.append("<b>⏱ Среднее время выполнения:</b>")
        for mname, times in sorted(mechanic_times.items()):
            avg  = sum(times) / len(times)
            h, m = divmod(int(avg), 60)
            lines.append(f"  🔧 {mname}: {h}ч {m}мин (из {len(times)} заявок)")
    else:
        lines.append("<i>Недостаточно данных для анализа времени</i>")

    # Топ ломающегося оборудования
    equipment_counts: dict = {}
    for r in REQUESTS.values():
        eq = r.get('problem', '').strip()
        if eq:
            equipment_counts[eq] = equipment_counts.get(eq, 0) + 1

    if equipment_counts:
        lines.append("\n<b>🔩 Топ ломающегося оборудования:</b>")
        top = sorted(equipment_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        for i, (eq, cnt) in enumerate(top, 1):
            lines.append(f"  {i}. {eq} — {cnt} раз")

    # Оценки качества ремонта
    rating_counts = {'good': 0, 'ok': 0, 'bad': 0}
    for r in REQUESTS.values():
        rv = r.get('rating')
        if rv in rating_counts:
            rating_counts[rv] += 1
    total_rated = sum(rating_counts.values())
    if total_rated:
        lines.append("\n<b>⭐ Оценки качества ремонта:</b>")
        lines.append(f"  👍 Хорошо: {rating_counts['good']}")
        lines.append(f"  😐 Нормально: {rating_counts['ok']}")
        lines.append(f"  👎 Плохо: {rating_counts['bad']}")
        score = (rating_counts['good'] * 100 + rating_counts['ok'] * 50) / total_rated
        lines.append(f"  📊 Индекс качества: <b>{score:.0f}%</b> (оценено {total_rated})")

    keyboard = [
        [InlineKeyboardButton("📈 Графики",          callback_data='admin_charts')],
        [InlineKeyboardButton("🏆 Рейтинг слесарей", callback_data='lb_month')],
        [InlineKeyboardButton("🔙 Назад",            callback_data='role_admin')],
    ]
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


# ============================================================================
# ЭКСПОРТ В EXCEL/CSV  (#4)
# ============================================================================

EXPORT_COLUMNS = [
    "ID", "Цех", "Отделение", "Проблема/оборудование", "Срочность",
    "Оператор", "Время подачи", "Слесарь", "Принята", "Время выполнения",
    "Статус", "ETA", "Комментарий слесаря", "Оценка",
]

def _section_name(req: dict) -> str:
    return WORKSHOP_SECTIONS.get(req.get('workshop', ''), {}).get(req.get('section', ''), '')

def _export_row(r: dict) -> list:
    return [
        r.get('id', ''),
        WORKSHOPS.get(r.get('workshop', ''), r.get('workshop', '')),
        _section_name(r),
        r.get('problem', ''),
        URGENCY_LEVELS.get(r.get('urgency', ''), r.get('urgency', '')),
        r.get('user_name', ''),
        r.get('timestamp', ''),
        r.get('mechanic_name') or '',
        r.get('accept_time', ''),
        r.get('done_time', ''),
        STATUS_LABELS.get(r.get('status', ''), r.get('status', '')),
        r.get('eta', ''),
        r.get('done_comment') or '',
        RATING_LABELS.get(r.get('rating'), ''),
    ]

def build_export_file(reqs: list, period_label: str) -> tuple:
    """Возвращает (BytesIO, filename). .xlsx если есть openpyxl, иначе .csv."""
    rows = [_export_row(r) for r in reqs]
    date_tag = datetime.now().strftime('%Y%m%d_%H%M')
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter

        wb = Workbook()
        ws = wb.active
        ws.title = "Заявки"
        ws.append(EXPORT_COLUMNS)
        header_fill = PatternFill("solid", fgColor="4472C4")
        for col_idx, _ in enumerate(EXPORT_COLUMNS, 1):
            c = ws.cell(row=1, column=col_idx)
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = header_fill
        for row in rows:
            ws.append(row)
        # Авто-ширина колонок (по самому длинному значению, с потолком)
        for col_idx, header in enumerate(EXPORT_COLUMNS, 1):
            longest = max([len(str(header))] + [len(str(row[col_idx - 1])) for row in rows] or [0])
            ws.column_dimensions[get_column_letter(col_idx)].width = min(longest + 2, 45)
        ws.freeze_panes = "A2"
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return buf, f"zayavki_{date_tag}.xlsx"
    except ImportError:
        # Фолбэк без зависимостей: CSV с BOM (Excel корректно откроет кириллицу)
        text_buf = io.StringIO()
        writer = csv.writer(text_buf, delimiter=';')
        writer.writerow(EXPORT_COLUMNS)
        writer.writerows(rows)
        buf = io.BytesIO(('﻿' + text_buf.getvalue()).encode('utf-8'))
        buf.seek(0)
        return buf, f"zayavki_{date_tag}.csv"

async def admin_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор периода для экспорта заявок в файл."""
    query = update.callback_query
    await query.answer()
    keyboard = [
        [
            InlineKeyboardButton("📅 Сегодня", callback_data='exp_req_1'),
            InlineKeyboardButton("📅 3 дня",   callback_data='exp_req_3'),
        ],
        [
            InlineKeyboardButton("📅 Неделя",  callback_data='exp_req_7'),
            InlineKeyboardButton("📅 Месяц",   callback_data='exp_req_30'),
        ],
        [InlineKeyboardButton("📋 Все за всё время", callback_data='exp_req_0')],
        [InlineKeyboardButton("🔙 Назад",            callback_data='role_admin')],
    ]
    await query.edit_message_text(
        f"📥 <b>Экспорт заявок в файл</b>\n\nВсего в системе: <b>{len(REQUESTS)}</b>\n\nЗа какой период выгрузить?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def admin_export_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сформировать и отправить файл с заявками за период."""
    query = update.callback_query
    await query.answer("Готовлю файл…")
    days = int(query.data.split('exp_req_')[1])

    def _req_num(r):
        try: return int(r['id'].split('-')[1])
        except: return 0

    all_sorted = sorted(REQUESTS.values(), key=_req_num, reverse=True)
    if days > 0:
        filtered = _filter_requests_by_days(all_sorted, days)
        period_labels = {1: 'сегодня', 3: 'за 3 дня', 7: 'за неделю', 30: 'за месяц'}
        period_label  = period_labels.get(days, f'за {days} дн.')
    else:
        filtered = all_sorted
        period_label = 'всё время'

    if not filtered:
        keyboard = [[InlineKeyboardButton("🔙 К выбору периода", callback_data='admin_export')]]
        await query.edit_message_text(
            f"📥 <b>Экспорт — {period_label}</b>\n\nЗа этот период заявок нет.",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
        )
        return

    buf, filename = build_export_file(filtered, period_label)
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=buf,
        filename=filename,
        caption=f"📥 Заявки — {period_label} ({len(filtered)} шт.)"
    )
    # Свежее меню админа внизу, чтобы не прокручивать вверх после получения файла
    text, markup = build_role_menu(update.effective_user.id, 'admin')
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"✅ Файл готов: <b>{filename}</b> ({len(filtered)} шт.)\n\n{text}",
        reply_markup=markup,
        parse_mode='HTML'
    )


# ============================================================================
# ГРАФИКИ АНАЛИТИКИ  (#8)
# ============================================================================

def build_analytics_charts() -> io.BytesIO:
    """Рисует сводку графиков в один PNG. Требует matplotlib."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    reqs = list(REQUESTS.values())

    # 1) Заявки по статусам (чистые подписи без эмодзи для matplotlib)
    status_clean = {'new': 'Новые', 'in_progress': 'В работе', 'postponed': 'Отложены',
                    'done': 'Выполнены', 'cancelled': 'Отменены'}
    status_counts = {}
    for r in reqs:
        lbl = status_clean.get(r['status'], r['status'])
        status_counts[lbl] = status_counts.get(lbl, 0) + 1

    # 2) Топ-10 ломающегося оборудования
    eq_counts = {}
    for r in reqs:
        eq = (r.get('problem') or '').strip()
        if eq:
            eq_counts[eq] = eq_counts.get(eq, 0) + 1
    eq_top = sorted(eq_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    # 3) Загрузка слесарей (выполненные заявки)
    mech_counts = {}
    for r in reqs:
        if r['status'] == 'done' and r.get('mechanic_name'):
            mech_counts[r['mechanic_name']] = mech_counts.get(r['mechanic_name'], 0) + 1
    mech_top = sorted(mech_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    # 4) Оценки качества (без эмодзи — шрифт matplotlib их не рисует)
    rating_counts = {'Хорошо': 0, 'Нормально': 0, 'Плохо': 0}
    rmap = {'good': 'Хорошо', 'ok': 'Нормально', 'bad': 'Плохо'}
    for r in reqs:
        key = rmap.get(r.get('rating'))
        if key:
            rating_counts[key] += 1

    fig, axes = plt.subplots(2, 2, figsize=(12, 9))
    fig.suptitle('Аналитика заявок', fontsize=16, fontweight='bold')

    ax = axes[0][0]
    if status_counts:
        ax.bar(list(status_counts.keys()), list(status_counts.values()), color='#4472C4')
        ax.set_title('Заявки по статусам')
        ax.tick_params(axis='x', rotation=30, labelsize=8)
    else:
        ax.text(0.5, 0.5, 'Нет данных', ha='center'); ax.set_axis_off()

    ax = axes[0][1]
    if eq_top:
        names = [e[:25] for e, _ in eq_top][::-1]
        vals  = [c for _, c in eq_top][::-1]
        ax.barh(names, vals, color='#E15759')
        ax.set_title('Топ ломающегося оборудования')
        ax.tick_params(axis='y', labelsize=8)
    else:
        ax.text(0.5, 0.5, 'Нет данных', ha='center'); ax.set_axis_off()

    ax = axes[1][0]
    if mech_top:
        ax.bar([m[:15] for m, _ in mech_top], [c for _, c in mech_top], color='#59A14F')
        ax.set_title('Выполнено заявок по слесарям')
        ax.tick_params(axis='x', rotation=30, labelsize=8)
    else:
        ax.text(0.5, 0.5, 'Нет данных', ha='center'); ax.set_axis_off()

    ax = axes[1][1]
    if sum(rating_counts.values()) > 0:
        ax.pie(list(rating_counts.values()), labels=list(rating_counts.keys()),
               autopct='%1.0f%%', colors=['#59A14F', '#EDC948', '#E15759'])
        ax.set_title('Оценки качества ремонта')
    else:
        ax.text(0.5, 0.5, 'Оценок пока нет', ha='center'); ax.set_axis_off()

    fig.tight_layout(rect=[0, 0, 1, 0.96])
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=110)
    plt.close(fig)
    buf.seek(0)
    return buf

async def admin_charts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Рисую графики…")
    if not REQUESTS:
        await query.answer("Нет данных для графиков", show_alert=True)
        return
    try:
        buf = build_analytics_charts()
    except ImportError:
        await query.answer("⚠️ Библиотека matplotlib не установлена", show_alert=True)
        return
    except Exception as e:
        logger.error(f"Ошибка построения графиков: {e}", exc_info=True)
        await query.answer("❌ Не удалось построить графики", show_alert=True)
        return
    await context.bot.send_photo(
        chat_id=update.effective_chat.id,
        photo=buf,
        filename='analytics.png',
        caption='📈 Графики аналитики заявок'
    )
    # Свежее меню админа внизу, чтобы не прокручивать вверх после графиков
    text, markup = build_role_menu(update.effective_user.id, 'admin')
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text, reply_markup=markup, parse_mode='HTML'
    )


# ============================================================================
# РЕЙТИНГ СЛЕСАРЕЙ  (#15)
# ============================================================================

MONTHS_RU = {1:'январь', 2:'февраль', 3:'март', 4:'апрель', 5:'май', 6:'июнь',
             7:'июль', 8:'август', 9:'сентябрь', 10:'октябрь', 11:'ноябрь', 12:'декабрь'}

def build_leaderboard(period: str = 'month') -> str:
    """Текст рейтинга слесарей по выполненным заявкам и качеству. period: month|all."""
    done = [r for r in REQUESTS.values() if r['status'] == 'done' and r.get('mechanic_name')]

    if period == 'month':
        now = datetime.now()
        filtered = []
        for r in done:
            try:
                d = datetime.strptime(r.get('done_time', ''), '%d.%m.%Y %H:%M')
            except (ValueError, TypeError):
                continue
            if d.year == now.year and d.month == now.month:
                filtered.append(r)
        done = filtered
        period_label = f"{MONTHS_RU[now.month]} {now.year}"
    else:
        period_label = "всё время"

    if not done:
        return f"<b>🏆 РЕЙТИНГ СЛЕСАРЕЙ — {period_label}</b>\n\n<i>Нет выполненных заявок за этот период.</i>"

    # Сбор статистики по слесарям
    stats = {}
    for r in done:
        m = r['mechanic_name']
        s = stats.setdefault(m, {'count': 0, 'good': 0, 'ok': 0, 'bad': 0, 'times': []})
        s['count'] += 1
        rv = r.get('rating')
        if rv in ('good', 'ok', 'bad'):
            s[rv] += 1
        try:
            t0 = datetime.strptime(r['timestamp'], '%d.%m.%Y %H:%M')
            t1 = datetime.strptime(r['done_time'], '%d.%m.%Y %H:%M')
            s['times'].append((t1 - t0).total_seconds() / 60)
        except (ValueError, TypeError, KeyError):
            pass

    # Сортировка: сначала по числу заявок, затем по индексу качества
    def _quality(s):
        rated = s['good'] + s['ok'] + s['bad']
        return (s['good'] * 100 + s['ok'] * 50) / rated if rated else -1

    ranked = sorted(stats.items(), key=lambda kv: (kv[1]['count'], _quality(kv[1])), reverse=True)

    medals = {0: '🥇', 1: '🥈', 2: '🥉'}
    lines = [f"<b>🏆 РЕЙТИНГ СЛЕСАРЕЙ — {period_label}</b>\n"]
    for i, (name, s) in enumerate(ranked):
        place = medals.get(i, f"{i + 1}.")
        line = f"{place} <b>{name}</b> — {s['count']} заявок"
        # Среднее время
        if s['times']:
            avg = sum(s['times']) / len(s['times'])
            h, m = divmod(int(avg), 60)
            line += f"\n     ⏱ ср. время: {h}ч {m}мин"
        # Качество
        rated = s['good'] + s['ok'] + s['bad']
        if rated:
            q = _quality(s)
            line += f"\n     ⭐ качество: {q:.0f}% (👍{s['good']} 😐{s['ok']} 👎{s['bad']})"
        lines.append(line)

    return "\n".join(lines)

async def admin_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    period = 'all' if query.data == 'lb_all' else 'month'
    text = build_leaderboard(period)

    if period == 'month':
        toggle = InlineKeyboardButton("📅 За всё время", callback_data='lb_all')
    else:
        toggle = InlineKeyboardButton("📅 За текущий месяц", callback_data='lb_month')
    keyboard = [
        [toggle],
        [InlineKeyboardButton("🔙 К аналитике", callback_data='admin_analytics')],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')


# ============================================================================
# СТАТИСТИКА ПО КАЖДОМУ СЛЕСАРЮ (для премий)
# ============================================================================

def _is_this_month(dt_str: str) -> bool:
    try:
        d = datetime.strptime(dt_str, '%d.%m.%Y %H:%M')
    except (ValueError, TypeError):
        return False
    now = datetime.now()
    return d.year == now.year and d.month == now.month

def _mechanic_roster() -> dict:
    """Все слесари: id -> имя. Реальное имя из users.json в приоритете; если там
    осталась заглушка «Пользователь <id>» — берём настоящее имя из заявок."""
    # Последнее известное имя из заявок (реальное имя из Telegram)
    req_names = {}
    for r in REQUESTS.values():
        mid = r.get('mechanic_id')
        if mid and r.get('mechanic_name'):
            req_names[int(mid)] = r['mechanic_name']

    roster = {}
    for uid, info in load_users().items():
        if info.get('role') != 'mechanic':
            continue
        i = int(uid)
        name = (info.get('name') or '').strip()
        if not name or name == f'Пользователь {uid}':
            name = req_names.get(i, name or f'Слесарь {uid}')
        roster[i] = name

    # Слесари, которых нет в users.json, но есть в заявках
    for i, nm in req_names.items():
        roster.setdefault(i, nm)
    return roster

def _mechanic_stats(mech_id: int, name: str, period: str = 'month') -> dict:
    """Считает статистику слесаря за период. Единый источник для карточки и ведомости."""
    mine = [r for r in REQUESTS.values() if r.get('mechanic_id') == mech_id]
    done = [r for r in mine if r['status'] == 'done']
    if period == 'month':
        done = [r for r in done if _is_this_month(r.get('done_time', ''))]

    good = ok = bad = 0
    times = []
    urg = {}
    for r in done:
        rv = r.get('rating')
        if rv == 'good': good += 1
        elif rv == 'ok': ok += 1
        elif rv == 'bad': bad += 1
        try:
            t0 = datetime.strptime(r['timestamp'], '%d.%m.%Y %H:%M')
            t1 = datetime.strptime(r['done_time'], '%d.%m.%Y %H:%M')
            times.append((t1 - t0).total_seconds() / 60)
        except (ValueError, TypeError, KeyError):
            pass
        u = URGENCY_EMOJI.get(r.get('urgency'), '')
        if u:
            urg[u] = urg.get(u, 0) + 1

    other = [t for t in OTHER_TASKS.values() if name and name in (t.get('worker') or '')]
    if period == 'month':
        other = [t for t in other if _is_this_month(t.get('created_at', ''))]

    rated = good + ok + bad
    return {
        'count': len(done), 'good': good, 'ok': ok, 'bad': bad, 'rated': rated,
        'quality': (good * 100 + ok * 50) / rated if rated else None,
        'avg_time': sum(times) / len(times) if times else None,
        'fastest': min(times) if times else None,
        'urg': urg, 'other': len(other),
        'in_prog': len([r for r in mine if r['status'] == 'in_progress']),
        'postponed': len([r for r in mine if r['status'] == 'postponed']),
        'score': len(done) * 10 + good * 5 + ok * 2 - bad * 3 + len(other) * 5,
    }

def build_mechanic_card(mech_id: int, name: str, period: str = 'month') -> str:
    """Подробная карточка слесаря: что сделал, качество, скорость, очки для премии."""
    s = _mechanic_stats(mech_id, name, period)
    period_label = f"{MONTHS_RU[datetime.now().month]} {datetime.now().year}" if period == 'month' else "всё время"

    lines = [
        f"<b>🔧 {name}</b>",
        f"<i>Период: {period_label}</i>\n",
        f"✅ Выполнено заявок: <b>{s['count']}</b>",
    ]
    if s['urg']:
        lines.append("   по срочности: " + "  ".join(f"{e}{c}" for e, c in s['urg'].items()))
    if s['avg_time'] is not None:
        h, m = divmod(int(s['avg_time']), 60)
        fh, fm = divmod(int(s['fastest']), 60)
        lines.append(f"⏱ Ср. время ремонта: <b>{h}ч {m}мин</b> (быстрейший {fh}ч {fm}мин)")
    if s['rated']:
        lines.append(f"⭐ Качество: <b>{s['quality']:.0f}%</b>  (👍{s['good']} 😐{s['ok']} 👎{s['bad']})")
    else:
        lines.append("⭐ Качество: <i>нет оценок</i>")
    lines.append(f"📝 Прочие работы: <b>{s['other']}</b>")
    lines.append(f"⏳ Сейчас в работе: {s['in_prog']}  |  ⏸ отложено: {s['postponed']}")
    lines.append(f"\n🏅 <b>Очки за период: {s['score']}</b>")
    lines.append("<i>(заявка +10, 👍+5, 😐+2, 👎−3, прочая работа +5)</i>")
    return "\n".join(lines)

def _make_table_file(headers: list, rows: list, basename: str) -> tuple:
    """Универсальный экспорт таблицы: .xlsx если есть openpyxl, иначе .csv (BOM)."""
    date_tag = datetime.now().strftime('%Y%m%d_%H%M')
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter
        wb = Workbook(); ws = wb.active; ws.title = "Отчёт"
        ws.append(headers)
        fill = PatternFill("solid", fgColor="4472C4")
        for ci, _ in enumerate(headers, 1):
            c = ws.cell(row=1, column=ci); c.font = Font(bold=True, color="FFFFFF"); c.fill = fill
        for row in rows:
            ws.append(row)
        for ci, h in enumerate(headers, 1):
            longest = max([len(str(h))] + [len(str(row[ci - 1])) for row in rows] or [0])
            ws.column_dimensions[get_column_letter(ci)].width = min(longest + 2, 40)
        ws.freeze_panes = "A2"
        buf = io.BytesIO(); wb.save(buf); buf.seek(0)
        return buf, f"{basename}_{date_tag}.xlsx"
    except ImportError:
        sb = io.StringIO(); w = csv.writer(sb, delimiter=';')
        w.writerow(headers); w.writerows(rows)
        buf = io.BytesIO(('﻿' + sb.getvalue()).encode('utf-8')); buf.seek(0)
        return buf, f"{basename}_{date_tag}.csv"

def build_premium_sheet(period: str = 'month') -> tuple:
    """Ведомость премий по всем слесарям одним файлом. Возвращает (BytesIO, filename)."""
    roster = _mechanic_roster()
    rows = []
    for mid, name in roster.items():
        s = _mechanic_stats(mid, name, period)
        rows.append([
            name, s['count'], s['good'], s['ok'], s['bad'],
            round(s['quality']) if s['quality'] is not None else '',
            round(s['avg_time']) if s['avg_time'] is not None else '',
            s['other'], s['score'],
        ])
    rows.sort(key=lambda r: r[-1], reverse=True)  # по очкам
    headers = ["Слесарь", "Выполнено", "Хорошо", "Норм", "Плохо",
               "Качество %", "Ср.время, мин", "Прочие", "Очки"]
    tag = "month" if period == 'month' else "all"
    return _make_table_file(headers, rows, f"vedomost_premiy_{tag}")

async def admin_mech_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список слесарей для просмотра персональной статистики."""
    query = update.callback_query
    await query.answer()
    roster = _mechanic_roster()
    if not roster:
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data='role_admin')]]
        await query.edit_message_text(
            "<b>🏅 Статистика по слесарям</b>\n\n<i>Слесари ещё не зарегистрированы.</i>",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        return

    keyboard = [
        [InlineKeyboardButton(f"🔧 {nm}", callback_data=f'mech_card_{mid}_month')]
        for mid, nm in sorted(roster.items(), key=lambda kv: kv[1].lower())
    ]
    keyboard.append([
        InlineKeyboardButton("📥 Ведомость: месяц", callback_data='prem_month'),
        InlineKeyboardButton("всё время", callback_data='prem_all'),
    ])
    keyboard.append([InlineKeyboardButton("🏆 Общий рейтинг", callback_data='lb_month')])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='role_admin')])
    await query.edit_message_text(
        "<b>🏅 Статистика по слесарям</b>\n\nВыбери слесаря — покажу что сделал и очки для премии:",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def admin_premium_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выгрузить ведомость премий по всем слесарям в файл."""
    query = update.callback_query
    await query.answer("Готовлю ведомость…")
    period = 'all' if query.data == 'prem_all' else 'month'
    buf, fname = build_premium_sheet(period)
    period_label = 'всё время' if period == 'all' else 'текущий месяц'
    await context.bot.send_document(
        chat_id=update.effective_chat.id, document=buf, filename=fname,
        caption=f"📥 Ведомость премий — {period_label}")
    # Свежее меню админа внизу
    text, markup = build_role_menu(update.effective_user.id, 'admin')
    await context.bot.send_message(
        chat_id=update.effective_chat.id, text=text, reply_markup=markup, parse_mode='HTML')

async def admin_mech_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Карточка конкретного слесаря. callback: mech_card_<id>_<period>."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split('_')          # ['mech','card','<id>','<period>']
    try:
        mech_id = int(parts[2])
    except (IndexError, ValueError):
        await query.answer("❌ Ошибка данных", show_alert=True)
        return
    period = parts[3] if len(parts) > 3 else 'month'

    roster = _mechanic_roster()
    name = roster.get(mech_id, f'Слесарь {mech_id}')
    text = build_mechanic_card(mech_id, name, period)

    if period == 'month':
        toggle = InlineKeyboardButton("📅 За всё время", callback_data=f'mech_card_{mech_id}_all')
    else:
        toggle = InlineKeyboardButton("📅 За текущий месяц", callback_data=f'mech_card_{mech_id}_month')
    keyboard = [
        [toggle],
        [InlineKeyboardButton("🔙 К списку слесарей", callback_data='admin_mech_list')],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def admin_all_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор периода для просмотра всех заявок."""
    query = update.callback_query
    await query.answer()
    total = len(REQUESTS)
    keyboard = [
        [
            InlineKeyboardButton("📅 Сегодня",  callback_data='adm_req_1'),
            InlineKeyboardButton("📅 3 дня",    callback_data='adm_req_3'),
        ],
        [
            InlineKeyboardButton("📅 Неделя",   callback_data='adm_req_7'),
            InlineKeyboardButton("📅 Месяц",    callback_data='adm_req_30'),
        ],
        [InlineKeyboardButton("📋 Все за всё время", callback_data='adm_req_0')],
        [InlineKeyboardButton("🔙 Назад",            callback_data='role_admin')],
    ]
    await query.edit_message_text(
        f"📋 <b>Все заявки</b>\n\nВсего в системе: <b>{total}</b>\n\nЗа какой период показать?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def admin_all_requests_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показ всех заявок за выбранный период."""
    query = update.callback_query
    await query.answer()
    days = int(query.data.split('adm_req_')[1])

    def _req_num(r):
        try: return int(r['id'].split('-')[1])
        except: return 0

    all_sorted = sorted(REQUESTS.values(), key=_req_num, reverse=True)
    if days > 0:
        filtered = _filter_requests_by_days(all_sorted, days)
        period_labels = {1: 'сегодня', 3: 'за 3 дня', 7: 'за неделю', 30: 'за месяц'}
        period_label  = period_labels.get(days, f'за {days} дн.')
    else:
        filtered = all_sorted
        period_label = 'всё время'

    total_all = len(REQUESTS)
    shown = filtered[:50]

    if not filtered:
        text = f"📋 <b>Все заявки — {period_label}</b>\n\nЗа этот период заявок нет."
    else:
        lines = [f"📋 <b>ВСЕ ЗАЯВКИ — {period_label.upper()} ({len(filtered)})</b>"]
        if len(filtered) > 50:
            lines.append(f"<i>Показаны последние 50 из {len(filtered)}</i>\n")
        else:
            lines.append("")
        for r in shown:
            emoji  = URGENCY_EMOJI.get(r['urgency'], '')
            status = STATUS_LABELS.get(r['status'], r['status'])
            entry  = (
                f"<b>{r['id']}</b> | {WORKSHOPS.get(r['workshop'], r['workshop'])} | {status}\n"
                f"   📝 {r['problem']}\n"
                f"   {emoji} | 👷 {r['user_name']} | 📅 {r['timestamp']}\n"
            )
            if r['status'] == 'done':
                entry += f"   ✅ {r.get('done_time', '—')} | 🔧 {r.get('mechanic_name', '—')}\n"
            lines.append(entry)
        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:4000] + "\n\n<i>... (обрезано)</i>"

    keyboard = [[InlineKeyboardButton("🔙 К выбору периода", callback_data='admin_all_requests')]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def admin_users_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    users = load_users()
    # Группируем по ролям для читаемости
    groups = {'admin': [], 'mechanic': [], 'operator': []}
    for uid, info in users.items():
        groups.setdefault(info.get('role', 'other'), []).append((uid, info.get('name', '—')))

    lines = [f"<b>👥 ПОЛЬЗОВАТЕЛИ ({len(users)})</b>"]
    for role in ('admin', 'mechanic', 'operator'):
        members = groups.get(role, [])
        if not members:
            continue
        lines.append(f"\n<b>{ROLE_NAMES.get(role, role)}</b> ({len(members)})")
        for uid, name in sorted(members, key=lambda x: (x[1] or '').lower()):
            lines.append(f"  • {name} — <code>{uid}</code>")
    # прочие роли, если вдруг есть
    for role, members in groups.items():
        if role in ('admin', 'mechanic', 'operator') or not members:
            continue
        lines.append(f"\n<b>{role}</b>")
        for uid, name in members:
            lines.append(f"  • {name} — <code>{uid}</code>")

    lines.append(f"\n<i>Управление:\n/rename [ID] [имя]\n/removeuser [ID]</i>")

    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data='role_admin')]]
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

# ============================================================================
# ГРАФИК ППР (АДМИН)
# ============================================================================

async def admin_ppr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ppr_list = load_ppr()

    # Группируем по дню недели
    by_day = {d: [] for d in range(7)}
    for item in ppr_list:
        by_day[item.get('weekday', 0)].append(item)

    lines = [f"<b>🗓 ГРАФИК ППР ({len(ppr_list)} задач)</b>\n"]
    for d in range(7):
        items = by_day[d]
        if items:
            lines.append(f"<b>{DAY_NAMES[d]}:</b>")
            for item in items:
                wname = WORKSHOPS.get(item.get('workshop',''), item.get('workshop',''))
                lines.append(f"  • {item['time']} — {item['equipment']} ({wname})\n    🔧 {item['task']}")

    pause_note = f"  ⏸ на паузе: {len(PPR_PAUSED_WORKSHOPS)} цех(ов)" if PPR_PAUSED_WORKSHOPS else ""
    keyboard = [
        [InlineKeyboardButton("📋 По дням недели",   callback_data='ppr_view_days')],
        [InlineKeyboardButton("➕ Добавить задачу",  callback_data='ppr_add')],
        [InlineKeyboardButton("📤 Отправить сейчас", callback_data='ppr_send_now')],
        [InlineKeyboardButton(f"⏸ Пауза ППР по цеху{pause_note}", callback_data='ppr_pause')],
        [InlineKeyboardButton("🔙 Назад",            callback_data='role_admin')],
    ]
    text = "\n".join(lines) if len("\n".join(lines)) < 4000 else f"<b>🗓 ГРАФИК ППР</b>\n\nВсего задач: <b>{len(ppr_list)}</b>\nИспользуй «По дням недели» для просмотра."
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def admin_ppr_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Экран паузы ППР по цехам: список цехов со статусом, тап = переключить."""
    query = update.callback_query
    if not is_admin(update.effective_user.id):
        await query.answer("❌ Только админ", show_alert=True)
        return
    await query.answer()
    lines = ["<b>⏸ ПАУЗА ППР ПО ЦЕХУ</b>\n",
             "Нажми на цех, чтобы поставить/снять паузу.",
             "На паузе — уведомления ППР этого цеха не рассылаются.",
             "Задачи ППР не удаляются, всё вернётся при снятии паузы.\n"]
    keyboard = []
    for code, name in WORKSHOPS.items():
        paused = code in PPR_PAUSED_WORKSHOPS
        mark = "⏸ на паузе" if paused else "▶️ активен"
        keyboard.append([InlineKeyboardButton(f"{name} — {mark}", callback_data=f'pprmute_{code}')])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='admin_ppr')])
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def admin_ppr_pause_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключить паузу ППР для цеха + применить сразу."""
    query = update.callback_query
    if not is_admin(update.effective_user.id):
        await query.answer("❌ Только админ", show_alert=True)
        return
    code = query.data.split('_', 1)[1]   # pprmute_{code}
    if code in PPR_PAUSED_WORKSHOPS:
        PPR_PAUSED_WORKSHOPS.discard(code); state = "▶️ ППР снова идёт"
    else:
        PPR_PAUSED_WORKSHOPS.add(code); state = "⏸ ППР на паузе"
    save_ppr_paused()
    # Применить немедленно: переставить сегодняшние задачи ППР (идемпотентно)
    if context.job_queue is not None:
        schedule_ppr_today(context.job_queue)
    await query.answer(f"{WORKSHOPS.get(code, code)}: {state}")
    await admin_ppr_pause(update, context)

async def admin_ppr_view_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать кнопки по дням — нажать чтобы увидеть задачи дня."""
    query = update.callback_query
    await query.answer()
    ppr_list = load_ppr()
    by_day = {d: [] for d in range(7)}
    for item in ppr_list:
        by_day[item.get('weekday', 0)].append(item)

    keyboard = []
    for d in range(7):
        cnt = len(by_day[d])
        keyboard.append([InlineKeyboardButton(f"{DAY_NAMES[d]} ({cnt})", callback_data=f'ppr_day_{d}')])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='admin_ppr')])
    await query.edit_message_text("<b>📋 Выбери день:</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def admin_ppr_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать задачи конкретного дня — каждая задача кнопкой."""
    query = update.callback_query
    await query.answer()
    day = int(query.data.split('ppr_day_')[1])
    ppr_list = load_ppr()
    items = sorted([i for i in ppr_list if i.get('weekday') == day], key=lambda x: x.get('time',''))

    keyboard = []
    for item in items:
        label = f"🕐 {item['time']} — {item['equipment'][:30]}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"ppr_task_{item['id']}")])
    keyboard.append([InlineKeyboardButton("➕ Добавить в этот день", callback_data='ppr_add')])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='ppr_view_days')])

    text = f"<b>{DAY_NAMES[day]} — ППР ({len(items)} задач)</b>\n\nВыбери задачу для просмотра/редактирования:"
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def admin_ppr_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Детали одной ППР-задачи с кнопками редактирования и удаления."""
    query = update.callback_query
    await query.answer()
    entry_id = query.data.split('ppr_task_')[1]
    ppr_list = load_ppr()
    item = next((i for i in ppr_list if str(i.get('id')) == str(entry_id)), None)
    if not item:
        await query.answer("❌ Задача не найдена", show_alert=True)
        return
    d = item.get('weekday', 0)
    wname = WORKSHOPS.get(item.get('workshop',''), item.get('workshop',''))
    text = (
        f"<b>📋 ППР-задача</b>\n\n"
        f"🔧 Оборудование: <b>{item['equipment']}</b>\n"
        f"🏭 Цех: {wname}\n"
        f"📝 Задача: {item['task']}\n"
        f"📅 День: {DAY_NAMES[d]}\n"
        f"🕐 Время: {item['time']}"
    )
    keyboard = [
        [InlineKeyboardButton("✏️ Редактировать", callback_data=f"ppr_edit_{entry_id}")],
        [InlineKeyboardButton("❌ Удалить",        callback_data=f"ppr_del_{entry_id}")],
        [InlineKeyboardButton("🔙 Назад",          callback_data=f"ppr_day_{d}")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

def _ppr_new_workshop_keyboard():
    """Кнопки выбора цеха для новой ППР-задачи."""
    rows = []
    row = []
    for code, name in WORKSHOPS.items():
        short = name.replace('Хлебный цех', 'Хлебный').replace('Булочный цех', 'Булочный')
        row.append(InlineKeyboardButton(short, callback_data=f'ppr_nw_{code}'))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data='admin_ppr')])
    return InlineKeyboardMarkup(rows)

def _ppr_new_day_keyboard():
    """Кнопки дней недели для новой ППР-задачи."""
    days_row1 = [InlineKeyboardButton(DAY_SHORT[d], callback_data=f'ppr_nd_{d}') for d in range(4)]
    days_row2 = [InlineKeyboardButton(DAY_SHORT[d], callback_data=f'ppr_nd_{d}') for d in range(4, 7)]
    return InlineKeyboardMarkup([days_row1, days_row2,
                                 [InlineKeyboardButton("❌ Отмена", callback_data='admin_ppr')]])

def _ppr_new_summary(d: dict) -> str:
    """Текст с текущим состоянием черновика."""
    wname    = WORKSHOPS.get(d.get('workshop', ''), '—')
    day_str  = DAY_NAMES.get(d.get('weekday'), '—') if 'weekday' in d else '—'
    return (
        f"🔧 {d.get('equipment', '—')}\n"
        f"🏭 {wname}\n"
        f"📋 {d.get('task', '—')}\n"
        f"📅 {day_str}\n"
        f"🕐 {d.get('time', '—')}"
    )

async def admin_ppr_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['ppr_new_step'] = 'equipment'
    context.user_data['ppr_new_data'] = {}
    context.user_data['last_menu_msg_id'] = query.message.message_id
    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data='admin_ppr')]]
    await query.edit_message_text(
        "<b>➕ Новая ППР-задача</b>\n\n"
        "<b>Шаг 1/5</b> — Напиши <b>название оборудования</b>:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def admin_ppr_new_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Текстовые шаги создания ППР-задачи: equipment, task, time."""
    step = context.user_data.get('ppr_new_step')
    text = update.message.text.strip()
    d    = context.user_data.setdefault('ppr_new_data', {})

    if step == 'equipment':
        d['equipment'] = text
        context.user_data['ppr_new_step'] = 'workshop'
        await edit_or_send(update, context,
            f"<b>➕ Новая ППР-задача</b>\n\n"
            f"🔧 {text}\n\n"
            "<b>Шаг 2/5</b> — Выбери <b>цех</b>:",
            _ppr_new_workshop_keyboard().inline_keyboard
        )

    elif step == 'task':
        d['task'] = text
        context.user_data['ppr_new_step'] = 'weekday'
        await edit_or_send(update, context,
            f"<b>➕ Новая ППР-задача</b>\n\n"
            f"🔧 {d.get('equipment')}  |  🏭 {WORKSHOPS.get(d.get('workshop',''), '—')}\n"
            f"📋 {text}\n\n"
            "<b>Шаг 4/5</b> — Выбери <b>день недели</b>:",
            _ppr_new_day_keyboard().inline_keyboard
        )

    elif step == 'time':
        if not _valid_hhmm(text):
            # Повторный запрос
            keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data='admin_ppr')]]
            await edit_or_send(update, context,
                "❌ Формат времени: <code>09:00</code> (часы 0–23, минуты 0–59)\n\nПопробуй ещё раз:",
                keyboard
            )
            return
        d['time'] = text
        context.user_data['ppr_new_step'] = 'confirm'
        keyboard = [
            [InlineKeyboardButton("✅ Сохранить",     callback_data='ppr_nsave')],
            [InlineKeyboardButton("🔄 Начать заново", callback_data='ppr_add')],
            [InlineKeyboardButton("❌ Отмена",         callback_data='admin_ppr')],
        ]
        await edit_or_send(update, context,
            f"<b>➕ Новая ППР-задача — подтверди:</b>\n\n"
            f"{_ppr_new_summary(d)}",
            keyboard
        )

async def admin_ppr_new_workshop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 2: цех выбран кнопкой."""
    query = update.callback_query
    await query.answer()
    code = query.data.split('ppr_nw_', 1)[1]
    d    = context.user_data.setdefault('ppr_new_data', {})
    d['workshop'] = code
    context.user_data['ppr_new_step'] = 'task'
    context.user_data['last_menu_msg_id'] = query.message.message_id
    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data='admin_ppr')]]
    await query.edit_message_text(
        f"<b>➕ Новая ППР-задача</b>\n\n"
        f"🔧 {d.get('equipment')}  |  🏭 {WORKSHOPS.get(code, code)}\n\n"
        "<b>Шаг 3/5</b> — Напиши <b>что нужно сделать</b> (вид работы):",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def admin_ppr_new_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 4: день недели выбран кнопкой."""
    query = update.callback_query
    await query.answer()
    day  = int(query.data.split('ppr_nd_', 1)[1])
    d    = context.user_data.setdefault('ppr_new_data', {})
    d['weekday'] = day
    context.user_data['ppr_new_step'] = 'time'
    context.user_data['last_menu_msg_id'] = query.message.message_id
    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data='admin_ppr')]]
    await query.edit_message_text(
        f"<b>➕ Новая ППР-задача</b>\n\n"
        f"🔧 {d.get('equipment')}  |  🏭 {WORKSHOPS.get(d.get('workshop',''), '—')}\n"
        f"📋 {d.get('task')}  |  📅 {DAY_NAMES[day]}\n\n"
        "<b>Шаг 5/5</b> — Напиши <b>время</b> (формат <code>09:00</code>):",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def admin_ppr_new_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Финальный шаг: сохранить ППР-задачу."""
    query = update.callback_query
    await query.answer()
    d = context.user_data.pop('ppr_new_data', {})
    context.user_data.pop('ppr_new_step', None)
    if not all(k in d for k in ('equipment', 'workshop', 'task', 'weekday', 'time')):
        await query.answer("❌ Данные неполные, начни заново.", show_alert=True)
        return
    ppr_list = load_ppr()
    new_id = str(max((int(i.get('id', 0)) for i in ppr_list), default=0) + 1)
    ppr_list.append({
        'id':        new_id,
        'equipment': d['equipment'],
        'workshop':  d['workshop'],
        'task':      d['task'],
        'weekday':   d['weekday'],
        'time':      d['time'],
    })
    save_ppr(ppr_list)
    # Если ППР на сегодня — уведомления встанут сразу, без перезапуска
    _resync_ppr_jobs(context.job_queue)
    keyboard = [
        [InlineKeyboardButton("➕ Добавить ещё", callback_data='ppr_add')],
        [InlineKeyboardButton("🔙 К графику ППР", callback_data='admin_ppr')],
    ]
    await query.edit_message_text(
        f"✅ <b>ППР-задача #{new_id} добавлена!</b>\n\n"
        f"{_ppr_new_summary(d)}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def admin_ppr_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    entry_id = query.data.split('ppr_edit_')[1]
    ppr_list = load_ppr()
    item = next((i for i in ppr_list if i.get('id') == entry_id), None)
    if not item:
        await query.answer("❌ Не найдено", show_alert=True)
        return

    d = item.get('weekday', 0)
    wname = WORKSHOPS.get(item.get('workshop',''), '')
    text = (
        f"<b>✏️ Редактирование задачи</b>\n\n"
        f"🔧 Оборудование: <b>{item['equipment']}</b>\n"
        f"🏭 Цех: <b>{wname}</b>\n"
        f"📋 Задача: <b>{item['task']}</b>\n"
        f"📅 День: <b>{DAY_NAMES[d]}</b>\n"
        f"🕐 Время: <b>{item['time']}</b>\n\n"
        f"Что именно изменить?"
    )
    keyboard = [
        [InlineKeyboardButton("🔧 Оборудование", callback_data=f"ppr_ef_{entry_id}_equip")],
        [InlineKeyboardButton("🏭 Цех",          callback_data=f"ppr_ef_{entry_id}_workshop")],
        [InlineKeyboardButton("📋 Задача",        callback_data=f"ppr_ef_{entry_id}_task")],
        [InlineKeyboardButton("📅 День недели",  callback_data=f"ppr_ef_{entry_id}_weekday")],
        [InlineKeyboardButton("🕐 Время",         callback_data=f"ppr_ef_{entry_id}_time")],
        [InlineKeyboardButton("🔙 Назад",         callback_data=f"ppr_task_{entry_id}")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def admin_ppr_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запросить новое значение конкретного поля ППР."""
    query = update.callback_query
    await query.answer()
    # callback_data: ppr_ef_{id}_{field}
    parts    = query.data.split('_', 3)   # ['ppr', 'ef', id, field]
    entry_id = parts[2]
    field    = parts[3]

    FIELD_LABELS = {
        'equip':    ('🔧 Оборудование', 'Тестомесильная машина №1'),
        'workshop': ('🏭 Цех', 'хлебный / булочный'),
        'task':     ('📋 Задача', 'Замена масла'),
        'weekday':  ('📅 День недели', 'Пн / Вт / Ср / Чт / Пт / Сб / Вс'),
        'time':     ('🕐 Время', '09:00'),
    }
    label, example = FIELD_LABELS.get(field, ('Поле', '...'))

    context.user_data['waiting_for_ppr_field'] = {'id': entry_id, 'field': field}
    context.user_data['last_menu_msg_id'] = query.message.message_id

    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data=f"ppr_edit_{entry_id}")]]
    await query.edit_message_text(
        f"<b>✏️ Изменить: {label}</b>\n\n"
        f"Напиши новое значение:\n"
        f"Пример: <code>{example}</code>\n\n"
        f"👇 Напиши в чат:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def admin_ppr_field_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получено новое значение поля ППР."""
    info     = context.user_data.pop('waiting_for_ppr_field', None)
    if not info:
        return
    entry_id = info.get('id')
    field    = info.get('field')
    if not entry_id or not field:
        await update.message.reply_text("❌ Сессия истекла, открой задачу заново.")
        return
    value    = update.message.text.strip()

    ppr_list = load_ppr()
    item = next((i for i in ppr_list if str(i.get('id')) == str(entry_id)), None)
    if not item:
        await update.message.reply_text("❌ Задача не найдена.")
        return

    if field == 'equip':
        item['equipment'] = value
        display = value
    elif field == 'workshop':
        code = next((c for c, n in WORKSHOPS.items() if value.lower() in n.lower()), None)
        if not code:
            # попробовать напрямую
            code = 'bread' if 'хлеб' in value.lower() else 'bun' if 'булоч' in value.lower() else None
        if not code:
            context.user_data['waiting_for_ppr_field'] = info
            await update.message.reply_text(
                "❌ Не понял цех. Напиши <b>хлебный</b> или <b>булочный</b>.",
                parse_mode='HTML'
            )
            return
        item['workshop'] = code
        display = WORKSHOPS[code]
    elif field == 'task':
        item['task'] = value
        display = value
    elif field == 'weekday':
        weekday = DAY_CODES.get(value.lower())
        if weekday is None:
            context.user_data['waiting_for_ppr_field'] = info
            await update.message.reply_text(
                "❌ Не понял день. Напиши например <b>Пн</b> или <b>Среда</b>.",
                parse_mode='HTML'
            )
            return
        item['weekday'] = weekday
        display = DAY_NAMES[weekday]
    elif field == 'time':
        # Проверить формат и диапазон ЧЧ:ММ
        if not _valid_hhmm(value):
            context.user_data['waiting_for_ppr_field'] = info
            await update.message.reply_text(
                "❌ Формат времени: <code>09:00</code> (часы 0–23, минуты 0–59)", parse_mode='HTML')
            return
        item['time'] = value
        display = value
    else:
        return

    save_ppr(ppr_list)
    # Перепланировать сегодняшние уведомления: старые снять (день/время могли смениться),
    # актуальные поставить заново
    _resync_ppr_jobs(context.job_queue, entry_id)

    d     = item.get('weekday', 0)
    wname = WORKSHOPS.get(item.get('workshop',''), '')
    keyboard = [
        [InlineKeyboardButton("✏️ Ещё изменить", callback_data=f"ppr_edit_{entry_id}")],
        [InlineKeyboardButton("🔙 К задаче",      callback_data=f"ppr_task_{entry_id}")],
    ]
    await edit_or_send(update, context,
        f"✅ <b>Сохранено!</b>\n\n"
        f"🔧 {item['equipment']}\n"
        f"🏭 {wname}\n"
        f"📋 {item['task']}\n"
        f"📅 {DAY_NAMES[d]} | 🕐 {item['time']}",
        keyboard
    )

async def admin_ppr_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    entry_id = query.data.split('ppr_del_')[1]
    ppr_list = load_ppr()
    before = len(ppr_list)
    ppr_list = [i for i in ppr_list if i.get('id') != entry_id]
    if len(ppr_list) < before:
        save_ppr(ppr_list)
        # Снять сегодняшние уведомления удалённого ППР
        _unschedule_ppr_today(context.job_queue, entry_id)
        await query.answer("✅ Удалено!", show_alert=True)

    keyboard = []
    for item in sorted(ppr_list, key=lambda x: (x.get('weekday',0), x.get('time',''))):
        d = item.get('weekday', 0)
        label = f"❌ {DAY_SHORT[d]} {item['time']} — {item['equipment'][:25]}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"ppr_del_{item['id']}")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='admin_ppr')])
    await query.edit_message_text(
        "<b>❌ Выбери задачу для удаления:</b>" if ppr_list else "✅ Все задачи удалены.",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
    )

async def admin_ppr_send_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    weekday = datetime.now().weekday()
    sent = await send_ppr_notifications(context.bot, weekday=weekday)
    day_name = DAY_NAMES[weekday]
    msg = f"✅ Отправлено {sent} уведомлений ({day_name})." if sent else f"ℹ️ На {day_name} ППР-задач нет."
    await query.answer(msg, show_alert=True)

async def send_ppr_notifications(bot, weekday: int) -> int:
    """Рассылает ППР-уведомления за указанный день недели."""
    ppr_list = load_ppr()
    today_items = sorted(
        [item for item in ppr_list if item.get('weekday') == weekday and not _ppr_muted(item)],
        key=lambda x: x.get('time', '')
    )
    if not today_items:
        return 0

    users = load_users()
    mechanics = [int(uid) for uid, info in users.items() if info['role'] in ('mechanic', 'admin')]

    day_name = DAY_NAMES[weekday]
    lines = [f"🗓 <b>ПЛАНОВОЕ ТО — {day_name}</b>\n"]
    for item in today_items:
        wname = WORKSHOPS.get(item.get('workshop',''), item.get('workshop',''))
        lines.append(f"🕐 <b>{item['time']}</b> — {item['equipment']}\n   🏭 {wname}\n   🔧 {item['task']}")
    text = "\n\n".join(lines)

    sent = 0
    for mech_id in mechanics:
        try:
            await bot.send_message(chat_id=mech_id, text=text, parse_mode='HTML')
            sent += 1
        except Exception as e:
            logger.error(f"Ошибка ППР-уведомления для {mech_id}: {e}")
    return sent

async def daily_ppr_job(context: ContextTypes.DEFAULT_TYPE):
    """Ежедневная задача: рассылка ППР по дню недели."""
    weekday = datetime.now().weekday()
    await send_ppr_notifications(context.bot, weekday=weekday)

async def ppr_startup_digest(context: ContextTypes.DEFAULT_TYPE):
    """Catch-up: если бот запустился после времени рассылки — отправить сводку сейчас."""
    weekday = datetime.now().weekday()
    sent = await send_ppr_notifications(context.bot, weekday=weekday)
    logger.info(f"ППР catch-up сводка отправлена {sent} пользователям (день {weekday})")

# ============================================================================
# РАССЫЛКА (АДМИН)
# ============================================================================

async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("👷 Операторам",  callback_data='broadcast_target_operator')],
        [InlineKeyboardButton("🔧 Слесарям",   callback_data='broadcast_target_mechanic')],
        [InlineKeyboardButton("👥 Всем",        callback_data='broadcast_target_all')],
        [InlineKeyboardButton("🔙 Назад",       callback_data='role_admin')],
    ]
    await query.edit_message_text(
        "<b>📢 РАССЫЛКА</b>\n\nКому отправить сообщение?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def admin_broadcast_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    target = query.data.split('broadcast_target_')[1]  # operator / mechanic / all
    context.user_data['broadcast_target'] = target
    context.user_data['waiting_for_broadcast'] = True

    labels = {'operator': '👷 Операторам', 'mechanic': '🔧 Слесарям', 'all': '👥 Всем'}
    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data='role_admin')]]
    await query.edit_message_text(
        f"<b>📢 Рассылка → {labels[target]}</b>\n\n"
        f"Напиши сообщение в чат, и я разошлю его всем:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def admin_broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('waiting_for_broadcast', None)
    target = context.user_data.pop('broadcast_target', None)
    message = update.message.text

    users = load_users()
    if target == 'all':
        recipients = [int(uid) for uid in users]
    else:
        recipients = [int(uid) for uid, info in users.items() if info['role'] == target]

    # Не слать самому себе (или слать — на выбор, сейчас исключаем)
    sender_id = update.effective_user.id
    recipients = [uid for uid in recipients if uid != sender_id]

    broadcast_text = f"📢 <b>Сообщение от администратора:</b>\n\n{message}"
    sent, failed = 0, 0
    for uid in recipients:
        try:
            await context.bot.send_message(chat_id=uid, text=broadcast_text, parse_mode='HTML')
            sent += 1
        except Exception:
            failed += 1

    labels = {'operator': 'операторам', 'mechanic': 'слесарям', 'all': 'всем'}
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data='role_admin')]]
    await update.message.reply_text(
        f"✅ <b>Рассылка завершена</b>\n\n"
        f"Отправлено {labels.get(target, '')}: <b>{sent}</b>\n"
        f"Не доставлено: <b>{failed}</b>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

# ============================================================================
# ОЧИСТКА ИСТОРИИ (АДМИН)
# ============================================================================

async def admin_clear_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    done_c  = len([r for r in REQUESTS.values() if r['status'] == 'done'])
    total_c = len(REQUESTS)

    keyboard = [
        [InlineKeyboardButton("🗓 Старше 7 дней",  callback_data='clear_older_7')],
        [InlineKeyboardButton("🗓 Старше 30 дней", callback_data='clear_older_30')],
        [InlineKeyboardButton("🗓 Старше 90 дней", callback_data='clear_older_90')],
        [InlineKeyboardButton("📅 Указать дату",   callback_data='clear_by_date')],
        [InlineKeyboardButton(f"✅ Все выполненные ({done_c})", callback_data='clear_done')],
        [InlineKeyboardButton(f"⚠️ Все заявки ({total_c})",    callback_data='clear_all')],
        [InlineKeyboardButton("🔙 Назад",           callback_data='role_admin')],
    ]
    await query.edit_message_text(
        "<b>🗑 ОЧИСТКА ИСТОРИИ</b>\n\n"
        f"Сейчас в боте: <b>{total_c}</b> заявок (выполненных: <b>{done_c}</b>)\n\n"
        "Выбери что удалить:\n"
        "<i>⚠️ Удаление необратимо!</i>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def admin_clear_older(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Предпросмотр + подтверждение удаления заявок старше N дней."""
    query = update.callback_query
    await query.answer()

    days = int(query.data.split('_')[-1])
    cutoff = datetime.now() - timedelta(days=days)

    to_delete = []
    for req_id, r in REQUESTS.items():
        try:
            ts = datetime.strptime(r['timestamp'], '%d.%m.%Y %H:%M')
            if ts < cutoff:
                to_delete.append(req_id)
        except Exception:
            pass

    if not to_delete:
        await query.edit_message_text(
            f"✅ Нет заявок старше {days} дней.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='admin_clear_menu')]]),
            parse_mode='HTML'
        )
        return

    context.user_data['clear_ids'] = to_delete
    keyboard = [
        [InlineKeyboardButton(f"🗑 Да, удалить {len(to_delete)} заявок", callback_data='clear_confirm')],
        [InlineKeyboardButton("❌ Отмена", callback_data='admin_clear_menu')],
    ]
    await query.edit_message_text(
        f"<b>Удалить {len(to_delete)} заявок старше {days} дней?</b>\n\n"
        "<i>Это действие необратимо!</i>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def admin_clear_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение удаления всех выполненных заявок."""
    query = update.callback_query
    await query.answer()

    to_delete = [req_id for req_id, r in REQUESTS.items() if r['status'] == 'done']

    if not to_delete:
        await query.edit_message_text(
            "✅ Выполненных заявок нет.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='admin_clear_menu')]]),
            parse_mode='HTML'
        )
        return

    context.user_data['clear_ids'] = to_delete
    keyboard = [
        [InlineKeyboardButton(f"🗑 Да, удалить {len(to_delete)} заявок", callback_data='clear_confirm')],
        [InlineKeyboardButton("❌ Отмена", callback_data='admin_clear_menu')],
    ]
    await query.edit_message_text(
        f"<b>Удалить все {len(to_delete)} выполненных заявок?</b>\n\n"
        "<i>Это действие необратимо!</i>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def admin_clear_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение удаления ВСЕХ заявок."""
    query = update.callback_query
    await query.answer()

    to_delete = list(REQUESTS.keys())
    if not to_delete:
        await query.edit_message_text(
            "✅ Заявок нет.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='admin_clear_menu')]]),
            parse_mode='HTML'
        )
        return

    context.user_data['clear_ids'] = to_delete
    keyboard = [
        [InlineKeyboardButton(f"🗑 Да, удалить ВСЕ {len(to_delete)} заявок", callback_data='clear_confirm')],
        [InlineKeyboardButton("❌ Отмена", callback_data='admin_clear_menu')],
    ]
    await query.edit_message_text(
        f"<b>⚠️ УДАЛИТЬ ВСЕ {len(to_delete)} ЗАЯВОК?</b>\n\n"
        "<i>Это действие полностью очистит историю!</i>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def admin_clear_by_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Попросить ввести дату."""
    query = update.callback_query
    await query.answer()
    context.user_data['waiting_for_clear_date'] = True
    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data='admin_clear_menu')]]
    await query.edit_message_text(
        "<b>📅 Введи дату</b>\n\n"
        "Удалить заявки <b>до</b> указанной даты.\n"
        "Формат: <code>ДД.ММ.ГГГГ</code>\n\n"
        "Пример: <code>01.06.2025</code>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def admin_clear_date_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка введённой даты."""
    context.user_data.pop('waiting_for_clear_date', None)
    text = update.message.text.strip()
    try:
        cutoff = datetime.strptime(text, '%d.%m.%Y')
    except ValueError:
        context.user_data['waiting_for_clear_date'] = True
        await update.message.reply_text(
            "❌ Неверный формат. Введи дату в виде <code>ДД.ММ.ГГГГ</code>:",
            parse_mode='HTML'
        )
        return

    to_delete = []
    for req_id, r in REQUESTS.items():
        try:
            ts = datetime.strptime(r['timestamp'], '%d.%m.%Y %H:%M')
            if ts < cutoff:
                to_delete.append(req_id)
        except Exception:
            pass

    if not to_delete:
        await update.message.reply_text(
            f"✅ Нет заявок до {text}.",
            parse_mode='HTML'
        )
        return

    context.user_data['clear_ids'] = to_delete
    keyboard = [
        [InlineKeyboardButton(f"🗑 Да, удалить {len(to_delete)} заявок", callback_data='clear_confirm')],
        [InlineKeyboardButton("❌ Отмена", callback_data='admin_clear_menu')],
    ]
    await update.message.reply_text(
        f"<b>Удалить {len(to_delete)} заявок до {text}?</b>\n\n"
        "<i>Это действие необратимо!</i>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def admin_clear_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выполнить удаление."""
    query = update.callback_query
    await query.answer()

    to_delete = context.user_data.pop('clear_ids', [])
    if not to_delete:
        await query.edit_message_text("❌ Нет заявок для удаления.")
        return

    deleted = 0
    for req_id in to_delete:
        if req_id in REQUESTS:
            del REQUESTS[req_id]
            deleted += 1

    save_requests()

    keyboard = [[InlineKeyboardButton("🔙 В админ панель", callback_data='role_admin')]]
    await query.edit_message_text(
        f"✅ <b>Удалено {deleted} заявок.</b>\n\n"
        f"Осталось в боте: <b>{len(REQUESTS)}</b>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

# ============================================================================
# БЫСТРЫЙ ДОСТУП ЧЕРЕЗ REPLY-КЛАВИАТУРУ (для механиков)
# ============================================================================

async def operator_quick_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать меню оператора/бригадира новым сообщением внизу чата."""
    if get_user_role(update.effective_user.id) == 'brigadir':
        keyboard = [
            [InlineKeyboardButton("🗓 ППР сегодня",   callback_data='pprcheck')],
            [InlineKeyboardButton("🏭 Мои цеха",      callback_data='brig_shops')],
            [InlineKeyboardButton("➕ Новая заявка",  callback_data='operator_new_request')],
            [InlineKeyboardButton("📋 Мои заявки",    callback_data='operator_my_requests')],
            [InlineKeyboardButton("🔙 Главное меню",  callback_data='back_to_main')],
        ]
        text = "<b>🧑‍🏭 Меню Бригадира</b>\n\nОтметь итоги ППР или подай заявку."
    else:
        keyboard = [
            [InlineKeyboardButton("➕ Новая заявка",  callback_data='operator_new_request')],
            [InlineKeyboardButton("📋 Мои заявки",    callback_data='operator_my_requests')],
            [InlineKeyboardButton("🔙 Главное меню",  callback_data='back_to_main')],
        ]
        text = "<b>👷 Меню Оператора</b>\n\nПодай новую заявку или посмотри статус старых."
    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id, text=text,
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
    )
    context.user_data['last_menu_msg_id'] = msg.message_id

async def mechanic_quick_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать меню мех группы новым сообщением внизу чата."""
    new_count       = len([r for r in REQUESTS.values() if r['status'] == 'new'])
    in_prog_count   = len([r for r in REQUESTS.values() if r['status'] == 'in_progress'])
    postponed_count = len([r for r in REQUESTS.values() if r['status'] == 'postponed'])
    user_id = update.effective_user.id if update.effective_user else 0
    keyboard = [
        [InlineKeyboardButton(checkin_label(user_id),                 callback_data='checkin_today')],
        [InlineKeyboardButton(f"📬 Новые заявки ({new_count})",        callback_data='mechanic_new_requests')],
        [InlineKeyboardButton(f"⏳ В работе ({in_prog_count})",         callback_data='mechanic_in_progress')],
        [InlineKeyboardButton(f"⏸ Отложенные ({postponed_count})",     callback_data='mechanic_postponed')],
        [InlineKeyboardButton("✅ Завершенные",                          callback_data='mechanic_completed')],
        [InlineKeyboardButton("📝 Прочие работы",                       callback_data='mechanic_other_tasks')],
        [InlineKeyboardButton("🏆 Рейтинг", callback_data='mechanic_rating')],
        [InlineKeyboardButton("🔙 Главное меню",                        callback_data='back_to_main')]
    ]
    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="<b>🔧 Меню Слесарной группы</b>\n\nВыбери раздел:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
    context.user_data['last_menu_msg_id'] = msg.message_id

async def mechanic_quick_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Новые заявки — всегда новое сообщение внизу чата."""
    new_reqs = [r for r in REQUESTS.values() if r['status'] == 'new']
    if not new_reqs:
        text = "📬 <b>Новые заявки</b>\n\nНовых заявок нет. 👍"
        keyboard = [[InlineKeyboardButton("🔙 Меню", callback_data='role_mechanic')]]
    else:
        urgency_order = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
        new_reqs.sort(key=lambda x: urgency_order.get(x['urgency'], 9))
        lines = [f"📬 <b>НОВЫЕ ЗАЯВКИ ({len(new_reqs)})</b>\n"]
        for r in new_reqs:
            emoji = URGENCY_EMOJI.get(r['urgency'], '')
            photo_mark = ' 📸' if r.get('photo_file_id') else ''
            lines.append(
                f"{emoji} <b>{r['problem']}{photo_mark}</b>\n"
                f"   🏭 {WORKSHOPS.get(r['workshop'], r['workshop'])}\n"
                f"   {URGENCY_LEVELS.get(r['urgency'], '')} | 🕐 {r['timestamp']}\n"
                f"   👷 {r['user_name']}\n"
            )
        keyboard = [
            [InlineKeyboardButton(f"✅ Принять {r['id']}", callback_data=f'accept_{r["id"]}')]
            for r in new_reqs
        ]
        keyboard.append([InlineKeyboardButton("🔙 Меню", callback_data='role_mechanic')])
        text = "\n".join(lines)
    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
    context.user_data['last_menu_msg_id'] = msg.message_id

async def mechanic_quick_inprogress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """В работе — всегда новое сообщение внизу чата."""
    in_prog = [r for r in REQUESTS.values() if r['status'] == 'in_progress']
    if not in_prog:
        text = "⏳ <b>В работе</b>\n\nНет заявок в работе."
        keyboard = [[InlineKeyboardButton("🔙 Меню", callback_data='role_mechanic')]]
    else:
        lines = [f"⏳ <b>В РАБОТЕ ({len(in_prog)})</b>\n"]
        keyboard = []
        for r in in_prog:
            emoji = URGENCY_EMOJI.get(r['urgency'], '')
            lines.append(
                f"{emoji} <b>{r['problem']}</b>\n"
                f"   🏭 {WORKSHOPS.get(r['workshop'], r['workshop'])}\n"
                f"   🔧 {r.get('mechanic_name', '—')} | 🕐 {r['timestamp']}\n"
            )
            keyboard.append([
                InlineKeyboardButton(f"✅ Завершить {r['id']}", callback_data=f'done_{r["id"]}'),
                InlineKeyboardButton(f"⏸ Отложить {r['id']}",  callback_data=f'postpone_{r["id"]}'),
            ])
        keyboard.append([InlineKeyboardButton("🔙 Меню", callback_data='role_mechanic')])
        text = "\n".join(lines)
    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
    context.user_data['last_menu_msg_id'] = msg.message_id

async def mechanic_quick_completed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Завершённые — всегда новое сообщение внизу чата."""
    all_done = sorted(
        [r for r in REQUESTS.values() if r['status'] == 'done'],
        key=lambda x: x.get('done_time', x['timestamp']), reverse=True
    )
    total_done = len(all_done)
    shown_done = all_done[:50]
    if not shown_done:
        text = "✅ <b>Завершённые</b>\n\nЗавершённых заявок пока нет."
    else:
        lines = [f"✅ <b>ЗАВЕРШЁННЫЕ ({total_done})</b>"]
        if total_done > 50:
            lines.append(f"<i>Показаны последние 50 из {total_done}</i>\n")
        else:
            lines.append("")
        for r in shown_done:
            entry = (
                f"✅ <b>{r['problem']}</b>\n"
                f"   🏭 {WORKSHOPS.get(r['workshop'], r['workshop'])}\n"
                f"   🔧 {r.get('mechanic_name', '—')} | ✅ {r.get('done_time', '—')}\n"
            )
            if r.get('done_comment'):
                entry += f"   💬 {r['done_comment']}\n"
            lines.append(entry)
        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:4000] + "\n\n<i>... (обрезано)</i>"
    keyboard = [[InlineKeyboardButton("🔙 Меню", callback_data='role_mechanic')]]
    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
    context.user_data['last_menu_msg_id'] = msg.message_id

async def cmd_testsheets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/testsheets — диагностика подключения к Google Sheets."""
    if not is_admin(update.effective_user.id):
        return
    lines = ["🔬 <b>Диагностика Google Sheets</b>\n"]
    # 1. Service Account авторизация
    try:
        client = _gs_client()
        lines.append("✅ Service Account авторизован")
    except Exception as e:
        lines.append(f"❌ Service Account: <code>{html_lib.escape(str(e))}</code>")
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')
        return
    # 2. Открытие таблицы
    try:
        ss = client.open(SPREADSHEET_NAME)
        lines.append(f"✅ Таблица открыта: {ss.title}")
        lines.append(f"   Листы: {html_lib.escape(str([w.title for w in ss.worksheets()]))}")
    except Exception as e:
        lines.append(f"❌ Открытие таблицы: <code>{html_lib.escape(str(e))}</code>")
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')
        return
    # 3. Тестовая запись
    try:
        sheet = get_sheet('bread')
        lines.append(f"✅ get_sheet('bread') OK — '{sheet.title}'")
    except Exception as e:
        lines.append(f"❌ get_sheet('bread'): <code>{html_lib.escape(str(e))}</code>")
    await update.message.reply_text("\n".join(lines), parse_mode='HTML')

async def cmd_testppr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/testppr — тест ППР уведомлений (только для администратора)."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Только для администратора.")
        return
    now      = datetime.now()
    weekday  = now.weekday()
    day_name = DAY_NAMES[weekday]
    ppr_list = load_ppr()
    today_tasks = sorted(
        [p for p in ppr_list if p.get('weekday') == weekday],
        key=lambda x: x.get('time', '')
    )
    # Отправляем утреннюю сводку
    if today_tasks:
        sent = await send_ppr_notifications(context.bot, weekday=weekday)
    else:
        sent = 0
    # Информация о запланированных run_once-задачах
    if context.application.job_queue:
        jobs = context.application.job_queue.jobs()
        ppr_jobs = [j.name for j in jobs if j.name and j.name.startswith('ppr_')]
    else:
        ppr_jobs = []
    mechanics = get_all_mechanics()
    lines = [
        f"🔬 <b>Диагностика ППР</b>",
        f"📅 Сегодня: {now.strftime('%d.%m.%Y %H:%M')} ({day_name})",
        f"👷 Слесарей в системе: {len(mechanics)}",
        f"📋 ППР задач на сегодня (день {weekday}): {len(today_tasks)}",
        f"📨 Сводка отправлена: {sent} чел.",
        f"⏱ Активных ППР-заданий (run_once): {len(ppr_jobs)}",
    ]
    if ppr_jobs:
        lines.append("\n<b>Активные ППР-задания:</b>")
        for jname in sorted(ppr_jobs):
            lines.append(f"  • {jname}")
    if today_tasks:
        lines.append(f"\n<b>Задачи дня:</b>")
        for t in today_tasks:
            wname = WORKSHOPS.get(t.get('workshop', ''), t.get('workshop', ''))
            lines.append(f"  {t['time']} — {t['equipment']} ({wname})")
    await update.message.reply_text("\n".join(lines), parse_mode='HTML')

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/menu — быстрый вызов меню текущей роли."""
    role = get_user_role(update.effective_user.id)
    if not role:
        await update.message.reply_text("❌ У тебя нет доступа. Напиши /start")
        return
    await _send_role_menu(update, context)

# ============================================================================
# ПРОЧИЕ ЗАДАЧИ — АДМИН
# ============================================================================

async def admin_other_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запуск записи прочей задачи (из админки). Шаг 1: назначен слесарю."""
    query = update.callback_query
    await query.answer()
    admin_name = update.effective_user.full_name or update.effective_user.first_name
    context.user_data['ot_step']      = 'ot_worker'
    context.user_data['ot_from_role'] = 'admin'
    context.user_data['ot_data'] = {
        'created_at': datetime.now().strftime('%d.%m.%Y %H:%M'),
        'created_by': admin_name,
    }
    context.user_data['last_menu_msg_id'] = query.message.message_id
    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data='role_admin')]]
    await query.edit_message_text(
        "<b>📝 Новая прочая задача — шаг 1/4</b>\n\n"
        "👤 Напиши <b>кому назначена</b> (ФИО или имя слесаря):",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def mechanic_other_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запуск записи прочей задачи (из меню механика). Worker = сам механик, шаг 1: место."""
    query = update.callback_query
    await query.answer()
    mech_name = update.effective_user.full_name or update.effective_user.first_name
    context.user_data['ot_step']      = 'ot_location'
    context.user_data['ot_from_role'] = 'mechanic'
    context.user_data['ot_data'] = {
        'created_at': datetime.now().strftime('%d.%m.%Y %H:%M'),
        'created_by': mech_name,
        'worker':     mech_name,
    }
    context.user_data['last_menu_msg_id'] = query.message.message_id
    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data='role_mechanic')]]
    await query.edit_message_text(
        f"<b>📝 Прочие работы — шаг 1/3</b>\n\n"
        f"👤 Исполнитель: <b>{mech_name}</b>\n\n"
        "📍 Напиши <b>место / куда</b> (где выполнялась работа):",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )


async def ot_text_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых шагов записи прочей задачи."""
    step      = context.user_data.get('ot_step')
    from_role = context.user_data.get('ot_from_role', 'admin')
    back_cb   = 'role_mechanic' if from_role == 'mechanic' else 'role_admin'
    text = update.message.text.strip()
    d    = context.user_data.setdefault('ot_data', {})
    keyboard_cancel = [[InlineKeyboardButton("❌ Отмена", callback_data=back_cb)]]

    if step == 'ot_worker':
        # Только для admin-потока
        d['worker'] = text
        context.user_data['ot_step'] = 'ot_location'
        await edit_or_send(update, context,
            f"👤 Назначен: <b>{text}</b>\n\n"
            "<b>📝 Шаг 2/4</b>\n\n"
            "📍 Напиши <b>место / куда</b>:",
            keyboard_cancel
        )

    elif step == 'ot_location':
        d['location'] = text
        if from_role == 'mechanic':
            # Показываем выбор напарника
            context.user_data['ot_step'] = 'ot_partner_ask'
            await edit_or_send(update, context,
                f"👤 {d.get('worker', '')}  |  📍 {text}\n\n"
                "👥 <b>Был ли напарник?</b>",
                [
                    [InlineKeyboardButton("👤 Работал один",       callback_data='ot_solo')],
                    [InlineKeyboardButton("👥 Добавить напарника", callback_data='ot_with_partner')],
                    [InlineKeyboardButton("❌ Отмена",              callback_data=back_cb)],
                ]
            )
        else:
            context.user_data['ot_step'] = 'ot_work_type'
            await edit_or_send(update, context,
                f"👤 {d.get('worker', '')}  |  📍 {text}\n\n"
                "<b>📝 Шаг 3/4</b>\n\n"
                "🔧 Напиши <b>вид работы</b>:",
                keyboard_cancel
            )

    elif step == 'ot_partner':
        # Механик ввёл имя напарника
        d['partner'] = text
        context.user_data['ot_step'] = 'ot_work_type'
        await edit_or_send(update, context,
            f"👤 {d.get('worker', '')} + {text}  |  📍 {d.get('location', '')}\n\n"
            "<b>📝 Шаг 2</b>\n\n"
            "🔧 Напиши <b>что сделал</b> (вид работы):",
            keyboard_cancel
        )

    elif step == 'ot_work_type':
        d['work_type'] = text
        context.user_data['ot_step'] = 'ot_description'
        step_label = "Шаг 3" if from_role == 'mechanic' else "Шаг 4/4"
        partner = d.get('partner')
        worker_line = f"👤 {d.get('worker', '')} + {partner}" if partner else f"👤 {d.get('worker', '')}"
        await edit_or_send(update, context,
            f"{worker_line}  |  📍 {d.get('location', '')}\n"
            f"🔧 {text}\n\n"
            f"<b>📝 {step_label}</b>\n\n"
            "📄 Напиши <b>описание</b> (подробности, доп. инфо)\n"
            "<i>или нажми «Пропустить»</i>:",
            [
                [InlineKeyboardButton("➡️ Пропустить", callback_data='ot_skip_desc')],
                [InlineKeyboardButton("❌ Отмена",      callback_data=back_cb)],
            ]
        )

    elif step == 'ot_description':
        d['description'] = text
        await _ot_save_and_confirm(update, context)


async def ot_solo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Механик выбрал «работал один» — идём сразу к виду работы."""
    query = update.callback_query
    await query.answer()
    d = context.user_data.setdefault('ot_data', {})
    d.pop('partner', None)
    context.user_data['ot_step'] = 'ot_work_type'
    context.user_data['last_menu_msg_id'] = query.message.message_id
    await query.edit_message_text(
        f"👤 {d.get('worker', '')}  |  📍 {d.get('location', '')}\n\n"
        "<b>📝 Шаг 2</b>\n\n"
        "🔧 Напиши <b>что сделал</b> (вид работы):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data='role_mechanic')]]),
        parse_mode='HTML'
    )

async def ot_with_partner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Механик выбрал «добавить напарника» — просим ввести имя."""
    query = update.callback_query
    await query.answer()
    d = context.user_data.setdefault('ot_data', {})
    context.user_data['ot_step'] = 'ot_partner'
    context.user_data['last_menu_msg_id'] = query.message.message_id
    await query.edit_message_text(
        f"👤 {d.get('worker', '')}  |  📍 {d.get('location', '')}\n\n"
        "👥 Напиши <b>имя напарника</b>:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data='role_mechanic')]]),
        parse_mode='HTML'
    )

async def admin_ot_skip_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пропуск описания → сохранение."""
    query = update.callback_query
    # Защита: устаревшая кнопка (после перезапуска бота ot_data уже нет)
    if not context.user_data.get('ot_data'):
        await query.answer("❌ Сессия истекла, начни запись задачи заново.", show_alert=True)
        return
    await query.answer()
    context.user_data['ot_data']['description'] = ''
    context.user_data['last_menu_msg_id'] = query.message.message_id
    await _ot_save_and_confirm(update, context)


async def _ot_save_and_confirm(update, context):
    """Сохранить задачу в хранилище и Sheets, показать подтверждение."""
    d = context.user_data.pop('ot_data', {})
    context.user_data.pop('ot_step', None)

    ot_id   = new_ot_id()
    partner = d.get('partner', '')
    worker_display = f"{d.get('worker', '')} + {partner}" if partner else d.get('worker', '')
    task = {
        'id':          ot_id,
        'created_at':  d.get('created_at', datetime.now().strftime('%d.%m.%Y %H:%M')),
        'created_by':  d.get('created_by', ''),
        'worker':      worker_display,
        'location':    d.get('location', ''),
        'work_type':   d.get('work_type', ''),
        'description': d.get('description', ''),
    }
    OTHER_TASKS[ot_id] = task
    save_other_tasks()

    row_ok = False
    try:
        sheet = get_other_tasks_sheet()
        sheet.append_row([
            task['id'],
            task['created_at'],
            task['created_by'],
            task['worker'],
            task['location'],
            task['work_type'],
            task['description'],
        ])
        row_ok = True
    except Exception as e:
        logger.error(f"Ошибка записи прочей задачи в Sheets: {e}")

    status    = "✅ Записано в Google Sheets!" if row_ok else "⚠️ Ошибка записи в Sheets"
    from_role = context.user_data.pop('ot_from_role', 'admin')
    if from_role == 'mechanic':
        keyboard = [
            [InlineKeyboardButton("➕ Ещё одну",      callback_data='mechanic_other_tasks')],
            [InlineKeyboardButton("🔙 Меню слесаря",  callback_data='role_mechanic')],
        ]
    else:
        keyboard = [
            [InlineKeyboardButton("➕ Ещё одну",    callback_data='admin_other_tasks')],
            [InlineKeyboardButton("🔙 Админ панель", callback_data='role_admin')],
        ]
    await edit_or_send(update, context,
        f"✅ <b>Задача {ot_id} записана!</b>\n\n"
        f"📅 {task['created_at']}\n"
        f"👤 Исполнитель: {task['worker']}\n"
        f"📍 {task['location']}\n"
        f"🔧 {task['work_type']}\n"
        f"📄 {task['description'] or '—'}\n\n"
        f"{status}",
        keyboard
    )

# ============================================================================
# ОБРАБОТЧИКИ ТЕКСТА, ФОТО И КНОПКА НАЗАД
# ============================================================================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Нажатие нижней клавиатуры (Меню / частые действия) — срабатывает в любой момент,
    # даже посреди пошагового ввода: сбрасываем черновик и выполняем действие.
    txt = (update.message.text or '').strip()
    if txt in REPLY_BUTTONS:
        try:
            await update.message.delete()
        except Exception:
            pass
        for _k in ('waiting_for_problem', 'waiting_for_op_comment', 'waiting_for_photo',
                   'workshop', 'section', 'problem', 'urgency', 'photo_file_id', 'op_comment'):
            context.user_data.pop(_k, None)
        if txt == MENU_BTN:
            await open_role_menu(update, context)
        elif txt == BTN_NEW_REQ:
            await operator_new_request(update, context)
        elif txt == BTN_MY_REQ:
            await operator_my_requests(update, context)
        elif txt == BTN_NEW_TASKS:
            await mechanic_new_requests(update, context)
        elif txt == BTN_CHECKIN:
            await mechanic_checkin(update, context)
        return

    # Удаляем сообщение пользователя чтобы чат не скроллился
    try:
        await update.message.delete()
    except Exception:
        pass

    if context.user_data.get('waiting_for_problem'):
        await problem_received(update, context)
    elif context.user_data.get('waiting_for_op_comment'):
        await operator_comment_received(update, context)
    elif context.user_data.get('waiting_for_postpone_reason'):
        await postpone_reason_received(update, context)
    elif context.user_data.get('waiting_for_done_comment'):
        await done_comment_received(update, context)
    elif context.user_data.get('waiting_for_eta'):
        await mechanic_eta_text_received(update, context)
    elif context.user_data.get('waiting_for_clear_date'):
        await admin_clear_date_received(update, context)
    elif context.user_data.get('waiting_for_broadcast'):
        await admin_broadcast_send(update, context)
    elif context.user_data.get('waiting_for_ppr_postpone'):
        await ppr_postpone_reason_received(update, context)
    elif context.user_data.get('waiting_for_ppr_field'):
        await admin_ppr_field_received(update, context)
    elif context.user_data.get('ppr_new_step') in ('equipment', 'task', 'time'):
        await admin_ppr_new_text(update, context)
    elif context.user_data.get('ot_step'):
        await ot_text_received(update, context)
    elif context.user_data.get('inv_state'):
        await admin_inv_text_received(update, context)
    else:
        await update.message.reply_text("Используй кнопки меню или напиши /start 🙂")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Удаляем фото пользователя чтобы чат не скроллился
    try:
        await update.message.delete()
    except Exception:
        pass

    try:
        if context.user_data.get('waiting_for_photo'):
            await operator_photo_received(update, context)
        elif context.user_data.get('waiting_for_done_photo'):
            await mechanic_done_photo_received(update, context)
        else:
            await edit_or_send(update, context,
                "📸 Фото получено, но сейчас не ожидается.\n"
                "Используй кнопки меню или /start",
                [[InlineKeyboardButton("🏠 Главное меню", callback_data='back_to_main')]]
            )
    except Exception as e:
        logger.error(f"Ошибка handle_photo: {e}", exc_info=True)

async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()

    user_id = update.effective_user.id
    role    = get_user_role(user_id)

    keyboard = []
    if role in ('operator', 'admin'):
        keyboard.append([InlineKeyboardButton("👷 Оператор (подать заявку)", callback_data='role_operator')])
    if role in ('mechanic', 'admin'):
        keyboard.append([InlineKeyboardButton("🔧 Слесарная группа (принять заявку)", callback_data='role_mechanic')])
    if role in ('brigadir', 'admin'):
        keyboard.append([InlineKeyboardButton("🧑‍🏭 Бригадир (ППР + заявки)", callback_data='role_brigadir')])
    if role == 'admin':
        keyboard.append([InlineKeyboardButton("👨‍💼 Админ (отчеты)", callback_data='role_admin')])

    await query.edit_message_text(
        f"🏭 <b>Главное меню</b>\n\nТвоя роль: {ROLE_NAMES.get(role, role) if role else '❌ Нет доступа'}\n\nВыбери действие:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

# ============================================================================
# УПРАВЛЕНИЕ ИМУЩЕСТВОМ — обработчики
# ============================================================================

# ---- вспомогательные функции рендеринга ----

async def _inv_show_workshops(update, context):
    lines = ["<b>🏭 УПРАВЛЕНИЕ ИМУЩЕСТВОМ</b>\n"]
    if not WORKSHOPS:
        lines.append("<i>Цехов пока нет.</i>")
    else:
        for wid, wname in WORKSHOPS.items():
            sec_cnt = len(WORKSHOP_SECTIONS.get(wid, {}))
            lines.append(f"• {wname} ({sec_cnt} разд.)")
    keyboard = [
        [InlineKeyboardButton(f"🏭 {wname}", callback_data=f'inv_w_{wid}')]
        for wid, wname in WORKSHOPS.items()
    ]
    keyboard.append([InlineKeyboardButton("➕ Добавить цех", callback_data='inv_aw')])
    keyboard.append([InlineKeyboardButton("🔙 Назад",        callback_data='role_admin')])
    await edit_or_send(update, context, "\n".join(lines), keyboard)

async def _inv_show_workshop(update, context, wid: str):
    wname = WORKSHOPS.get(wid, wid)
    secs  = WORKSHOP_SECTIONS.get(wid, {})
    lines = [f"<b>🏭 {wname}</b>\n"]
    if not secs:
        lines.append("<i>Разделов пока нет.</i>")
    else:
        for sid, sname in secs.items():
            eq_cnt = len(SECTION_EQUIPMENT.get((wid, sid), []))
            lines.append(f"• {sname} — {eq_cnt} ед.")
    keyboard = [
        [InlineKeyboardButton(f"📂 {sname}", callback_data=f'inv_s_{wid}|{sid}')]
        for sid, sname in secs.items()
    ]
    keyboard.append([InlineKeyboardButton("➕ Добавить раздел", callback_data=f'inv_as_{wid}')])
    keyboard.append([InlineKeyboardButton("🗑 Удалить цех",     callback_data=f'inv_xw_{wid}')])
    keyboard.append([InlineKeyboardButton("🔙 К цехам",         callback_data='admin_inventory')])
    await edit_or_send(update, context, "\n".join(lines), keyboard)

async def _inv_show_section(update, context, wid: str, sid: str):
    wname  = WORKSHOPS.get(wid, wid)
    sname  = WORKSHOP_SECTIONS.get(wid, {}).get(sid, sid)
    equips = SECTION_EQUIPMENT.get((wid, sid), [])
    lines  = [f"<b>🏭 {wname} → 📂 {sname}</b>\n"]
    if not equips:
        lines.append("<i>Оборудования пока нет.</i>")
    else:
        for i, eq in enumerate(equips, 1):
            lines.append(f"{i}. {eq}")
    # Кнопки удаления по каждой позиции
    keyboard = [
        [InlineKeyboardButton(f"🗑 {eq[:30]}", callback_data=f'inv_xep_{wid}|{sid}|{i}')]
        for i, eq in enumerate(equips)
    ]
    keyboard.append([InlineKeyboardButton("➕ Добавить оборудование", callback_data=f'inv_ae_{wid}|{sid}')])
    keyboard.append([InlineKeyboardButton("🗑 Удалить раздел",        callback_data=f'inv_xs_{wid}|{sid}')])
    keyboard.append([InlineKeyboardButton("🔙 К цеху",                callback_data=f'inv_w_{wid}')])
    await edit_or_send(update, context, "\n".join(lines), keyboard)

# ---- основные обработчики ----

async def admin_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(update.effective_user.id):
        await query.answer("❌ Нет доступа!", show_alert=True)
        return
    await query.answer()
    context.user_data['last_menu_msg_id'] = query.message.message_id
    await _inv_show_workshops(update, context)

async def admin_inv_workshop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    wid = query.data.split('inv_w_', 1)[1]
    context.user_data['last_menu_msg_id'] = query.message.message_id
    await _inv_show_workshop(update, context, wid)

async def admin_inv_section(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        rest = query.data.split('inv_s_', 1)[1]
        wid, sid = rest.split('|', 1)
    except (IndexError, ValueError):
        await query.answer("❌ Ошибка данных", show_alert=True)
        return
    context.user_data['last_menu_msg_id'] = query.message.message_id
    await _inv_show_section(update, context, wid, sid)

async def admin_inv_add_workshop_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['inv_state'] = 'add_workshop'
    context.user_data['last_menu_msg_id'] = query.message.message_id
    await edit_or_send(update, context,
        "🏭 <b>Новый цех</b>\n\nВведи название цеха:",
        [[InlineKeyboardButton("❌ Отмена", callback_data='admin_inventory')]]
    )

async def admin_inv_add_section_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    wid = query.data.split('inv_as_', 1)[1]
    context.user_data['inv_state'] = 'add_section'
    context.user_data['inv_wid']   = wid
    context.user_data['last_menu_msg_id'] = query.message.message_id
    wname = WORKSHOPS.get(wid, wid)
    await edit_or_send(update, context,
        f"📂 <b>Новый раздел в «{wname}»</b>\n\nВведи название раздела:",
        [[InlineKeyboardButton("❌ Отмена", callback_data=f'inv_w_{wid}')]]
    )

async def admin_inv_add_equipment_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rest = query.data.split('inv_ae_', 1)[1]
    wid, sid = rest.split('|', 1)
    context.user_data['inv_state'] = 'add_equipment'
    context.user_data['inv_wid']   = wid
    context.user_data['inv_sid']   = sid
    context.user_data['last_menu_msg_id'] = query.message.message_id
    sname = WORKSHOP_SECTIONS.get(wid, {}).get(sid, sid)
    await edit_or_send(update, context,
        f"⚙️ <b>Новое оборудование в «{sname}»</b>\n\nВведи название:",
        [[InlineKeyboardButton("❌ Отмена", callback_data=f'inv_s_{wid}|{sid}')]]
    )

async def admin_inv_del_workshop_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    wid  = query.data.split('inv_xw_', 1)[1]
    wname = WORKSHOPS.get(wid, wid)
    sec_cnt = len(WORKSHOP_SECTIONS.get(wid, {}))
    eq_cnt  = sum(len(SECTION_EQUIPMENT.get((wid, s), [])) for s in WORKSHOP_SECTIONS.get(wid, {}))
    context.user_data['last_menu_msg_id'] = query.message.message_id
    await edit_or_send(update, context,
        f"⚠️ <b>Удалить цех «{wname}»?</b>\n\n"
        f"Разделов: {sec_cnt}  |  Оборудования: {eq_cnt} ед.\n\n"
        f"Всё содержимое цеха будет удалено безвозвратно.",
        [
            [InlineKeyboardButton("🗑 Да, удалить", callback_data=f'inv_xwc_{wid}')],
            [InlineKeyboardButton("❌ Отмена",       callback_data=f'inv_w_{wid}')]
        ]
    )

async def admin_inv_del_workshop_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    wid   = query.data.split('inv_xwc_', 1)[1]

    # Защита: нельзя удалять цех, пока по нему есть незакрытые заявки —
    # иначе они «осиротеют» и перестанут писаться в Google Sheets.
    active = [r for r in REQUESTS.values()
              if r.get('workshop') == wid and r.get('status') in ('new', 'in_progress', 'postponed')]
    if active:
        await query.answer()
        ids = ", ".join(r['id'] for r in active[:10])
        await edit_or_send(update, context,
            f"⛔️ <b>Нельзя удалить цех «{WORKSHOPS.get(wid, wid)}»</b>\n\n"
            f"По нему есть незакрытые заявки ({len(active)}): {ids}\n\n"
            f"Сначала заверши или отмени их, потом удаляй цех.",
            [[InlineKeyboardButton("🔙 К цеху", callback_data=f'inv_w_{wid}')]]
        )
        return

    wname = WORKSHOPS.pop(wid, wid)
    WORKSHOP_SECTIONS.pop(wid, None)
    for k in [k for k in list(SECTION_EQUIPMENT.keys()) if k[0] == wid]:
        del SECTION_EQUIPMENT[k]
    save_inventory()
    await query.answer(f"✅ Цех «{wname}» удалён", show_alert=True)
    context.user_data['last_menu_msg_id'] = query.message.message_id
    await _inv_show_workshops(update, context)

async def admin_inv_del_section_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rest = query.data.split('inv_xs_', 1)[1]
    wid, sid = rest.split('|', 1)
    sname    = WORKSHOP_SECTIONS.get(wid, {}).get(sid, sid)
    eq_cnt   = len(SECTION_EQUIPMENT.get((wid, sid), []))
    context.user_data['last_menu_msg_id'] = query.message.message_id
    await edit_or_send(update, context,
        f"⚠️ <b>Удалить раздел «{sname}»?</b>\n\n"
        f"Оборудования: {eq_cnt} ед.\n\nВсё оборудование будет удалено.",
        [
            [InlineKeyboardButton("🗑 Да, удалить", callback_data=f'inv_xsc_{wid}|{sid}')],
            [InlineKeyboardButton("❌ Отмена",       callback_data=f'inv_s_{wid}|{sid}')]
        ]
    )

async def admin_inv_del_section_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    rest  = query.data.split('inv_xsc_', 1)[1]
    wid, sid = rest.split('|', 1)
    sname = WORKSHOP_SECTIONS.get(wid, {}).get(sid, sid)
    if wid in WORKSHOP_SECTIONS:
        WORKSHOP_SECTIONS[wid].pop(sid, None)
    SECTION_EQUIPMENT.pop((wid, sid), None)
    save_inventory()
    await query.answer(f"✅ Раздел «{sname}» удалён", show_alert=True)
    context.user_data['last_menu_msg_id'] = query.message.message_id
    await _inv_show_workshop(update, context, wid)

async def admin_inv_del_equipment_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает страницу подтверждения удаления оборудования."""
    query = update.callback_query
    await query.answer()
    try:
        rest  = query.data.split('inv_xep_', 1)[1]
        parts = rest.split('|')
        wid, sid, idx = parts[0], parts[1], int(parts[2])
    except (IndexError, ValueError):
        await query.answer("❌ Ошибка данных", show_alert=True)
        return
    equips = SECTION_EQUIPMENT.get((wid, sid), [])
    if 0 <= idx < len(equips):
        eq_name = equips[idx]
        sname = WORKSHOP_SECTIONS.get(wid, {}).get(sid, sid)
        context.user_data['last_menu_msg_id'] = query.message.message_id
        await edit_or_send(update, context,
            f"⚠️ <b>Удалить оборудование?</b>\n\n"
            f"⚙️ {eq_name}\n"
            f"📂 Раздел: {sname}",
            [
                [InlineKeyboardButton("🗑 Да, удалить", callback_data=f'inv_xec_{wid}|{sid}|{idx}')],
                [InlineKeyboardButton("❌ Отмена",       callback_data=f'inv_s_{wid}|{sid}')]
            ]
        )
    else:
        await query.answer("❌ Не найдено (список обновился)", show_alert=True)
        context.user_data['last_menu_msg_id'] = query.message.message_id
        await _inv_show_section(update, context, wid, sid)

async def admin_inv_del_equipment_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет оборудование после подтверждения."""
    query = update.callback_query
    try:
        rest  = query.data.split('inv_xec_', 1)[1]
        parts = rest.split('|')
        wid, sid, idx = parts[0], parts[1], int(parts[2])
    except (IndexError, ValueError):
        await query.answer("❌ Ошибка данных", show_alert=True)
        return
    equips = SECTION_EQUIPMENT.get((wid, sid), [])
    if 0 <= idx < len(equips):
        removed = equips.pop(idx)
        SECTION_EQUIPMENT[(wid, sid)] = equips
        save_inventory()
        await query.answer(f"✅ «{removed[:30]}» удалено", show_alert=True)
    else:
        await query.answer("❌ Не найдено (список обновился)", show_alert=True)
    context.user_data['last_menu_msg_id'] = query.message.message_id
    await _inv_show_section(update, context, wid, sid)

async def admin_inv_text_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает текстовый ввод при добавлении цеха / раздела / оборудования."""
    state = context.user_data.pop('inv_state', None)
    name  = (update.message.text or '').strip() if update.message else ''

    if not name:
        context.user_data['inv_state'] = state  # вернуть стейт
        await edit_or_send(update, context,
            "❌ Название не может быть пустым. Введи снова:",
            [[InlineKeyboardButton("❌ Отмена", callback_data='admin_inventory')]]
        )
        return

    if state == 'add_workshop':
        wid = _inv_new_id('w')
        WORKSHOPS[wid] = name
        WORKSHOP_SECTIONS[wid] = {}
        save_inventory()
        await edit_or_send(update, context,
            f"✅ <b>Цех «{name}» добавлен!</b>\n\nТеперь можно добавить разделы.",
            [
                [InlineKeyboardButton(f"🏭 Открыть цех", callback_data=f'inv_w_{wid}')],
                [InlineKeyboardButton("🔙 Все цеха",     callback_data='admin_inventory')]
            ]
        )

    elif state == 'add_section':
        wid = context.user_data.pop('inv_wid', None)
        if not wid or wid not in WORKSHOPS:
            await edit_or_send(update, context, "❌ Цех не найден.",
                [[InlineKeyboardButton("🔙 К цехам", callback_data='admin_inventory')]])
            return
        sid = _inv_new_id('s')
        WORKSHOP_SECTIONS.setdefault(wid, {})[sid] = name
        SECTION_EQUIPMENT[(wid, sid)] = []
        save_inventory()
        wname = WORKSHOPS.get(wid, wid)
        await edit_or_send(update, context,
            f"✅ <b>Раздел «{name}» добавлен в «{wname}»!</b>",
            [
                [InlineKeyboardButton("📂 Открыть раздел", callback_data=f'inv_s_{wid}|{sid}')],
                [InlineKeyboardButton("🔙 К цеху",          callback_data=f'inv_w_{wid}')]
            ]
        )

    elif state == 'add_equipment':
        wid = context.user_data.pop('inv_wid', None)
        sid = context.user_data.pop('inv_sid', None)
        if not wid or not sid:
            await edit_or_send(update, context, "❌ Раздел не найден.",
                [[InlineKeyboardButton("🔙 К цехам", callback_data='admin_inventory')]])
            return
        SECTION_EQUIPMENT.setdefault((wid, sid), []).append(name)
        save_inventory()
        sname = WORKSHOP_SECTIONS.get(wid, {}).get(sid, sid)
        total = len(SECTION_EQUIPMENT.get((wid, sid), []))
        await edit_or_send(update, context,
            f"✅ <b>«{name}» добавлено!</b>\n📂 {sname} — теперь {total} ед. оборудования",
            [
                [InlineKeyboardButton("➕ Добавить ещё",   callback_data=f'inv_ae_{wid}|{sid}')],
                [InlineKeyboardButton("📂 Открыть раздел", callback_data=f'inv_s_{wid}|{sid}')],
                [InlineKeyboardButton("🔙 К цеху",          callback_data=f'inv_w_{wid}')]
            ]
        )

    else:
        await edit_or_send(update, context, "⚠️ Неизвестное действие.",
            [[InlineKeyboardButton("🔙 К цехам", callback_data='admin_inventory')]])

# ============================================================================
# УТРЕННЯЯ РАССЫЛКА РЕЙТИНГА — мотивация слесарей
# ============================================================================

async def morning_rating_job(context: ContextTypes.DEFAULT_TYPE):
    """Каждое утро шлёт КАЖДОМУ слесарю его персональный рейтинг: топ-3 + своё место + догонялка."""
    _roll_week_if_needed()
    # если очков за неделю ни у кого нет — не спамим пустым рейтингом
    if not any(mm['week']['points'] for mm in SCORES.get('mechanics', {}).values()):
        return
    users = load_users()
    mechanics = [int(uid) for uid, info in users.items() if info.get('role') == 'mechanic']
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏆 Открыть рейтинг", callback_data='mechanic_rating')]])
    sent = 0
    for uid in mechanics:
        try:
            text = "☀️ <b>Доброе утро!</b> Новый день — новые очки 💪\n\n" + rating_board_text(uid)
            await context.bot.send_message(chat_id=uid, text=text, parse_mode='HTML', reply_markup=kb)
            sent += 1
        except Exception as e:
            logger.error(f"Утренний рейтинг {uid}: {e}")
    logger.info(f"Утренний рейтинг разослан: {sent} слесарям")

# ============================================================================
# ИТОГИ НЕДЕЛИ — объявление победителя + обновление лидерборда
# ============================================================================

async def weekly_winner_job(context: ContextTypes.DEFAULT_TYPE):
    """Запускается ежедневно утром, но объявляет победителя лишь ОДИН раз —
    когда появились неозвученные итоги прошлой недели (т.е. по факту в понедельник).
    Самозащита флагом 'announced' переживает перезапуск и простой бота."""
    _roll_week_if_needed()
    lw = SCORES.get("last_week")
    if lw and not lw.get("announced"):
        ranking = [(n, p) for (n, p) in lw.get("ranking", []) if p != 0]
        if ranking:
            medals = ["🥇", "🥈", "🥉"]
            lines = [f"🏆 <b>ИТОГИ НЕДЕЛИ</b> ({lw['week_id']})\n"]
            for i, (name, pts) in enumerate(ranking[:3]):
                lines.append(f"{medals[i]} {name} — {pts} очк.")
            lines.append(f"\n🎉 Поздравляем, <b>{ranking[0][0]}</b>! "
                         f"Новая неделя — счёт с нуля, у всех есть шанс. Погнали! 💪")
            text = "\n".join(lines)
            for uid in get_all_mechanics():
                try:
                    await context.bot.send_message(chat_id=uid, text=text, parse_mode='HTML')
                except Exception as e:
                    logger.error(f"Итоги недели, ошибка отправки {uid}: {e}")
            logger.info(f"Итоги недели {lw['week_id']} объявлены, победитель: {ranking[0][0]}")
        lw["announced"] = True
        save_scores()
    rebuild_rating_board()

# ============================================================================
# АВТООЧИСТКА — раз в 30 дней удаляет выполненные заявки старше 90 дней
# ============================================================================

async def auto_cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    """Ежедневно в 03:00 проверяет нужна ли очистка (раз в 30 дней)."""
    today = datetime.now().date()

    # Проверяем когда последний раз чистили
    last_cleanup = None
    if os.path.exists(LAST_CLEANUP_FILE):
        try:
            with open(LAST_CLEANUP_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            last_cleanup = datetime.strptime(data.get('date', ''), '%d.%m.%Y').date()
        except Exception:
            pass

    if last_cleanup and (today - last_cleanup).days < 30:
        return  # ещё не время

    # Удаляем выполненные заявки старше 90 дней
    cutoff = datetime.now() - timedelta(days=90)
    to_delete = []
    for req_id, r in REQUESTS.items():
        if r['status'] == 'done':
            try:
                done_t = datetime.strptime(r.get('done_time', r['timestamp']), '%d.%m.%Y %H:%M')
                if done_t < cutoff:
                    to_delete.append(req_id)
            except Exception:
                pass

    if to_delete:
        for req_id in to_delete:
            del REQUESTS[req_id]
        save_requests()
        logger.info(f"Автоочистка: удалено {len(to_delete)} заявок старше 90 дней")
        await notify_admins(
            context.bot,
            f"🗑 <b>Автоочистка завершена</b>\n\n"
            f"Удалено из бота: <b>{len(to_delete)}</b> выполненных заявок старше 90 дней.\n"
            f"В Google Sheets они сохранены.\n"
            f"Осталось в боте: <b>{len(REQUESTS)}</b>"
        )
    else:
        logger.info("Автоочистка: нечего удалять")

    # Сохраняем дату последней очистки
    try:
        with open(LAST_CLEANUP_FILE, 'w', encoding='utf-8') as f:
            json.dump({'date': today.strftime('%d.%m.%Y')}, f)
    except Exception as e:
        logger.error(f"Ошибка сохранения даты очистки: {e}")

# --- Месячная архивация листов цехов ---

async def monthly_archive_job(context: ContextTypes.DEFAULT_TYPE):
    """В 1-й день месяца архивирует листы цехов за ПРОШЛЫЙ месяц."""
    now = datetime.now()
    if now.day != 1:
        return
    prev_month_last_day = now - timedelta(days=1)  # последний день прошлого месяца
    archived, label = archive_workshops(prev_month_last_day)
    if not archived:
        return
    admins = [int(uid) for uid, info in load_users().items() if info.get('role') == 'admin']
    for uid in admins:
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=(f"🗄 <b>Месячная архивация выполнена</b>\n\n"
                      f"Листы цехов за <b>{label}</b> заархивированы, созданы свежие.\n"
                      f"Цехов: {len(archived)}"),
                parse_mode='HTML')
        except Exception as e:
            logger.error(f"Не удалось уведомить админа {uid} об архивации: {e}")

async def admin_archive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение принудительной архивации."""
    query = update.callback_query
    await query.answer()
    label = f"{MONTHS_RU[datetime.now().month]} {datetime.now().year}"
    keyboard = [
        [InlineKeyboardButton("✅ Да, архивировать", callback_data='archive_confirm')],
        [InlineKeyboardButton("🔙 Назад",            callback_data='role_admin')],
    ]
    await query.edit_message_text(
        f"🗄 <b>Архивация листов цехов</b>\n\n"
        f"Текущие листы цехов будут переименованы в «… ({label})», "
        f"а вместо них создадутся свежие пустые. Открытые заявки перенесутся в новые листы.\n\n"
        f"⚠️ Обычно это происходит само 1-го числа. Выполнить сейчас принудительно?",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def admin_archive_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Архивирую…")
    try:
        archived, label = archive_workshops(datetime.now())
        if archived:
            txt = (f"✅ <b>Архивация выполнена ({label})</b>\n\n"
                   f"Заархивировано листов: {len(archived)}\n" +
                   "\n".join(f"• {n}" for n in archived))
        else:
            txt = "ℹ️ Нечего архивировать — листы не найдены или уже заархивированы за этот месяц."
    except Exception as e:
        logger.error(f"Ошибка принудительной архивации: {e}", exc_info=True)
        txt = "❌ Ошибка архивации. Проверь логи / доступ к Google Sheets."
    await query.edit_message_text(txt, parse_mode='HTML')
    text, markup = build_role_menu(update.effective_user.id, 'admin')
    await context.bot.send_message(
        chat_id=update.effective_chat.id, text=text, reply_markup=markup, parse_mode='HTML')

# ============================================================================
# ЗАПУСК
# ============================================================================

def main():
    # Очищенная копия: сначала настройте отдельного тестового бота и таблицу.
    if not BOT_TOKEN or ADMIN_ID <= 0 or not SPREADSHEET_NAME:
        raise SystemExit(
            "Настройте BOT_TOKEN, ADMIN_ID и SPREADSHEET_NAME через переменные окружения. См. README.md."
        )
    if not os.path.isfile(SA_FILE):
        raise SystemExit("Укажите новый ключ Google через GOOGLE_SERVICE_ACCOUNT_FILE. См. README.md.")

    load_requests()
    load_other_tasks()
    load_daily_state()
    load_inventory()
    load_scores()
    load_ppr_paused()
    load_brig_shops()

    app = Application.builder().token(BOT_TOKEN).defaults(Defaults(tzinfo=TIMEZONE)).build()

    # Глобальная защита от двойных нажатий — раньше всех остальных обработчиков
    app.add_handler(CallbackQueryHandler(debounce_callbacks), group=-1)

    # Команды
    app.add_handler(CommandHandler("start",      start))
    app.add_handler(CommandHandler("menu",       cmd_menu))
    app.add_handler(CommandHandler("testsheets", cmd_testsheets))
    app.add_handler(CommandHandler("testppr",    cmd_testppr))
    app.add_handler(CommandHandler("adduser",    cmd_adduser))
    app.add_handler(CommandHandler("removeuser", cmd_removeuser))
    app.add_handler(CommandHandler("rename",     cmd_rename))
    app.add_handler(CommandHandler("find",       cmd_find))
    app.add_handler(CommandHandler("backup",     cmd_backup))
    app.add_handler(CommandHandler("users",      cmd_users))
    app.add_handler(CommandHandler("myid",       cmd_myid))

    # Главное меню
    app.add_handler(CallbackQueryHandler(operator_menu,  pattern="^role_operator$"))
    app.add_handler(CallbackQueryHandler(mechanic_menu,  pattern="^role_mechanic$"))
    app.add_handler(CallbackQueryHandler(admin_menu,     pattern="^role_admin$"))
    app.add_handler(CallbackQueryHandler(brigadir_menu,  pattern="^role_brigadir$"))
    app.add_handler(CallbackQueryHandler(brig_my_shops,   pattern="^brig_shops$"))
    app.add_handler(CallbackQueryHandler(brig_shop_toggle, pattern="^brigshop_"))
    app.add_handler(CallbackQueryHandler(ppr_check_today,   pattern="^pprcheck$"))
    app.add_handler(CallbackQueryHandler(ppr_check_verify,  pattern="^pprv_"))
    app.add_handler(CallbackQueryHandler(ppr_check_verdict, pattern="^pprd_"))
    app.add_handler(CallbackQueryHandler(back_to_main,   pattern="^back_to_main$"))

    # Оператор
    app.add_handler(CallbackQueryHandler(operator_new_request, pattern="^operator_new_request$"))
    app.add_handler(CallbackQueryHandler(operator_my_requests, pattern="^operator_my_requests$"))
    app.add_handler(CallbackQueryHandler(operator_my_period,   pattern="^op_period_"))
    app.add_handler(CallbackQueryHandler(workshop_selected,    pattern="^workshop_"))
    app.add_handler(CallbackQueryHandler(section_selected,     pattern="^section_"))
    app.add_handler(CallbackQueryHandler(equipment_other,      pattern="^equip_other$"))
    app.add_handler(CallbackQueryHandler(equipment_selected,   pattern="^equip_\\d+$"))
    app.add_handler(CallbackQueryHandler(urgency_selected,     pattern="^urgency_"))
    app.add_handler(CallbackQueryHandler(operator_skip_comment,    pattern="^skip_op_comment$"))
    app.add_handler(CallbackQueryHandler(operator_skip_photo,      pattern="^skip_photo$"))
    app.add_handler(CallbackQueryHandler(operator_cancel_request,  pattern="^op_cancel_"))
    app.add_handler(CallbackQueryHandler(operator_rate,            pattern="^rate_"))
    app.add_handler(CallbackQueryHandler(admin_grant_access,      pattern="^acc_grant_"))
    app.add_handler(CallbackQueryHandler(admin_deny_access,       pattern="^acc_deny_"))

    # Слесарь
    app.add_handler(CallbackQueryHandler(mechanic_checkin,         pattern="^checkin_today$"))
    app.add_handler(CallbackQueryHandler(mechanic_checkin_do,      pattern="^checkin_(day|sutki)$"))
    app.add_handler(CallbackQueryHandler(mechanic_rating_board,    pattern="^mechanic_rating$"))
    app.add_handler(CallbackQueryHandler(mechanic_new_requests,    pattern="^mechanic_new_requests$"))
    app.add_handler(CallbackQueryHandler(mechanic_in_progress,     pattern="^mechanic_in_progress$"))
    app.add_handler(CallbackQueryHandler(mechanic_postponed,       pattern="^mechanic_postponed$"))
    app.add_handler(CallbackQueryHandler(mechanic_completed,       pattern="^mechanic_completed$"))
    app.add_handler(CallbackQueryHandler(mechanic_done_period,     pattern="^mech_done_"))
    app.add_handler(CallbackQueryHandler(mechanic_accept_request,  pattern="^accept_"))
    app.add_handler(CallbackQueryHandler(mechanic_eta_selected,    pattern="^eta_"))
    app.add_handler(CallbackQueryHandler(mechanic_done_ask_comment,pattern="^done_"))
    app.add_handler(CallbackQueryHandler(mechanic_postpone_request,pattern="^postpone_"))
    app.add_handler(CallbackQueryHandler(mechanic_done_skip_photo, pattern="^skip_done_photo$"))

    # Админ
    app.add_handler(CallbackQueryHandler(admin_stats,        pattern="^admin_stats$"))
    app.add_handler(CallbackQueryHandler(admin_analytics,    pattern="^admin_analytics$"))
    app.add_handler(CallbackQueryHandler(admin_charts,       pattern="^admin_charts$"))
    app.add_handler(CallbackQueryHandler(admin_leaderboard,  pattern="^lb_(month|all)$"))
    app.add_handler(CallbackQueryHandler(admin_archive,         pattern="^admin_archive$"))
    app.add_handler(CallbackQueryHandler(admin_archive_confirm, pattern="^archive_confirm$"))
    app.add_handler(CallbackQueryHandler(admin_mech_list,    pattern="^admin_mech_list$"))
    app.add_handler(CallbackQueryHandler(admin_mech_card,    pattern="^mech_card_"))
    app.add_handler(CallbackQueryHandler(admin_premium_export, pattern="^prem_(month|all)$"))
    app.add_handler(CallbackQueryHandler(admin_export,        pattern="^admin_export$"))
    app.add_handler(CallbackQueryHandler(admin_export_period, pattern="^exp_req_"))
    app.add_handler(CallbackQueryHandler(admin_all_requests,        pattern="^admin_all_requests$"))
    app.add_handler(CallbackQueryHandler(admin_all_requests_period, pattern="^adm_req_"))
    app.add_handler(CallbackQueryHandler(admin_users_list,   pattern="^admin_users_list$"))
    app.add_handler(CallbackQueryHandler(admin_clear_menu,   pattern="^admin_clear_menu$"))
    app.add_handler(CallbackQueryHandler(admin_clear_older,  pattern="^clear_older_"))
    app.add_handler(CallbackQueryHandler(admin_clear_done,   pattern="^clear_done$"))
    app.add_handler(CallbackQueryHandler(admin_clear_all,    pattern="^clear_all$"))
    app.add_handler(CallbackQueryHandler(admin_clear_by_date,pattern="^clear_by_date$"))
    app.add_handler(CallbackQueryHandler(admin_clear_confirm,   pattern="^clear_confirm$"))
    app.add_handler(CallbackQueryHandler(admin_broadcast,        pattern="^admin_broadcast$"))
    app.add_handler(CallbackQueryHandler(admin_broadcast_target, pattern="^broadcast_target_"))
    app.add_handler(CallbackQueryHandler(admin_ppr,              pattern="^admin_ppr$"))
    app.add_handler(CallbackQueryHandler(admin_ppr_view_days,    pattern="^ppr_view_days$"))
    app.add_handler(CallbackQueryHandler(admin_ppr_day,          pattern="^ppr_day_"))
    app.add_handler(CallbackQueryHandler(admin_ppr_task,         pattern="^ppr_task_"))
    app.add_handler(CallbackQueryHandler(admin_ppr_add,          pattern="^ppr_add$"))
    app.add_handler(CallbackQueryHandler(admin_ppr_new_workshop, pattern="^ppr_nw_"))
    app.add_handler(CallbackQueryHandler(admin_ppr_new_day,      pattern="^ppr_nd_"))
    app.add_handler(CallbackQueryHandler(admin_ppr_new_save,     pattern="^ppr_nsave$"))
    app.add_handler(CallbackQueryHandler(admin_ppr_edit,         pattern="^ppr_edit_"))
    app.add_handler(CallbackQueryHandler(admin_ppr_edit_field,   pattern="^ppr_ef_"))
    app.add_handler(CallbackQueryHandler(admin_ppr_delete,       pattern="^ppr_del_"))
    app.add_handler(CallbackQueryHandler(admin_ppr_send_now,     pattern="^ppr_send_now$"))
    app.add_handler(CallbackQueryHandler(admin_ppr_pause,        pattern="^ppr_pause$"))
    app.add_handler(CallbackQueryHandler(admin_ppr_pause_toggle, pattern="^pprmute_"))

    # Прочие задачи — Админ и Механик
    app.add_handler(CallbackQueryHandler(admin_other_tasks,    pattern="^admin_other_tasks$"))
    app.add_handler(CallbackQueryHandler(mechanic_other_tasks, pattern="^mechanic_other_tasks$"))
    app.add_handler(CallbackQueryHandler(admin_ot_skip_desc,   pattern="^ot_skip_desc$"))
    app.add_handler(CallbackQueryHandler(ot_solo,              pattern="^ot_solo$"))
    app.add_handler(CallbackQueryHandler(ot_with_partner,      pattern="^ot_with_partner$"))

    # Управление имуществом — Админ
    app.add_handler(CallbackQueryHandler(admin_inventory,                pattern="^admin_inventory$"))
    app.add_handler(CallbackQueryHandler(admin_inv_add_workshop_prompt,  pattern="^inv_aw$"))
    app.add_handler(CallbackQueryHandler(admin_inv_del_workshop_execute, pattern="^inv_xwc_"))
    app.add_handler(CallbackQueryHandler(admin_inv_del_workshop_confirm, pattern="^inv_xw_"))
    app.add_handler(CallbackQueryHandler(admin_inv_del_section_execute,  pattern="^inv_xsc_"))
    app.add_handler(CallbackQueryHandler(admin_inv_del_section_confirm,  pattern="^inv_xs_"))
    app.add_handler(CallbackQueryHandler(admin_inv_del_equipment_execute, pattern="^inv_xec_"))
    app.add_handler(CallbackQueryHandler(admin_inv_del_equipment_confirm, pattern="^inv_xep_"))
    app.add_handler(CallbackQueryHandler(admin_inv_add_section_prompt,   pattern="^inv_as_"))
    app.add_handler(CallbackQueryHandler(admin_inv_add_equipment_prompt, pattern="^inv_ae_"))
    app.add_handler(CallbackQueryHandler(admin_inv_section,              pattern="^inv_s_"))
    app.add_handler(CallbackQueryHandler(admin_inv_workshop,             pattern="^inv_w_"))

    # ППР подтверждение / откладывание
    app.add_handler(CallbackQueryHandler(ppr_confirm,         pattern="^pprc_"))
    app.add_handler(CallbackQueryHandler(ppr_confirm_solo,    pattern="^pprsolo_"))
    app.add_handler(CallbackQueryHandler(ppr_confirm_with,    pattern="^pprwith_"))
    app.add_handler(CallbackQueryHandler(ppr_confirm_partner, pattern="^pprpart_"))
    app.add_handler(CallbackQueryHandler(ppr_postpone_ask,    pattern="^pprp_"))

    # Сообщения
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))

    # ППР jobs
    if app.job_queue is not None:
        # Утренняя сводка ППР
        ppr_hour, ppr_min = map(int, PPR_NOTIFY_TIME.split(':'))
        app.job_queue.run_daily(
            daily_ppr_job,
            time=dtime(hour=ppr_hour, minute=ppr_min, second=0),
            name='daily_ppr'
        )
        # Полночная перепланировка точных уведомлений
        app.job_queue.run_daily(
            ppr_midnight_setup,
            time=dtime(hour=0, minute=5, second=0),
            name='ppr_midnight_setup'
        )
        # Запланировать уведомления на сегодня прямо сейчас (при старте бота)
        schedule_ppr_today(app.job_queue)
        # Разослать всем участникам их меню при запуске бота
        app.job_queue.run_once(send_startup_menus, when=3, name='startup_menus')
        # Возобновить напоминания по заявкам, которые остались непринятыми (после перезапуска)
        resumed = 0
        for _rid, _r in REQUESTS.items():
            if _r.get('status') == 'new':
                arm_overdue_reminder(app.job_queue, _rid)
                resumed += 1
        if resumed:
            logger.info(f"⏰ Возобновлены напоминания по {resumed} непринятым заявкам")
        # Catch-up: если бот запустился после PPR_NOTIFY_TIME — отправить сводку сразу
        now_boot = datetime.now()
        ppr_notify_dt = now_boot.replace(hour=ppr_hour, minute=ppr_min, second=0, microsecond=0)
        if now_boot > ppr_notify_dt:
            app.job_queue.run_once(ppr_startup_digest, when=10, name='ppr_startup_catchup')
        # Итоговый отчёт дня в 22:00
        app.job_queue.run_daily(
            daily_report_job_evening,
            time=dtime(hour=22, minute=0, second=0),
            name='daily_report_evening'
        )
        # Утренняя рассылка рейтинга слесарям (мотивация)
        rt_h, rt_m = map(int, RATING_MORNING_TIME.split(':'))
        app.job_queue.run_daily(
            morning_rating_job,
            time=dtime(hour=rt_h, minute=rt_m, second=0),
            name='morning_rating'
        )
        # Итоги недели: ежедневно 08:30 (по факту объявит раз в неделю, в понедельник)
        app.job_queue.run_daily(
            weekly_winner_job,
            time=dtime(hour=8, minute=30, second=0),
            name='weekly_winner'
        )
        # При старте: обновить лидерборд и, если пропустили понедельник, объявить итоги
        app.job_queue.run_once(weekly_winner_job, when=15, name='weekly_winner_startup')
        # Автоочистка старых заявок в 03:00 (срабатывает раз в 30 дней)
        app.job_queue.run_daily(
            auto_cleanup_job,
            time=dtime(hour=3, minute=0, second=0),
            name='auto_cleanup'
        )
        # Ежедневный бэкап данных админам в 03:30
        app.job_queue.run_daily(
            daily_backup_job,
            time=dtime(hour=3, minute=30, second=0),
            name='daily_backup'
        )
        # Месячная архивация листов цехов — проверка каждый день в 00:30 (сработает 1-го числа)
        app.job_queue.run_daily(
            monthly_archive_job,
            time=dtime(hour=0, minute=30, second=0),
            name='monthly_archive'
        )
        logger.info(f"ППР-рассылка: сводка в {PPR_NOTIFY_TIME}, точные уведомления запланированы")
    else:
        logger.warning("=" * 60)
        logger.warning("⚠️ JobQueue НЕ ДОСТУПЕН! Не работают: напоминания о непринятых "
                       "заявках, ППР-рассылка, вечерний отчёт, автоочистка.")
        logger.warning("   Исправь: pip install 'python-telegram-bot[job-queue]'")
        logger.warning("=" * 60)
        print("⚠️  ВНИМАНИЕ: JobQueue не установлен — напоминания и ППР не работают!")
        print("    Запусти: pip install 'python-telegram-bot[job-queue]'")

    logger.info("🤖 Бот запущен!")
    print("=" * 60)
    print("✅ БОТ РАБОТАЕТ!")
    print("=" * 60)
    print("📋 Команды админа:")
    print("   /adduser [ID] [роль]  — добавить пользователя")
    print("   /removeuser [ID]      — удалить пользователя")
    print("   /rename [ID] [имя]    — переименовать пользователя")
    print("   /find [текст]         — поиск заявки")
    print("   /backup               — прислать бэкап данных")
    print("   /users                — список всех")
    print("   /myid                 — узнать свой ID")
    print("   /testppr              — тест ППР уведомлений")
    print("   /testsheets           — диагностика Google Sheets")
    print("=" * 60)

    async def error_handler(update, context):
        import telegram
        if isinstance(context.error, telegram.error.NetworkError):
            logger.warning(f"Сетевая ошибка (авто-переподключение): {context.error}")
        else:
            logger.error(f"Ошибка: {context.error}", exc_info=context.error)

    app.add_error_handler(error_handler)
    app.run_polling()

if __name__ == '__main__':
    main()
