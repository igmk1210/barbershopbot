"""
Бот для барбершопа: приём заявок на стрижку и отправка их владельцу.

Что делает:
  клиент нажимает /start -> выбирает услугу из прайса -> выбирает свободную
  дату (занятые дни помечены красным) -> выбирает свободное время (занятые
  слоты помечены и недоступны для выбора) -> оставляет имя и телефон ->
  заявка уходит владельцу, а слот помечается как занятый. За
  REMINDER_HOURS_BEFORE часов до визита клиенту приходит напоминание.

Владельцу доступны команды:
  /schedule — показать расписание (какие слоты свободны/заняты)
  /cancel ГГГГ-ММ-ДД ЧЧ:ММ — отменить запись и освободить слот

Настройка: переменные окружения BOT_TOKEN и OWNER_CHAT_ID (файл .env).
Расписание, часы работы, прайс-лист и напоминания настраиваются в блоке
"НАСТРОЙКИ" ниже.
"""

import asyncio
import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID")

# ---------------------------------------------------------------------------
# НАСТРОЙКИ — меняйте под конкретный барбершоп
# ---------------------------------------------------------------------------

SHOP_NAME = "Corleone BarberShop"
GREETING = (
    "Здравствуйте! 💈\n\n"
    f"Это бот записи в {SHOP_NAME}.\n\n"
    "Выберите услугу, свободную дату и время — заявка сразу уйдёт мастеру."
)
FINAL_MESSAGE = "Готово! Заявка принята ✅\n\nМы свяжемся с вами для подтверждения записи."

DAYS_AHEAD = 14      # на сколько дней вперёд открыта запись
# График у мастера неровный (обычно 2 рабочих / 1 выходной), но пока нет
# точки отсчёта цикла — временно считаем рабочими все дни. Как появится
# конкретная дата ближайшего рабочего дня, тут можно включить настоящий
# расчёт 2/1 вместо фиксированного выходного.
DAY_OFF = None       # выходной: 0=Пн, 1=Вт ... 6=Вс; None — без выходных
WORK_HOUR_FROM = 10  # запись с 10:00
WORK_HOUR_TO = 20    # запись до 20:00 (не включительно)
SLOT_MINUTES = 60    # длительность одного слота записи

# Прайс-лист: (название услуги, цена в BYN).
# "Отец и сын" указан по базовой цене за одного ребёнка — если детей
# несколько, мастер согласовывает скидку (5 BYN за каждого) с клиентом лично.
SERVICES: list[tuple[str, int]] = [
    ("Мужская стрижка", 30),
    ("Детская стрижка", 30),
    ("Стрижка налысо", 10),
    ("Оформление бороды", 20),
    ("Отец и сын (комплекс)", 55),
    ("Стрижка и оформление бороды", 45),
]
CURRENCY = "BYN"

TIMEZONE = ZoneInfo("Europe/Minsk")
REMINDER_HOURS_BEFORE = 2     # за сколько часов до визита напомнить клиенту
REMINDER_CHECK_SECONDS = 300  # как часто проверять, кому пора напомнить

BOOKINGS_FILE = Path(__file__).parent / "bookings.json"

WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS_RU = [
    "янв", "фев", "мар", "апр", "май", "июн",
    "июл", "авг", "сен", "окт", "ноя", "дек",
]

# ---------------------------------------------------------------------------
# Хранилище записей.
# Формат: { "YYYY-MM-DD": { "HH:MM": {name, phone, service, price, user_id,
#                                     username, reminded} } }
# ---------------------------------------------------------------------------


def load_bookings() -> dict:
    if BOOKINGS_FILE.exists():
        try:
            return json.loads(BOOKINGS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logging.exception("Не удалось прочитать %s, начинаю с пустого списка", BOOKINGS_FILE)
    return {}


def save_bookings() -> None:
    BOOKINGS_FILE.write_text(
        json.dumps(bookings, ensure_ascii=False, indent=2), encoding="utf-8"
    )


bookings: dict = load_bookings()


def time_slots() -> list[str]:
    start = WORK_HOUR_FROM * 60
    end = WORK_HOUR_TO * 60
    return [f"{m // 60:02d}:{m % 60:02d}" for m in range(start, end, SLOT_MINUTES)]


SLOTS = time_slots()
SLOT_COUNT = len(SLOTS)


def available_dates() -> list[date]:
    dates = []
    for i in range(DAYS_AHEAD):
        d = date.today() + timedelta(days=i)
        if DAY_OFF is None or d.weekday() != DAY_OFF:
            dates.append(d)
    return dates


def format_date_label(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day} {MONTHS_RU[d.month - 1]}, {WEEKDAYS_RU[d.weekday()]}"


def services_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{name} — {price} {CURRENCY}", callback_data=f"service:{i}")]
        for i, (name, price) in enumerate(SERVICES)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def dates_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for d in available_dates():
        iso = d.isoformat()
        free = SLOT_COUNT - len(bookings.get(iso, {}))
        label = f"{d.day} {MONTHS_RU[d.month - 1]}, {WEEKDAYS_RU[d.weekday()]}"
        if free <= 0:
            button = InlineKeyboardButton(text=f"🔴 {label}", callback_data="full_date")
        else:
            button = InlineKeyboardButton(text=f"🟢 {label}", callback_data=f"date:{iso}")
        row.append(button)
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="◀️ Назад к услугам", callback_data="back_to_services")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def times_keyboard(iso_date: str) -> InlineKeyboardMarkup:
    day_bookings = bookings.get(iso_date, {})
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for t in SLOTS:
        if t in day_bookings:
            button = InlineKeyboardButton(text=f"⛔ {t}", callback_data="busy_slot")
        else:
            button = InlineKeyboardButton(text=f"✅ {t}", callback_data=f"slot:{iso_date}:{t}")
        row.append(button)
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="◀️ Назад к датам", callback_data="back_to_dates")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить мой номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


