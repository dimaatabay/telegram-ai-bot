import os
import logging
import json
import re
import asyncio
import tempfile
from datetime import datetime

from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MANAGER_TELEGRAM_ID = os.getenv("MANAGER_TELEGRAM_ID")
GOOGLE_SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
GOOGLE_CREDENTIALS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "google_credentials.json"
)
SPREADSHEET_ID = "1NrEOoPLJu9hSNKED2qgpbifwnTQbYqpMGJ-RAWE3s6E"
CARS_SHEET_RANGE = "'Автомобили'!A2:H"
NEW_RENTAL_STATUS = "Новый"
RENTAL_STATUS_BY_ACTION = {
    "working": "В работе",
    "completed": "Завершён",
    "cancelled": "Отменён",
}
FINAL_RENTAL_STATUSES = {
    RENTAL_STATUS_BY_ACTION["completed"],
    RENTAL_STATUS_BY_ACTION["cancelled"],
}

OPENAI_TIMEOUT_SECONDS = 25
OPENAI_MAX_ATTEMPTS = 2
OPENAI_RETRY_DELAY_SECONDS = 1
client = OpenAI(
    api_key=OPENAI_API_KEY,
    timeout=OPENAI_TIMEOUT_SECONDS,
    max_retries=0,
)
sheets_service = None
MAX_HISTORY_MESSAGES = 10
HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.json")
STIX_WELCOME_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "assets", "stix_welcome.png"
)
BUSINESS_CONTEXT = """
Компания
SIXT — международная компания в сфере мобильности и аренды транспорта.

Компания основана в 1912 году.

SIXT работает более чем в 100 странах и имеет около 2200 пунктов обслуживания.

Основные услуги:
- краткосрочная аренда автомобилей;
- аренда фургонов и коммерческого транспорта;
- каршеринг;
- автомобильная подписка и долгосрочная аренда.

Аренда автомобилей

Клиент выбирает категорию автомобиля, место и период аренды.

Конкретная модель автомобиля может зависеть от наличия в выбранном пункте.

В стоимость аренды обычно входят:
- выбранная категория автомобиля;
- выбранный пакет пробега или безлимитный пробег;
- страхование ответственности перед третьими лицами;
- применимые налоги;
- круглосуточная помощь SIXT на дороге при технической неисправности.

Отдельно могут оплачиваться:
- топливо;
- дополнительный водитель;
- детское кресло;
- GPS;
- дополнительные пакеты защиты;
- дополнительный пробег;
- некоторые локальные сборы.

Стандартная топливная политика — full-to-full:
клиент получает автомобиль с полным баком и должен вернуть его с полным баком.

Документы

Для аренды требуется действующее водительское удостоверение.

В некоторых странах может потребоваться международное водительское удостоверение.

Конкретные требования зависят от страны, пункта аренды и категории автомобиля.

Оплата и депозит

При получении автомобиля может блокироваться депозит.

Размер депозита зависит от категории автомобиля, стоимости аренды и места получения.

Точные цены, наличие автомобилей и размер депозита необходимо проверять при конкретном бронировании.
""".strip()
SYSTEM_INSTRUCTIONS = """
Ты — консультант SIXT. Отвечай только на вопросы, связанные с SIXT, арендой
автомобилей, услугами компании, условиями аренды, оплатой и депозитом,
документами, дополнительными услугами и контекстом компании ниже.

Используй BUSINESS_CONTEXT как источник информации о компании. Не придумывай
цены, наличие автомобилей, размер депозита или условия конкретного пункта
аренды. Если точной информации нет, прямо скажи, что её нужно уточнить при
бронировании или у SIXT.

Если вопрос не относится к разрешённым темам, ответь ровно так:
"Я могу помочь только с вопросами об услугах и аренде автомобилей SIXT."

BUSINESS_CONTEXT:
{business_context}
""".strip().format(business_context=BUSINESS_CONTEXT)
RENTAL_BUTTON_TEXT = "Оформить аренду"
CONFIRM_RENTAL_BUTTON_TEXT = "✅ Подтвердить"
CANCEL_RENTAL_BUTTON_TEXT = "❌ Отменить"
CLIENT_CANCEL_RENTAL_BUTTON_TEXT = "❌ Отменить мою заявку"
CONFIRM_CLIENT_CANCELLATION_BUTTON_TEXT = "✅ Да, отменить"
DECLINE_CLIENT_CANCELLATION_BUTTON_TEXT = "↩️ Нет, оставить"
RENTAL_STEPS = (
    ("city", "Укажите город получения автомобиля."),
    ("car_type", "Выберите доступный автомобиль кнопкой ниже."),
    ("start_date", "Укажите дату начала аренды."),
    ("end_date", "Укажите дату окончания аренды."),
    ("name", "Укажите ваше имя."),
    ("phone", "Укажите номер телефона."),
)
# Temporary rental data is intentionally kept outside OpenAI histories and disk storage.
rental_requests = {}

