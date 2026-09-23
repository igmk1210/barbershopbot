"""
Бот для барбершопа: приём заявок на стрижку и отправка их владельцу.

Что делает:
  клиент нажимает /start -> выбирает свободную дату (занятые дни помечены
  красным) -> выбирает свободное время (занятые слоты помечены и
  недоступны для выбора) -> оставляет имя и телефон -> заявка уходит
  владельцу, а слот помечается как занятый.

Владельцу доступны команды:
  /schedule — показать расписание (какие слоты свободны/заняты)
  /cancel ГГГГ-ММ-ДД ЧЧ:ММ — отменить запись и освободить слот

Настройка: переменные окружения BOT_TOKEN и OWNER_CHAT_ID (файл .env).
Расписание, часы работы и длительность стрижки настраиваются в блоке
"НАСТРОЙКИ" ниже.
"""

import asyncio
import json
import logging
import os
from datetime import date, timedelta
from pathlib import Path

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

SHOP_NAME = "Barbershop"
GREETING = (
    "Здравствуйте! 💈\n\n"
    f"Это бот записи в {SHOP_NAME}.\n\n"
    "Выберите свободную дату и время — заявка сразу уйдёт мастеру."
)
FINAL_MESSAGE = "Готово! Заявка принята ✅\n\nМы свяжемся с вами для подтверждения записи."

DAYS_AHEAD = 14      # на сколько дней вперёд открыта запись
DAY_OFF = 0          # выходной: 0=Пн, 1=Вт ... 6=Вс; None — без выходных
WORK_HOUR_FROM = 10  # запись с 10:00
WORK_HOUR_TO = 20    # запись до 20:00 (не включительно)
SLOT_MINUTES = 60    # длительность одного слота записи

BOOKINGS_FILE = Path(__file__).parent / "bookings.json"

WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS_RU = [
    "янв", "фев", "мар", "апр", "май", "июн",
    "июл", "авг", "сен", "окт", "ноя", "дек",
]

# ---------------------------------------------------------------------------
# Хранилище записей.
# Формат: { "YYYY-MM-DD": { "HH:MM": {name, phone, user_id, username} } }
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
    await message.answer("Выберите дату:", reply_markup=dates_keyboard())


@dp.callback_query(F.data == "full_date")
async def full_date(callback: CallbackQuery) -> None:
    await callback.answer("На эту дату свободных мест нет 😔", show_alert=True)


@dp.callback_query(F.data == "back_to_dates")
async def back_to_dates(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Выберите дату:", reply_markup=dates_keyboard())
    await callback.answer()


@dp.callback_query(F.data.startswith("date:"))
async def choose_date(callback: CallbackQuery, state: FSMContext) -> None:
    iso = callback.data.split(":", 1)[1]
    await state.update_data(date=iso)
    await callback.message.edit_text(
        f"Дата: {format_date_label(iso)}\nВыберите время:",
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
    await state.set_state(Form.name)
    await callback.message.edit_text(
        f"Дата: {format_date_label(iso)}, время: {t}\n\nКак вас зовут?"
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
        "user_id": user.id,
        "username": user.username,
    }
    save_bookings()

    label = format_date_label(iso)
    lead = (
        "💈 <b>Новая запись в барбершоп</b>\n\n"
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
    logging.info("BOOKING: %s %s -> %s", iso, t, name)

    await message.answer(
        f"{FINAL_MESSAGE}\n\n📅 {label} в {t}",
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
                lines.append(f"  ⛔ {t} — {b['name']} ({b['phone']})")
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


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN (см. инструкцию, шаг 1)")
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