class Form(StatesGroup):
    name = State()
    phone = State()


def is_owner(user_id: int) -> bool:
    return bool(OWNER_CHAT_ID) and str(user_id) == str(OWNER_CHAT_ID)


dp = Dispatcher()


@dp.message(Command("start"))
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(GREETING, reply_markup=ReplyKeyboardRemove())
    await message.answer("Выберите услугу:", reply_markup=services_keyboard())


@dp.message(Command("price"))
async def price_list(message: Message) -> None:
    lines = [f"💈 <b>Прайс-лист {SHOP_NAME}</b>\n"]
    lines += [f"• {name} — {price} {CURRENCY}" for name, price in SERVICES]
    await message.answer("\n".join(lines))


@dp.callback_query(F.data == "back_to_services")
async def back_to_services(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Выберите услугу:", reply_markup=services_keyboard())
    await callback.answer()


@dp.callback_query(F.data.startswith("service:"))
async def choose_service(callback: CallbackQuery, state: FSMContext) -> None:
    idx = int(callback.data.split(":", 1)[1])
    if not 0 <= idx < len(SERVICES):
        await callback.answer()
        return
    name, price = SERVICES[idx]
    await state.update_data(service=name, price=price)
    await callback.message.edit_text(
        f"Услуга: {name} — {price} {CURRENCY}\n\nВыберите дату:",
        reply_markup=dates_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "full_date")
async def full_date(callback: CallbackQuery) -> None:
    await callback.answer("На эту дату свободных мест нет 😔", show_alert=True)


@dp.callback_query(F.data == "back_to_dates")
async def back_to_dates(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    service = data.get("service", "—")
    price = data.get("price")
    price_part = f" — {price} {CURRENCY}" if price is not None else ""
    await callback.message.edit_text(
        f"Услуга: {service}{price_part}\n\nВыберите дату:",
        reply_markup=dates_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("date:"))
async def choose_date(callback: CallbackQuery, state: FSMContext) -> None:
    iso = callback.data.split(":", 1)[1]
    await state.update_data(date=iso)
    data = await state.get_data()
    service = data.get("service", "—")
    price = data.get("price")
    price_part = f" — {price} {CURRENCY}" if price is not None else ""
    await callback.message.edit_text(
        f"Услуга: {service}{price_part}\nДата: {format_date_label(iso)}\nВыберите время:",
        reply_markup=times_keyboard(iso),
    )
    await callback.answer()


@dp.callback_query(F.data == "busy_slot")
async def busy_slot(callback: CallbackQuery) -> None:
    await callback.answer("Это время уже занято, выберите другое", show_alert=True)


@dp.callback_query(F.data.startswith("slot:"))
async def choose_slot(callback: CallbackQuery, state: FSMContext) -> None:
    _, iso, t = callback.data.split(":")
    if t in bookings.get(iso, {}):
        await callback.answer("Это время уже заняли, выберите другое", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=times_keyboard(iso))
        return
    await state.update_data(date=iso, time=t)
    data = await state.get_data()
    service = data.get("service", "—")
    price = data.get("price")
    price_part = f" — {price} {CURRENCY}" if price is not None else ""
    await state.set_state(Form.name)
    await callback.message.edit_text(
        f"Услуга: {service}{price_part}\nДата: {format_date_label(iso)}, время: {t}\n\nКак вас зовут?"
    )
    await callback.answer()


@dp.message(StateFilter(Form.name))
async def step_name(message: Message, state: FSMContext) -> None:
    await state.update_data(name=message.text)
    await state.set_state(Form.phone)
    await message.answer(
        "Спасибо! Теперь укажите номер телефона (или нажмите кнопку ниже).",
        reply_markup=phone_keyboard(),
    )


@dp.message(StateFilter(Form.phone), F.contact)
async def step_phone_contact(message: Message, state: FSMContext) -> None:
    await finish(message, state, message.contact.phone_number)


@dp.message(StateFilter(Form.phone))
async def step_phone_text(message: Message, state: FSMContext) -> None:
    await finish(message, state, message.text)


async def finish(message: Message, state: FSMContext, phone: str) -> None:
    data = await state.get_data()
    iso = data.get("date")
    t = data.get("time")
    name = data.get("name", "—")
    service = data.get("service", "—")
    price = data.get("price")

    if not iso or not t:
        await message.answer("Что-то пошло не так, начните заново: /start", reply_markup=ReplyKeyboardRemove())
        await state.clear()
        return

    if t in bookings.get(iso, {}):
        await message.answer(
            "К сожалению, это время только что заняли. Выберите другое:",
            reply_markup=ReplyKeyboardRemove(),
        )
        await message.answer(f"Дата: {format_date_label(iso)}\nВыберите время:", reply_markup=times_keyboard(iso))
        await state.set_state(None)
        return

    user = message.from_user
    bookings.setdefault(iso, {})[t] = {
        "name": name,
        "phone": phone,
        "service": service,
        "price": price,
        "user_id": user.id,
        "username": user.username,
        "reminded": False,
    }
    save_bookings()

    label = format_date_label(iso)
    price_part = f" ({price} {CURRENCY})" if price is not None else ""
    lead = (
        "💈 <b>Новая запись в барбершоп</b>\n\n"
        f"<b>Услуга:</b> {service}{price_part}\n"
        f"<b>Дата:</b> {label}\n"
        f"<b>Время:</b> {t}\n"
        f"<b>Имя:</b> {name}\n"
        f"<b>Телефон:</b> {phone}\n"
        f"<b>Telegram:</b> {'@' + user.username if user.username else 'нет ника'} (id {user.id})"
    )
    if OWNER_CHAT_ID:
        try:
            await message.bot.send_message(int(OWNER_CHAT_ID), lead)
        except Exception:
            logging.exception("Не удалось отправить заявку владельцу")
    logging.info("BOOKING: %s %s -> %s (%s)", iso, t, name, service)

    await message.answer(
        f"{FINAL_MESSAGE}\n\n💈 {service}{price_part}\n📅 {label} в {t}",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.clear()


@dp.message(Command("schedule"))
async def schedule(message: Message) -> None:
    if not is_owner(message.from_user.id):
        return
    lines = []
    for d in available_dates():
        iso = d.isoformat()
        day_bookings = bookings.get(iso, {})
        free = SLOT_COUNT - len(day_bookings)
        lines.append(f"\n<b>{format_date_label(iso)}</b> — свободно {free}/{SLOT_COUNT}")
        for t in SLOTS:
            if t in day_bookings:
                b = day_bookings[t]
                lines.append(f"  ⛔ {t} — {b['name']} ({b['phone']}) — {b.get('service', '—')}")
            else:
                lines.append(f"  ✅ {t}")
    await message.answer("\n".join(lines) or "Расписание пусто")


@dp.message(Command("cancel"))
async def cancel(message: Message) -> None:
    if not is_owner(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer("Используйте: /cancel ГГГГ-ММ-ДД ЧЧ:ММ")
        return
    _, iso, t = parts
    if t in bookings.get(iso, {}):
        del bookings[iso][t]
        if not bookings[iso]:
            del bookings[iso]
        save_bookings()
        await message.answer(f"Запись {iso} {t} отменена ✅")
    else:
        await message.answer("Такой записи не найдено")


@dp.message()
async def fallback(message: Message) -> None:
    await message.answer("Чтобы записаться на стрижку, нажмите /start")


async def reminder_loop(bot: Bot) -> None:
    """Раз в REMINDER_CHECK_SECONDS шлёт клиентам напоминание о записи."""
    while True:
        now = datetime.now(TIMEZONE)
        for iso, day_bookings in bookings.items():
            appt_date = date.fromisoformat(iso)
            for t, b in day_bookings.items():
                if b.get("reminded"):
                    continue
                hour, minute = map(int, t.split(":"))
                appt_at = datetime(
                    appt_date.year, appt_date.month, appt_date.day, hour, minute,
                    tzinfo=TIMEZONE,
                )
                remind_at = appt_at - timedelta(hours=REMINDER_HOURS_BEFORE)
                if remind_at <= now < appt_at:
                    try:
                        await bot.send_message(
                            b["user_id"],
                            f"⏰ Напоминание: сегодня в {t} вас ждёт {SHOP_NAME}.\n"
                            f"Услуга: {b.get('service', '—')}",
                        )
                        b["reminded"] = True
                        save_bookings()
                    except Exception:
                        logging.exception("Не удалось отправить напоминание пользователю %s", b.get("user_id"))
        await asyncio.sleep(REMINDER_CHECK_SECONDS)


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN (см. инструкцию, шаг 1)")
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(reminder_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