START_BUTTON_TEXT = "Начать работу"
CLEAR_BUTTON_TEXT = "Очистить историю"
CHAT_KEYBOARD = ReplyKeyboardMarkup(
    [
        [START_BUTTON_TEXT, CLEAR_BUTTON_TEXT],
        [RENTAL_BUTTON_TEXT, CLIENT_CANCEL_RENTAL_BUTTON_TEXT],
    ],
    resize_keyboard=True,
    is_persistent=True,
)
WELCOME_TEXT = (
    "\\u0414\\u043e\\u0431\\u0440\\u043e \\u043f\\u043e\\u0436\\u0430\\u043b\\u043e\\u0432\\u0430\\u0442\\u044c \\u0432 STIX \\U0001F44B\\n\\n"
    "\\u0412\\u044b\\u0431\\u0435\\u0440\\u0438\\u0442\\u0435 \\u043d\\u0443\\u0436\\u043d\\u043e\\u0435 \\u0434\\u0435\\u0439\\u0441\\u0442\\u0432\\u0438\\u0435 \\u043d\\u0438\\u0436\\u0435 \\u0438\\u043b\\u0438 \\u043f\\u0440\\u043e\\u0441\\u0442\\u043e \\u043d\\u0430\\u043f\\u0438\\u0448\\u0438\\u0442\\u0435 \\u0441\\u0432\\u043e\\u0439 \\u0432\\u043e\\u043f\\u0440\\u043e\\u0441."
).encode("ascii").decode("unicode_escape")
HELP_TEXT = (
    "STIX — помощник по аренде автомобилей.\n\n"
    "Доступные команды:\n"
    "/start — начать работу\n"
    "/help — помощь\n\n"
    "Вы также можете просто написать свой вопрос сообщением."
)
RENTAL_CONFIRMATION_KEYBOARD = ReplyKeyboardMarkup(
    [[CONFIRM_RENTAL_BUTTON_TEXT, CANCEL_RENTAL_BUTTON_TEXT]],
    resize_keyboard=True,
    is_persistent=True,
)


def load_user_histories():
    if not os.path.exists(HISTORY_FILE):
        return {}

    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as history_file:
            saved_histories = json.load(history_file)

        if not isinstance(saved_histories, dict):
            raise ValueError("History root must be an object")

        histories = {}
        for saved_user_id, messages in saved_histories.items():
            user_id = int(saved_user_id)
            if not isinstance(messages, list):
                raise ValueError("User history must be a list")

            if not all(
                isinstance(message, dict)
                and isinstance(message.get("role"), str)
                and isinstance(message.get("content"), str)
                for message in messages
            ):
                raise ValueError("Invalid message in history")

            histories[user_id] = messages[-MAX_HISTORY_MESSAGES:]

        return histories
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        logger.exception("Failed to load user history from %s", HISTORY_FILE)
        return {}


def save_user_histories():
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as history_file:
            json.dump(user_histories, history_file, ensure_ascii=False, indent=2)
        return True
    except Exception:
        logger.exception("Failed to save user history to %s", HISTORY_FILE)
        return False


user_histories = load_user_histories()


def trim_user_history(user_id):
    if len(user_histories[user_id]) > MAX_HISTORY_MESSAGES:
        user_histories[user_id] = user_histories[user_id][-MAX_HISTORY_MESSAGES:]


def rental_step_index(rental_request):
    for index, (field, _) in enumerate(RENTAL_STEPS):
        if field not in rental_request["data"]:
            return index
    return None


async def ask_current_rental_step(update: Update, user_id):
    step_index = rental_step_index(rental_requests[user_id])
    if step_index is not None:
        await update.message.reply_text(RENTAL_STEPS[step_index][1])


def parse_rental_date(value):
    for date_format in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            pass
    return None


def get_sheets_service():
    global sheets_service
    if sheets_service is None:
        credentials = Credentials.from_service_account_file(
            GOOGLE_CREDENTIALS_FILE,
            scopes=GOOGLE_SHEETS_SCOPES,
        )
        sheets_service = build("sheets", "v4", credentials=credentials)
    return sheets_service


def get_cars_from_sheet():
    """Return the current car inventory from the dedicated Cars sheet."""
    service = get_sheets_service()
    rows = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=CARS_SHEET_RANGE,
    ).execute().get("values", [])

    cars = []
    for row in rows:
        values = [str(value).strip() for value in row[:8]]
        values.extend([""] * (8 - len(values)))
        if not any(values):
            continue
        cars.append({
            "id": values[0],
            "make": values[1],
            "model": values[2],
            "car_class": values[3],
            "daily_price": values[4],
            "status": values[5],
            "city": values[6],
            "year": values[7],
        })
    return cars


def is_available_car_in_city(car, city):
    """Return whether a Cars-sheet record is free in the selected city."""
    return (
        car["city"].strip().casefold() == city.strip().casefold()
        and car["status"].strip().casefold() == "свободен"
    )


def get_available_cars_for_city(city):
    """Read current inventory and keep only free cars from one city."""
    return [
        car for car in get_cars_from_sheet()
        if is_available_car_in_city(car, city)
    ]


def get_available_cities_from_cars(cars):
    """Return unique, non-empty cities which currently have a free car."""
    cities = []
    seen_cities = set()
    for car in cars:
        city = car["city"].strip()
        city_key = city.casefold()
        if (
            car["status"].strip().casefold() == "свободен"
            and city
            and city_key not in seen_cities
        ):
            seen_cities.add(city_key)
            cities.append(city)
    return cities


def get_available_cities():
    """Read the current Cars sheet and return cities with free cars only."""
    return get_available_cities_from_cars(get_cars_from_sheet())


def car_display_name(car):
    return " ".join(part for part in (car["make"], car["model"]) if part).strip()


def car_button_text(car):
    display_name = car_display_name(car) or "Автомобиль"
    daily_price = car["daily_price"]
    return f"{display_name} — {daily_price} ₸/сутки" if daily_price else display_name


def available_cars_keyboard(cars):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(car_button_text(car), callback_data=f"car:{car['id']}")]
        for car in cars
    ])


def available_cities_keyboard(cities):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(city, callback_data=f"city:{index}")]
        for index, city in enumerate(cities)
    ])


def is_car_inventory_question(text):
    normalized_text = text.casefold()
    inventory_terms = (
        "машин", "автомобил", " авто", "suv", "sedan", "premium",
        "свобод", "марка", "модель", "цена", "стоимость",
    )
    known_makes = (
        "bmw", "toyota", "lamborghini", "mercedes", "audi", "lexus",
        "kia", "hyundai", "volkswagen", "porsche", "tesla", "nissan",
        "mazda", "honda", "ford", "chevrolet", "renault", "geely",
        "chery", "exeed", "land rover", "range rover",
    )
    direct_car_query_prefixes = (
        "есть ", "есть ли ", "имеется ", "найдется ", "сколько стоит ",
    )
    non_inventory_subjects = (
        "депозит", "залог", "страхов", "аренд", "документ", "услуг",
        "доставк", "брон", "оплат", "офис", "пункт",
    )
    return (
        any(term in normalized_text for term in inventory_terms)
        or any(make in normalized_text for make in known_makes)
        or (
            normalized_text.startswith(direct_car_query_prefixes)
            and not any(term in normalized_text for term in non_inventory_subjects)
        )
    )


def build_cars_context(cars):
    if not cars:
        return "В листе «Автомобили» нет записей."

    lines = []
    for car in cars:
        lines.append(
            "ID: {id}; Марка: {make}; Модель: {model}; Класс: {car_class}; "
            "Цена в сутки: {daily_price}; Статус: {status}; Город: {city}; Год: {year}".format(
                **car
            )
        )
    return "\n".join(lines)


def build_car_inventory_instructions(cars):
    return """
Пользователь спрашивает об автомобилях. Ниже приведены актуальные данные листа
«Автомобили» Google Sheets. Это единственный источник фактов для ответа об
автомобилях: не придумывай марку, модель, цену, статус, год, город или наличие.
Автомобиль доступен только при точном статусе «Свободен». Автомобиль со статусом
«Занят» не предлагай как доступный; при прямом вопросе о нём скажи, что сейчас он
недоступен. Если подходящего автомобиля или нужного поля нет в данных, честно
сообщи об этом. Отвечай компактно и естественно на языке пользователя.

ДАННЫЕ ЛИСТА «АВТОМОБИЛИ»:
{cars_context}
""".strip().format(cars_context=build_cars_context(cars))


def get_next_lead_number(values):
    existing_numbers = []
    for row in values:
        if not row:
            continue
        try:
            existing_numbers.append(int(str(row[0]).strip()))
        except (TypeError, ValueError):
            continue
    return max(existing_numbers, default=0) + 1


def append_rental_lead(user_id, rental_data):
    service = get_sheets_service()
    existing_rows = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="A2:A",
    ).execute().get("values", [])
    lead_number = get_next_lead_number(existing_rows)
    lead_row = [[
        lead_number,
        str(user_id),
        rental_data["name"],
        rental_data["car_type"],
        rental_data["start_date"],
        rental_data["end_date"],
        rental_data["phone"],
        NEW_RENTAL_STATUS,
    ]]

    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range="A:H",
        valueInputOption="RAW",
        body={"values": lead_row},
    ).execute()
    return lead_number


def update_rental_lead_status(lead_number, status):
    """Update column H for exactly one existing request number in column A."""
    service = get_sheets_service()
    rows = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="A2:H",
    ).execute().get("values", [])
    matching_rows = [
        row_index
        for row_index, row in enumerate(rows, start=2)
        if row and str(row[0]).strip() == str(lead_number)
    ]

    if len(matching_rows) != 1:
        raise ValueError(
            "Expected exactly one rental request for number %s, found %s"
            % (lead_number, len(matching_rows))
        )

    row_index = matching_rows[0]
    row = rows[row_index - 2]
    current_status = row[7] if len(row) > 7 else ""
    if current_status == status:
        return False

    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"H{row_index}",
        valueInputOption="RAW",
        body={"values": [[status]]},
    ).execute()
    return True


def find_active_rental_lead_for_user(user_id):
    """Return the highest-numbered active rental request owned by a user."""
    service = get_sheets_service()
    rows = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="A2:H",
    ).execute().get("values", [])
    active_leads = []

    for row_index, row in enumerate(rows, start=2):
        if len(row) < 8 or str(row[1]).strip() != str(user_id):
            continue
        try:
            lead_number = int(str(row[0]).strip())
        except (TypeError, ValueError):
            continue
        if lead_number <= 0 or row[7] not in {
            NEW_RENTAL_STATUS,
            RENTAL_STATUS_BY_ACTION["working"],
        }:
            continue
        active_leads.append((lead_number, row_index, row))

    if not active_leads:
        return None

    lead_number, row_index, row = max(active_leads, key=lambda lead: lead[0])
    return {
        "lead_number": lead_number,
        "row_index": row_index,
        "user_id": str(row[1]).strip(),
        "name": row[2],
        "car_type": row[3],
        "start_date": row[4],
        "end_date": row[5],
        "phone": row[6],
        "status": row[7],
    }


def cancel_rental_lead_for_user(user_id, lead_number):
    """Cancel one active lead only after re-checking its owner and status."""
    service = get_sheets_service()
    rows = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="A2:H",
    ).execute().get("values", [])
    matching_rows = [
        (row_index, row)
        for row_index, row in enumerate(rows, start=2)
        if len(row) >= 8
        and str(row[0]).strip() == str(lead_number)
        and str(row[1]).strip() == str(user_id)
    ]

    if len(matching_rows) != 1:
        raise ValueError(
            "Expected exactly one rental request %s for user %s, found %s"
            % (lead_number, user_id, len(matching_rows))
        )

    row_index, row = matching_rows[0]
    if row[7] not in {NEW_RENTAL_STATUS, RENTAL_STATUS_BY_ACTION["working"]}:
        return None

    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"H{row_index}",
        valueInputOption="RAW",
        body={"values": [[RENTAL_STATUS_BY_ACTION["cancelled"]]]},
    ).execute()
    return {
        "lead_number": int(str(row[0]).strip()),
        "name": row[2],
        "car_type": row[3],
        "phone": row[6],
    }


def get_manager_telegram_id():
    if not MANAGER_TELEGRAM_ID:
        logger.error("MANAGER_TELEGRAM_ID is not configured")
        return None

    try:
        manager_telegram_id = int(MANAGER_TELEGRAM_ID)
    except (TypeError, ValueError):
        logger.error("MANAGER_TELEGRAM_ID must be a valid integer")
        return None

    if manager_telegram_id <= 0:
        logger.error("MANAGER_TELEGRAM_ID must be a positive integer")
        return None

    return manager_telegram_id


async def notify_manager_about_rental(bot, lead_number, user_id, rental_data):
    manager_telegram_id = get_manager_telegram_id()
    if manager_telegram_id is None:
        return

    manager_message = (
        "Новая заявка на аренду\n\n"
        f"№: {lead_number}\n"
        f"Имя: {rental_data['name']}\n"
        f"Телефон: {rental_data['phone']}\n"
        f"Автомобиль: {rental_data['car_type']}\n"
        f"Город: {rental_data['city']}\n"
        f"Дата начала: {rental_data['start_date']}\n"
        f"Дата окончания: {rental_data['end_date']}\n"
        f"Статус: {NEW_RENTAL_STATUS}\n"
        f"Telegram ID: {user_id}"
    )

    try:
        await bot.send_message(
            chat_id=manager_telegram_id,
            text=manager_message,
            reply_markup=manager_status_keyboard(lead_number),
        )
    except Exception:
        logger.exception(
            "Failed to notify manager about saved rental request %s for user %s",
            lead_number,
            user_id,
        )


async def notify_manager_about_client_cancellation(bot, rental_lead, user_id):
    manager_telegram_id = get_manager_telegram_id()
    if manager_telegram_id is None:
        return

    manager_message = (
        "Клиент отменил заявку\n\n"
        f"№: {rental_lead['lead_number']}\n"
        f"Имя: {rental_lead['name']}\n"
        f"Телефон: {rental_lead['phone']}\n"
        f"Автомобиль: {rental_lead['car_type']}\n"
        f"Telegram ID: {user_id}\n"
        f"Статус: {RENTAL_STATUS_BY_ACTION['cancelled']}"
    )

    try:
        await bot.send_message(chat_id=manager_telegram_id, text=manager_message)
    except Exception:
        logger.exception(
            "Failed to notify manager about cancellation of rental request %s for user %s",
            rental_lead["lead_number"],
            user_id,
        )


def manager_status_keyboard(lead_number):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "В работу", callback_data=f"status:{lead_number}:working"
        ),
        InlineKeyboardButton(
            "Завершить", callback_data=f"status:{lead_number}:completed"
        ),
        InlineKeyboardButton(
            "Отменить", callback_data=f"status:{lead_number}:cancelled"
        ),
    ]])


def manager_message_with_status(message_text, status):
    return re.sub(
        r"(^\s*Статус:\s*).*?$",
        rf"\g<1>{status}",
        message_text,
        flags=re.MULTILINE,
    )


async def handle_manager_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None or query.data is None:
        return

    manager_telegram_id = get_manager_telegram_id()
    if manager_telegram_id is None or update.effective_user.id != manager_telegram_id:
        logger.warning(
            "Unauthorized rental status callback from Telegram user %s",
            update.effective_user.id,
        )
        await query.answer("Недостаточно прав.", show_alert=True)
        return

    try:
        _, lead_number, action = query.data.split(":", 2)
        status = RENTAL_STATUS_BY_ACTION[action]
        if not lead_number.isdigit() or int(lead_number) <= 0:
            raise ValueError("Invalid rental request number")
    except (KeyError, ValueError):
        logger.warning("Invalid manager rental status callback")
        await query.answer("Не удалось изменить статус заявки.", show_alert=True)
        return

    try:
        await asyncio.to_thread(update_rental_lead_status, lead_number, status)
    except Exception:
        logger.exception("Failed to update rental request %s status", lead_number)
        await query.answer(
            "Не удалось изменить статус заявки. Попробуйте ещё раз.",
            show_alert=True,
        )
        return

    message_text = manager_message_with_status(query.message.text, status)
    reply_markup = None if status in FINAL_RENTAL_STATUSES else manager_status_keyboard(lead_number)
    try:
        await query.edit_message_text(text=message_text, reply_markup=reply_markup)
    except Exception:
        logger.exception("Failed to update manager message for rental request %s", lead_number)
        await query.answer("Статус заявки обновлён.", show_alert=True)
        return

    await query.answer("Статус заявки обновлён.")


def client_cancellation_keyboard(lead_number):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            CONFIRM_CLIENT_CANCELLATION_BUTTON_TEXT,
            callback_data=f"client_cancel:{lead_number}:confirm",
        ),
        InlineKeyboardButton(
            DECLINE_CLIENT_CANCELLATION_BUTTON_TEXT,
            callback_data=f"client_cancel:{lead_number}:decline",
        ),
    ]])


async def clear_client_cancellation_keyboard(query):
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        logger.exception("Failed to remove client cancellation buttons")


async def start_client_rental_cancellation(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    user_id = update.effective_user.id
    try:
        rental_lead = await asyncio.to_thread(find_active_rental_lead_for_user, user_id)
    except Exception:
        logger.exception("Failed to find active rental request for user %s", user_id)
        await update.message.reply_text(
            "Не удалось получить данные заявки. Попробуйте немного позже."
        )
        return

    if rental_lead is None:
        await update.message.reply_text("У вас нет активных заявок для отмены.")
        return

    await update.message.reply_text(
        "Заявка №{lead_number}\n"
        "Автомобиль: {car_type}\n"
        "Дата начала: {start_date}\n"
        "Дата окончания: {end_date}\n"
        "Статус: {status}".format(**rental_lead),
        reply_markup=client_cancellation_keyboard(rental_lead["lead_number"]),
    )


async def handle_client_cancellation_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    if query is None or query.data is None:
        return

    try:
        _, lead_number, action = query.data.split(":", 2)
        if not lead_number.isdigit() or int(lead_number) <= 0:
            raise ValueError("Invalid rental request number")
        if action not in {"confirm", "decline"}:
            raise ValueError("Invalid cancellation action")
    except ValueError:
        logger.warning("Invalid client rental cancellation callback")
        await query.answer("Не удалось обработать отмену заявки.", show_alert=True)
        return

    if action == "decline":
        await query.answer()
        await clear_client_cancellation_keyboard(query)
        await query.message.reply_text("Заявка оставлена без изменений.")
        return

    user_id = update.effective_user.id
    try:
        rental_lead = await asyncio.to_thread(
            cancel_rental_lead_for_user, user_id, lead_number
        )
    except Exception:
        logger.exception(
            "Failed to cancel rental request %s for user %s", lead_number, user_id
        )
        await query.answer()
        await query.message.reply_text(
            "Не удалось отменить заявку. Попробуйте немного позже."
        )
        return

    if rental_lead is None:
        logger.warning(
            "User %s tried to cancel unavailable rental request %s", user_id, lead_number
        )
        await query.answer()
        await clear_client_cancellation_keyboard(query)
        await query.message.reply_text("У вас нет активных заявок для отмены.")
        return

    await query.answer()
    await clear_client_cancellation_keyboard(query)
    await notify_manager_about_client_cancellation(context.bot, rental_lead, user_id)
    await query.message.reply_text("Заявка отменена.", reply_markup=CHAT_KEYBOARD)


def is_rental_intent(text):
    normalized_text = " ".join(text.strip().casefold().split())

    explicit_rental_phrases = (
        # Russian
        "хочу арендовать", "хочу взять", "хочу забронировать",
        "хочу оформить", "хочу начать бронирование", "нужна аренда",
        "нужна машина", "нужен автомобиль", "нужен авто", "нужно авто",
        "нужно забронировать", "давайте оформим", "давайте забронируем",
        "оформите мне аренду", "хочу выбрать машину", "хочу машину на прокат",
        "хочу снять машину", "хочу заказать автомобиль", "дайте машину в аренду",
        "можно арендовать", "можно забронировать", "можно оформить автомобиль",
        "можно взять машину напрокат", "как забронировать", "как оформить аренду",
        "есть возможность арендовать", "прокатная машина",
        # Kazakh
        "жалға алғым келеді", "брондағым келеді", "маған көлік керек",
        "маған машина керек", "машина жалға керек", "жалға алуға бола ма",
        "брондауға бола ма", "қалай брондауға болады", "аренда рәсімдегім келеді",
        "көлікке тапсырыс бергім келеді", "бірнеше күнге машина керек",
        "алматыда машина керек", "ертеңге көлік керек",
        # English
        "i want to rent", "i want to book", "i need a rental car",
        "can i rent", "can i book", "i want to reserve", "i need a car",
        "i want to start a booking", "how can i book", "make a reservation",
    )
    if any(phrase in normalized_text for phrase in explicit_rental_phrases):
        return True

    vehicle_terms = ("машин", "автомобил", " авто", "авто ", "көлік", "car", "vehicle")
    known_makes = ("bmw", "toyota", "mercedes", "audi", "lexus", "kia", "hyundai")
    request_terms = (
        "хочу", "нужн", "мне надо", "можно", "давайте", "оформите", "дайте",
        "жалға", "бронда", "i want", "i need", "can i", "reserve", "booking",
    )
    has_vehicle = (
        any(term in normalized_text for term in vehicle_terms)
        or any(make in normalized_text for make in known_makes)
    )
    return has_vehicle and any(term in normalized_text for term in request_terms)


async def start_rental_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in rental_requests:
        rental_requests[user_id] = {"data": {}}
    rental_request = rental_requests[user_id]
    if rental_step_index(rental_request) == 0:
        await show_available_cities(update.message, rental_request)
    else:
        await ask_current_rental_step(update, user_id)


async def show_available_cities(message, rental_request, cities=None):
    """Show the current city selector without accepting a typed city."""
    try:
        if cities is None:
            cities = await asyncio.to_thread(get_available_cities)
    except Exception:
        logger.exception("Failed to get available rental cities from Google Sheets")
        await message.reply_text(
            "Не удалось получить список доступных городов. Попробуйте немного позже."
        )
        return

    if not cities:
        rental_request.pop("available_cities", None)
        await message.reply_text(
            "К сожалению, сейчас нет доступных автомобилей для аренды."
        )
        return

    rental_request["available_cities"] = cities
    await message.reply_text(
        "Выберите город получения автомобиля:",
        reply_markup=available_cities_keyboard(cities),
    )


async def show_rental_summary(update: Update, user_id):
    data = rental_requests[user_id]["data"]
    await update.message.reply_text(
        "Ваша заявка:\n\n"
        f"Город: {data['city']}\n"
        f"Дата начала: {data['start_date']}\n"
        f"Дата окончания: {data['end_date']}\n"
        f"Автомобиль: {data['car_type']}\n"
        f"Имя: {data['name']}\n"
        f"Телефон: {data['phone']}",
        reply_markup=RENTAL_CONFIRMATION_KEYBOARD,
    )


async def handle_rental_answer(update: Update, user_id, user_text):
    rental_request = rental_requests[user_id]
    step_index = rental_step_index(rental_request)
    if step_index is None:
        await show_rental_summary(update, user_id)
        return

    field, question = RENTAL_STEPS[step_index]
    value = user_text.strip()
    if not value:
        await update.message.reply_text("Ответ не должен быть пустым. " + question)
        return

    if field == "car_type":
        await update.message.reply_text("Пожалуйста, выберите автомобиль кнопкой ниже.")
        return

    if field == "city":
        await update.message.reply_text("Пожалуйста, выберите город кнопкой ниже.")
        await show_available_cities(update.message, rental_request)
        return

    if field == "end_date":
        start_date = parse_rental_date(rental_request["data"].get("start_date", ""))
        end_date = parse_rental_date(value)
        if start_date and end_date and end_date < start_date:
            await update.message.reply_text(
                "Дата окончания не может быть раньше даты начала. " + question
            )
            return

    rental_request["data"][field] = value
    if rental_step_index(rental_request) is None:
        await show_rental_summary(update, user_id)
    else:
        await ask_current_rental_step(update, user_id)


async def handle_city_selection_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    if query is None or query.data is None:
        return

    user_id = update.effective_user.id
    rental_request = rental_requests.get(user_id)
    if (
        rental_request is None
        or rental_step_index(rental_request) != 0
    ):
        await query.answer("Выбор города больше не ожидается.", show_alert=True)
        return

    city_index_text = query.data.removeprefix("city:").strip()
    try:
        city_index = int(city_index_text)
        if city_index < 0:
            raise ValueError("Negative city index")
        city = rental_request["available_cities"][city_index]
    except (KeyError, TypeError, ValueError, IndexError):
        await query.answer("Список городов устарел. Начните выбор заново.", show_alert=True)
        return

    try:
        cars = await asyncio.to_thread(get_cars_from_sheet)
    except Exception:
        logger.exception("Failed to re-check rental city %s", city)
        await query.answer()
        await query.message.reply_text(
            "Не удалось получить список доступных городов. Попробуйте немного позже."
        )
        return

    available_cars = [
        car for car in cars if is_available_car_in_city(car, city)
    ]
    if not available_cars:
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            "К сожалению, сейчас в этом городе нет свободных автомобилей. "
            "Выберите другой город."
        )
        await show_available_cities(
            query.message,
            rental_request,
            get_available_cities_from_cars(cars),
        )
        return

    rental_request["data"]["city"] = city
    rental_request.pop("available_cities", None)
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        "Выберите доступный автомобиль:",
        reply_markup=available_cars_keyboard(available_cars),
    )


async def handle_car_selection_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    if query is None or query.data is None:
        return

    user_id = update.effective_user.id
    rental_request = rental_requests.get(user_id)
    if (
        rental_request is None
        or rental_step_index(rental_request) is None
        or RENTAL_STEPS[rental_step_index(rental_request)][0] != "car_type"
    ):
        await query.answer("Выбор автомобиля больше не ожидается.", show_alert=True)
        return

    car_id = query.data.removeprefix("car:").strip()
    city = rental_request["data"].get("city", "")
    if not car_id or not city:
        await query.answer("Выберите автомобиль из актуального списка.", show_alert=True)
        return

    try:
        cars = await asyncio.to_thread(get_cars_from_sheet)
    except Exception:
        logger.exception("Failed to re-check car %s for rental city %s", car_id, city)
        await query.answer()
        await query.message.reply_text(
            "Не удалось получить актуальный список автомобилей. Попробуйте немного позже."
        )
        return

    matching_cars = [
        car for car in cars
        if car["id"] == car_id and is_available_car_in_city(car, city)
    ]
    if len(matching_cars) != 1:
        available_cars = [
            car for car in cars if is_available_car_in_city(car, city)
        ]
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            "Этот автомобиль уже недоступен. Выберите другой вариант."
        )
        if available_cars:
            await query.message.reply_text(
                "Выберите доступный автомобиль:",
                reply_markup=available_cars_keyboard(available_cars),
            )
        else:
            rental_request["data"].pop("city", None)
            await query.message.reply_text(
                "К сожалению, сейчас в этом городе нет свободных автомобилей. "
                "Выберите другой город."
            )
            await show_available_cities(
                query.message,
                rental_request,
                get_available_cities_from_cars(cars),
            )
        return

    car = matching_cars[0]
    rental_request["data"].update({
        "car_id": car["id"],
        "car_make": car["make"],
        "car_model": car["model"],
        "daily_price": car["daily_price"],
        "car_type": car_display_name(car),
    })
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(RENTAL_STEPS[rental_step_index(rental_request)][1])


async def confirm_rental_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rental_request = rental_requests.get(user_id)
    if rental_request is None or rental_step_index(rental_request) is not None:
        await update.message.reply_text("Нет заявки, ожидающей подтверждения.", reply_markup=CHAT_KEYBOARD)
        return

    if rental_request.get("is_saving"):
        await update.message.reply_text("Заявка уже сохраняется. Пожалуйста, подождите.")
        return

    rental_request["is_saving"] = True
    try:
        lead_number = await asyncio.to_thread(
            append_rental_lead,
            user_id,
            rental_request["data"],
        )
    except Exception:
        logger.exception("Failed to save rental request to Google Sheets for user %s", user_id)
        await update.message.reply_text(
            "Не удалось сохранить заявку. Попробуйте подтвердить ещё раз немного позже.",
            reply_markup=RENTAL_CONFIRMATION_KEYBOARD,
        )
        rental_request["is_saving"] = False
        return

    logger.info("Saved confirmed rental request to Google Sheets for user %s", user_id)
    await notify_manager_about_rental(
        context.bot,
        lead_number,
        user_id,
        rental_request["data"],
    )
    rental_requests.pop(user_id, None)
    await update.message.reply_text("Заявка принята.", reply_markup=CHAT_KEYBOARD)


async def cancel_rental_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rental_requests.pop(user_id, None)
    await update.message.reply_text("Оформление заявки отменено.", reply_markup=CHAT_KEYBOARD)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_welcome_message(update)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def send_welcome_message(update: Update):
    if not os.path.isfile(STIX_WELCOME_FILE):
        logger.error("STIX welcome image file is missing: %s", STIX_WELCOME_FILE)
    else:
        try:
            with open(STIX_WELCOME_FILE, "rb") as stix_welcome:
                await update.message.reply_photo(photo=stix_welcome)
        except Exception:
            logger.exception("Failed to send STIX welcome image")

    await update.message.reply_text(WELCOME_TEXT, reply_markup=CHAT_KEYBOARD)


async def send_greeting(update: Update):
    await update.message.reply_text(
        "Здравствуйте! Я AI-бот. Чем могу помочь?",
        reply_markup=CHAT_KEYBOARD,
    )


async def start_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_welcome_message(update)


def clear_user_history(user_id):
    previous_history = user_histories.pop(user_id, None)

    if save_user_histories():
        return True

    if previous_history is not None:
        user_histories[user_id] = previous_history

    return False


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if clear_user_history(user_id):
        await update.message.reply_text("История диалога очищена.")
        return

    try:
        await update.message.reply_text("Не удалось очистить историю. Попробуйте позже.")
    except Exception:
        logger.exception("Failed to send clear error message to user %s", user_id)


async def clear_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await clear_command(update, context)


def transcribe_voice_file(audio_path):
    with open(audio_path, "rb") as audio_file:
        transcription = client.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe",
            file=audio_file,
        )

    transcription_text = getattr(transcription, "text", transcription)
    if not isinstance(transcription_text, str) or not transcription_text.strip():
        raise ValueError("OpenAI returned an empty voice transcription")
    return transcription_text.strip()


async def run_openai_request(request):
    """Run one synchronous SDK operation off the event loop with one retry."""
    for attempt in range(OPENAI_MAX_ATTEMPTS):
        try:
            return await asyncio.to_thread(request)
        except (
            APITimeoutError,
            APIConnectionError,
            RateLimitError,
            InternalServerError,
        ) as error:
            if attempt == OPENAI_MAX_ATTEMPTS - 1:
                raise

            logger.warning(
                "Retrying OpenAI request after %s (attempt %s of %s)",
                type(error).__name__,
                attempt + 1,
                OPENAI_MAX_ATTEMPTS,
            )
            await asyncio.sleep(OPENAI_RETRY_DELAY_SECONDS)
        except APIStatusError as error:
            if error.status_code < 500 or attempt == OPENAI_MAX_ATTEMPTS - 1:
                raise

            logger.warning(
                "Retrying OpenAI request after HTTP %s (attempt %s of %s)",
                error.status_code,
                attempt + 1,
                OPENAI_MAX_ATTEMPTS,
            )
            await asyncio.sleep(OPENAI_RETRY_DELAY_SECONDS)


async def process_user_text(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text):
    user_id = update.effective_user.id

    if user_id in rental_requests:
        await handle_rental_answer(update, user_id, user_text)
        return

    if is_rental_intent(user_text):
        await start_rental_request(update, context)
        return

    car_inventory_instructions = None
    if is_car_inventory_question(user_text):
        try:
            cars = await asyncio.to_thread(get_cars_from_sheet)
            car_inventory_instructions = build_car_inventory_instructions(cars)
        except Exception:
            logger.exception("Failed to get car inventory from Google Sheets")
            try:
                await update.message.reply_text(
                    "Не удалось получить актуальный список автомобилей. Попробуйте немного позже."
                )
            except Exception:
                logger.exception("Failed to send car inventory error message to user %s", user_id)
            return

    if user_id not in user_histories:
        user_histories[user_id] = []

    user_histories[user_id].append({
        "role": "user",
        "content": user_text
    })
    trim_user_history(user_id)
    save_user_histories()

    try:
        response = await run_openai_request(
            lambda: client.responses.create(
                model="gpt-5-mini",
                input=[
                    {"role": "system", "content": SYSTEM_INSTRUCTIONS},
                    *(
                        [{"role": "system", "content": car_inventory_instructions}]
                        if car_inventory_instructions
                        else []
                    ),
                    *user_histories[user_id],
                ],
            )
        )
        ai_answer = response.output_text
    except Exception:
        logger.exception("OpenAI request failed for user %s", user_id)
        try:
            await update.message.reply_text(
                "Не удалось получить ответ. Попробуйте немного позже."
            )
        except Exception:
            logger.exception("Failed to send OpenAI error message to user %s", user_id)
        return

    user_histories[user_id].append({
        "role": "assistant",
        "content": ai_answer
    })
    trim_user_history(user_id)
    save_user_histories()

    try:
        await update.message.reply_text(ai_answer)
    except Exception:
        logger.exception("Failed to send response to user %s", user_id)


async def answer_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_user_text(update, context, update.message.text)


async def answer_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    temporary_audio_path = None

    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as temporary_audio:
            temporary_audio_path = temporary_audio.name

        try:
            voice_file = await update.message.voice.get_file()
            await voice_file.download_to_drive(custom_path=temporary_audio_path)
        except Exception:
            logger.exception("Failed to download voice message for user %s", user_id)
            await update.message.reply_text(
                "Не удалось скачать голосовое сообщение. Попробуйте ещё раз."
            )
            return

        try:
            user_text = await run_openai_request(
                lambda: transcribe_voice_file(temporary_audio_path)
            )
        except Exception:
            logger.exception("Failed to transcribe voice message for user %s", user_id)
            await update.message.reply_text(
                "Не удалось распознать голосовое сообщение. Попробуйте ещё раз."
            )
            return

        await process_user_text(update, context, user_text)
    finally:
        if temporary_audio_path:
            try:
                os.remove(temporary_audio_path)
            except FileNotFoundError:
                pass
            except OSError:
                logger.exception(
                    "Failed to delete temporary voice file for user %s", user_id
                )


async def set_bot_commands(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "\u041d\u0430\u0447\u0430\u0442\u044c \u0440\u0430\u0431\u043e\u0442\u0443"),
        BotCommand("help", "\u041f\u043e\u043c\u043e\u0449\u044c"),
        BotCommand("clear", "\u041e\u0447\u0438\u0441\u0442\u0438\u0442\u044c \u0438\u0441\u0442\u043e\u0440\u0438\u044e"),
    ])


async def handle_application_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log an unhandled update error and safely show a generic user message."""
    error_type = type(context.error).__name__ if context.error else "UnknownError"
    update_id = getattr(update, "update_id", None)
    user = getattr(update, "effective_user", None)
    user_id = getattr(user, "id", None)

    logger.error(
        "Unhandled Telegram update error: type=%s user_id=%s update_id=%s",
        error_type,
        user_id,
        update_id,
    )

    message = getattr(update, "effective_message", None)
    if message is None:
        return

    try:
        await message.reply_text("Произошла временная ошибка. Попробуйте ещё раз.")
    except Exception as send_error:
        logger.error(
            "Failed to send generic error response: type=%s user_id=%s update_id=%s",
            type(send_error).__name__,
            user_id,
            update_id,
        )


app = Application.builder().token(TELEGRAM_TOKEN).post_init(set_bot_commands).build()

app.add_error_handler(handle_application_error)
app.add_handler(CommandHandler("start", start_command))
app.add_handler(CommandHandler("help", help_command))
app.add_handler(CommandHandler("clear", clear_command))
app.add_handler(CallbackQueryHandler(handle_manager_status_callback, pattern=r"^status:"))
app.add_handler(CallbackQueryHandler(handle_client_cancellation_callback, pattern=r"^client_cancel:"))
app.add_handler(CallbackQueryHandler(handle_city_selection_callback, pattern=r"^city:"))
app.add_handler(CallbackQueryHandler(handle_car_selection_callback, pattern=r"^car:"))
app.add_handler(MessageHandler(filters.Regex("^" + re.escape(START_BUTTON_TEXT) + "$"), start_button))
app.add_handler(MessageHandler(filters.Regex("^" + re.escape(CLEAR_BUTTON_TEXT) + "$"), clear_button))
app.add_handler(MessageHandler(filters.Regex("^" + re.escape(RENTAL_BUTTON_TEXT) + "$"), start_rental_request))
app.add_handler(MessageHandler(filters.Regex("^" + re.escape(CLIENT_CANCEL_RENTAL_BUTTON_TEXT) + "$"), start_client_rental_cancellation))
app.add_handler(MessageHandler(filters.Regex("^" + re.escape(CONFIRM_RENTAL_BUTTON_TEXT) + "$"), confirm_rental_request))
app.add_handler(MessageHandler(filters.Regex("^" + re.escape(CANCEL_RENTAL_BUTTON_TEXT) + "$"), cancel_rental_request))
app.add_handler(MessageHandler(filters.VOICE, answer_voice_message))
app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, answer_message)
)

print("AI Telegram-бот запущен...")

app.run_polling()
