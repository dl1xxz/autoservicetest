"""Telegram-бот записи в автосервис / детейлинг-студию.

Запуск:
    Хостинг (bothost и подобные): токен — в поле «Bot Token», OWNER_IDS — в переменных окружения.
    Локально: pip install -r requirements.txt, скопировать .env.example в .env, python bot.py.
    Токен в код и в репозиторий не кладётся.

Клиент: /start → приветствие и кнопка мини-приложения (услуги, цены, работы, запись).
Если адрес мини-приложения не задан (WEBAPP_URL), запись идёт кнопками прямо в чате.
Владелец (ID из OWNER_IDS): /today, /week, /stats, /add, /block, /unblock, /help.
"""
import asyncio
import contextlib
import html
import logging
import uuid
from datetime import date, datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                           MenuButtonWebApp, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo)

import config
import db
import web

log = logging.getLogger("bot")
router = Router()

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
BTN_BOOK, BTN_MINE, BTN_INFO = "📅 Записаться", "🗒 Мои записи", "📍 Контакты"


class Book(StatesGroup):
    day = State()
    time = State()
    name = State()
    phone = State()
    car = State()
    confirm = State()


# ------------------------------------------------------------------ оформление
def esc(text) -> str:
    return html.escape(str(text or ""))


def money(amount: int) -> str:
    return f"{amount:,}".replace(",", " ") + " ₽"


def price_of(item: dict) -> str:
    """Цена услуги или записи; «от …», если так указано в прайсе студии."""
    key = item.get("service_key") or item.get("key")
    prefix = "от " if db.SERVICES.get(key, {}).get("from") else ""
    return prefix + money(item["price"])


DEMO_NOTE = "\n\n<i>Это демо-версия: записи не передаются в студию.</i>" if config.DEMO else ""


def duration(minutes: int) -> str:
    if minutes >= 1440:
        days = minutes // 1440
        return f"{days} сут."
    if minutes % 60 == 0:
        return f"{minutes // 60} ч"
    hours, rest = divmod(minutes, 60)
    return (f"{hours} ч " if hours else "") + (f"{rest} мин" if rest else "")


def when(ts: int) -> str:
    local = db.to_local(ts)
    return f"{WEEKDAYS[local.weekday()]} {local:%d.%m} в {local:%H:%M}"


def is_owner(user_id: int) -> bool:
    return user_id in config.OWNER_IDS


def main_menu() -> ReplyKeyboardMarkup:
    # С мини-приложением кнопка «Записаться» открывает его сразу, без сообщения в чат.
    book = (KeyboardButton(text=BTN_BOOK, web_app=WebAppInfo(url=config.WEBAPP_URL)) if config.WEBAPP_URL
            else KeyboardButton(text=BTN_BOOK))
    return ReplyKeyboardMarkup(
        keyboard=[[book], [KeyboardButton(text=BTN_MINE), KeyboardButton(text=BTN_INFO)]],
        resize_keyboard=True)


def open_app_kb(text: str = "Открыть запись", page: str = "") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, web_app=WebAppInfo(url=config.WEBAPP_URL + page))]])


def rows(buttons: list[InlineKeyboardButton], per_row: int) -> list[list[InlineKeyboardButton]]:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def services_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{s['name']} · {price_of(s)}", callback_data=f"svc:{s['key']}")]
        for s in config.SERVICES])


def days_kb(page: int = 0) -> InlineKeyboardMarkup:
    days = db.open_days()
    per_page = 8
    chunk = days[page * per_page:(page + 1) * per_page]
    buttons = [InlineKeyboardButton(text=f"{WEEKDAYS[d.weekday()]} {d:%d.%m}", callback_data=f"day:{d.isoformat()}")
               for d in chunk]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="← Раньше", callback_data=f"days:{page - 1}"))
    if (page + 1) * per_page < len(days):
        nav.append(InlineKeyboardButton(text="Позже →", callback_data=f"days:{page + 1}"))
    return InlineKeyboardMarkup(inline_keyboard=rows(buttons, 4) + ([nav] if nav else []))


def times_kb(service_key: str, day: date, ignore_booking: int | None = None) -> tuple[InlineKeyboardMarkup, int]:
    """Кнопки времени. Занятое время видно, но не нажимается."""
    buttons, free_count = [], 0
    for moment, free in db.slots(service_key, day, ignore_booking=ignore_booking):
        if free:
            free_count += 1
            buttons.append(InlineKeyboardButton(text=f"{moment:%H:%M}", callback_data=f"time:{moment:%H%M}"))
        else:
            buttons.append(InlineKeyboardButton(text=f"✖ {moment:%H:%M}", callback_data="busy"))
    back = [InlineKeyboardButton(text="← Другой день", callback_data="days:0")]
    return InlineKeyboardMarkup(inline_keyboard=rows(buttons, 4) + [back]), free_count


def booking_text(b: dict, title: str) -> str:
    lines = [f"<b>{esc(title)}</b>", "",
             f"{esc(b['service_name'])} · {price_of(b)}",
             f"🕒 {when(b['starts_at'])}"]
    if b["duration"] >= 1440:
        lines.append(f"Машина будет готова: {when(b['ends_at'])}")
    lines += [f"📍 {esc(config.ADDRESS)}", f"Запись № {b['id']}"]
    return "\n".join(lines)


def client_actions_kb(b: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Перенести", callback_data=f"move:{b['id']}"),
        InlineKeyboardButton(text="Отменить", callback_data=f"cancel:{b['id']}"),
    ]])


def owner_line(b: dict) -> str:
    mark = {"completed": "✅ ", "confirmed": ""}.get(b["status"], "")
    car = f", {esc(b['car'])}" if b["car"] else ""
    return (f"{mark}<b>{when(b['starts_at'])}</b> · {esc(b['box'])}\n"
            f"{esc(b['service_name'])} · {price_of(b)}\n"
            f"{esc(b['name'])}, {esc(b['phone'])}{car} · № {b['id']}")


def owner_actions_kb(b: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Готово", callback_data=f"o_done:{b['id']}"),
        InlineKeyboardButton(text="Не приехал", callback_data=f"o_noshow:{b['id']}"),
        InlineKeyboardButton(text="Отменить", callback_data=f"o_cancel:{b['id']}"),
    ]])


async def notify_owners(bot: Bot, text: str, booking: dict | None = None) -> None:
    for owner_id in config.OWNER_IDS:
        try:
            await bot.send_message(owner_id, text,
                                   reply_markup=owner_actions_kb(booking) if booking else None)
        except TelegramAPIError as error:  # владелец ещё не нажал /start у бота
            log.warning("не удалось написать владельцу %s: %s", owner_id, error)


# ------------------------------------------------------------------ клиент: меню
@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    name = esc(message.from_user.first_name or "")
    hello = f"Здравствуйте, {name}!" if name else "Здравствуйте!"
    if config.WEBAPP_URL:
        text = (f"{hello} Это <b>{esc(config.NAME)}</b>.\n\n"
                "Все услуги с ценами, фотографии работ и свободное время — в нашем приложении. "
                "Запись занимает минуту, подтверждение и напоминание придут в этот чат.")
    else:
        text = (f"{hello} Это запись в <b>{esc(config.NAME)}</b>.\n"
                "Выберите услугу и удобное время — подтверждение придёт сюда же.")
    text += DEMO_NOTE
    if is_owner(message.from_user.id):
        text += "\n\nВы владелец: команды кабинета — /help"
    await message.answer(text, reply_markup=main_menu())
    if config.WEBAPP_URL:
        await message.answer("Нажмите, чтобы выбрать услугу и время:", reply_markup=open_app_kb())


@router.message(Command("cancel"))
async def abort(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Хорошо, ничего не записал.", reply_markup=main_menu())


@router.message(F.text == BTN_INFO)
async def info(message: Message) -> None:
    hours = []
    for weekday in range(7):
        span = config.HOURS.get(weekday)
        hours.append(f"{WEEKDAYS[weekday]}: " + (f"{span[0]}–{span[1]}" if span else "выходной"))
    await message.answer(
        f"<b>{esc(config.NAME)}</b>\n📍 {esc(config.ADDRESS)}\n📞 {esc(config.PHONE)}\n\n" + "\n".join(hours),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Открыть карту", url=config.MAP_URL)]]))


@router.message(F.text == BTN_MINE)
async def my_bookings(message: Message, state: FSMContext) -> None:
    await state.clear()
    bookings = db.user_bookings(message.from_user.id)
    if not bookings:
        await message.answer("У вас нет активных записей.", reply_markup=main_menu())
        return
    for b in bookings:
        await message.answer(booking_text(b, "Ваша запись"), reply_markup=client_actions_kb(b))


# ------------------------------------------------------------------ клиент: запись
@router.message(F.text == BTN_BOOK)
async def choose_service(message: Message, state: FSMContext) -> None:
    await state.clear()
    if config.WEBAPP_URL:  # старый клиент Telegram прислал текст кнопки вместо открытия приложения
        await message.answer("Услуги, цены и свободное время — в приложении:", reply_markup=open_app_kb())
        return
    lines = ["<b>Что делаем?</b>", ""]
    for s in config.SERVICES:
        lines.append(f"• {esc(s['name'])} — {price_of(s)}, {duration(s['duration'])}")
    await message.answer("\n".join(lines), reply_markup=services_kb())


@router.callback_query(F.data.startswith("svc:"))
async def service_chosen(call: CallbackQuery, state: FSMContext) -> None:
    key = call.data.split(":", 1)[1]
    service = db.SERVICES.get(key)
    if service is None:
        await call.answer("Этой услуги больше нет.", show_alert=True)
        return
    await state.clear()
    await state.set_state(Book.day)
    await state.update_data(service=key)
    await call.message.edit_text(f"<b>{esc(service['name'])}</b> · {price_of(service)}\n\nВыберите день:",
                                 reply_markup=days_kb())
    await call.answer()


@router.callback_query(F.data.startswith("days:"))
async def days_page(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if "service" not in data:
        await call.answer("Начните заново: «Записаться».", show_alert=True)
        return
    await state.set_state(Book.day)
    await call.message.edit_text("Выберите день:", reply_markup=days_kb(int(call.data.split(":")[1])))
    await call.answer()


@router.callback_query(F.data.startswith("day:"))
async def day_chosen(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if "service" not in data:
        await call.answer("Начните заново: «Записаться».", show_alert=True)
        return
    day = date.fromisoformat(call.data.split(":", 1)[1])
    keyboard, free_count = times_kb(data["service"], day, data.get("move_id"))
    await state.update_data(day=day.isoformat())
    await state.set_state(Book.time)
    title = f"<b>{WEEKDAYS[day.weekday()]} {day:%d.%m}</b>\n"
    title += "Выберите время. ✖ — занято." if free_count else "На этот день свободного времени нет."
    await call.message.edit_text(title, reply_markup=keyboard)
    await call.answer()


@router.callback_query(F.data == "busy")
async def busy_slot(call: CallbackQuery) -> None:
    await call.answer("Это время занято.")


@router.callback_query(F.data.startswith("time:"))
async def time_chosen(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    if "service" not in data or "day" not in data:
        await call.answer("Начните заново: «Записаться».", show_alert=True)
        return
    hhmm = call.data.split(":", 1)[1]
    start = datetime.combine(date.fromisoformat(data["day"]),
                             datetime.strptime(hhmm, "%H%M").time(), db.TZ)
    start_ts = int(start.timestamp())

    if data.get("move_id"):  # это перенос существующей записи
        try:
            booking = db.reschedule_booking(data["move_id"], start_ts, user_id=call.from_user.id)
        except db.SlotTaken as error:
            keyboard, _ = times_kb(data["service"], start.date(), data["move_id"])
            await call.message.edit_text(f"{esc(error)}", reply_markup=keyboard)
            await call.answer()
            return
        except db.BookingError as error:
            await state.clear()
            await call.message.edit_text(esc(error))
            await call.answer()
            return
        await state.clear()
        await call.message.edit_text(booking_text(booking, "Запись перенесена"),
                                     reply_markup=client_actions_kb(booking))
        await notify_owners(bot, "🔁 Клиент перенёс запись\n\n" + owner_line(booking), booking)
        await call.answer()
        return

    await state.update_data(start=start_ts)
    await state.set_state(Book.name)
    await call.message.edit_text(f"Время: <b>{when(start_ts)}</b>\n\nКак к вам обращаться?")
    await call.answer()


@router.message(Book.name, F.text)
async def name_entered(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if not 1 <= len(name) <= 60 or name.startswith("/"):
        await message.answer("Напишите имя, например: Алексей.")
        return
    await state.update_data(name=name)
    await state.set_state(Book.phone)
    await message.answer(
        "Телефон для связи. Нажмите кнопку или напишите номер.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📱 Отправить мой номер", request_contact=True)]],
            resize_keyboard=True, one_time_keyboard=True))


@router.message(Book.phone, F.contact | F.text)
async def phone_entered(message: Message, state: FSMContext) -> None:
    raw = message.contact.phone_number if message.contact else message.text
    try:
        phone = db.normalize_phone(raw)
    except db.BookingError as error:
        await message.answer(esc(error))
        return
    await state.update_data(phone=phone)
    await state.set_state(Book.car)
    await message.answer("Марка и модель автомобиля? Например: Kia K5.", reply_markup=ReplyKeyboardRemove())


@router.message(Book.car, F.text)
async def car_entered(message: Message, state: FSMContext) -> None:
    car = message.text.strip()[:60]
    data = await state.update_data(car=car, request_id=uuid.uuid4().hex)
    service = db.SERVICES[data["service"]]
    await state.set_state(Book.confirm)
    await message.answer(
        "<b>Проверьте запись</b>\n\n"
        f"{esc(service['name'])} · {price_of(service)}\n"
        f"🕒 {when(data['start'])}\n"
        f"👤 {esc(data['name'])}, {esc(data['phone'])}\n"
        f"🚗 {esc(car)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm"),
            InlineKeyboardButton(text="Изменить время", callback_data="days:0"),
        ]]))


@router.callback_query(F.data == "confirm")
async def confirm(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    if await state.get_state() != Book.confirm.state or "request_id" not in data:
        await call.answer("Эта запись уже оформлена или устарела.", show_alert=True)
        return
    try:
        # request_id: повторное нажатие «Подтвердить» вернёт ту же запись, а не создаст вторую.
        booking = db.create_booking(
            user_id=call.from_user.id, name=data["name"], phone=data["phone"], car=data["car"],
            service_key=data["service"], start=data["start"], request_id=data["request_id"])
    except (db.SlotTaken, db.InvalidStart) as error:
        await state.set_state(Book.day)
        await call.message.edit_text(f"{esc(error)}\n\nВыберите день:", reply_markup=days_kb())
        await call.answer()
        return
    except db.BookingError as error:
        await state.clear()
        await call.message.edit_text(esc(error))
        await call.answer()
        return
    await state.clear()
    text = booking_text(booking, "Вы записаны ✅")
    if booking["reminded"] == 0:
        text += "\n\nНапомню за сутки до визита."
    await call.message.edit_text(text, reply_markup=client_actions_kb(booking))
    await call.message.answer("Ждём вас!", reply_markup=main_menu())
    await notify_owners(bot, "🆕 Новая запись\n\n" + owner_line(booking), booking)
    await call.answer()


# Любой ввод во время оформления, который не подошёл ни одному шагу выше.
@router.message(Book.day)
@router.message(Book.time)
@router.message(Book.confirm)
async def use_buttons(message: Message) -> None:
    await message.answer("Выберите вариант кнопкой в сообщении выше или отправьте /cancel.")


# ------------------------------------------------------------------ клиент: отмена и перенос
@router.callback_query(F.data.startswith("cancel:"))
async def ask_cancel(call: CallbackQuery) -> None:
    booking_id = call.data.split(":")[1]
    await call.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Да, отменить", callback_data=f"cancel_yes:{booking_id}"),
        InlineKeyboardButton(text="Нет, оставить", callback_data=f"keep:{booking_id}"),
    ]]))
    await call.answer()


@router.callback_query(F.data.startswith("keep:"))
async def keep(call: CallbackQuery) -> None:
    try:
        booking = db.get_booking(int(call.data.split(":")[1]))
    except db.NotFound:
        await call.answer("Запись не найдена.", show_alert=True)
        return
    if booking["user_id"] == call.from_user.id and booking["status"] == "confirmed":
        await call.message.edit_reply_markup(reply_markup=client_actions_kb(booking))
    await call.answer("Запись в силе.")


@router.callback_query(F.data.startswith("cancel_yes:"))
async def do_cancel(call: CallbackQuery, bot: Bot) -> None:
    try:
        booking = db.cancel_booking(int(call.data.split(":")[1]), user_id=call.from_user.id)
    except db.BookingError as error:
        await call.answer(str(error), show_alert=True)
        return
    await call.message.edit_text(booking_text(booking, "Запись отменена"))
    await notify_owners(bot, "❌ Клиент отменил запись\n\n" + owner_line(booking))
    await call.answer()


@router.callback_query(F.data.startswith("move:"))
async def start_move(call: CallbackQuery, state: FSMContext) -> None:
    try:
        booking = db.get_booking(int(call.data.split(":")[1]))
    except db.NotFound:
        booking = None
    if not booking or booking["user_id"] != call.from_user.id or booking["status"] != "confirmed":
        await call.answer("Эту запись нельзя перенести.", show_alert=True)
        return
    if booking["service_key"] not in db.SERVICES:
        await call.answer(f"Для переноса позвоните нам: {config.PHONE}", show_alert=True)
        return
    await state.clear()
    await state.set_state(Book.day)
    await state.update_data(service=booking["service_key"], move_id=booking["id"])
    await call.message.answer(f"Перенос записи № {booking['id']}. Выберите новый день:", reply_markup=days_kb())
    await call.answer()


# ------------------------------------------------------------------ владелец
def parse_local(day: str, hhmm: str) -> int:
    return int(datetime.combine(date.fromisoformat(day), datetime.strptime(hhmm, "%H:%M").time(), db.TZ).timestamp())


async def send_agenda(message: Message, day_from: date, day_to: date, title: str) -> None:
    bookings, blocks = db.agenda(day_from, day_to)
    if not bookings and not blocks:
        await message.answer(f"<b>{title}</b>\nЗаписей нет.")
        return
    await message.answer(f"<b>{title}</b> — записей: {len(bookings)}")
    for b in bookings:
        await message.answer(owner_line(b), reply_markup=owner_actions_kb(b) if b["status"] == "confirmed" else None)
    for block in blocks:
        reason = f" — {esc(block['reason'])}" if block["reason"] else ""
        await message.answer(f"⛔ {esc(block['box'])} закрыт: {when(block['starts_at'])} → {when(block['ends_at'])}"
                             f"{reason}\nСнять: /unblock {block['id']}")


@router.message(Command("help"), F.from_user.id.in_(config.OWNER_IDS))
async def owner_help(message: Message) -> None:
    await message.answer(
        "<b>Кабинет владельца</b>\n\n"
        "/today — записи на сегодня\n"
        "/week — записи на 7 дней\n"
        "/stats — итоги за 7 и 30 дней\n"
        "/add услуга ГГГГ-ММ-ДД ЧЧ:ММ телефон имя — записать клиента вручную\n"
        "/block бокс ГГГГ-ММ-ДД ЧЧ:ММ ЧЧ:ММ причина — закрыть бокс\n"
        "/unblock номер — снять блокировку\n\n"
        "Ключи услуг: " + ", ".join(f"<code>{s['key']}</code> ({esc(s['name'])})" for s in config.SERVICES) + "\n"
        "Боксы: " + ", ".join(f"<code>{i + 1}</code> — {esc(b)}" for i, b in enumerate(config.BOXES)) + "\n\n"
        "Пример: <code>/add wash 2026-10-20 14:00 +79160000000 Иван</code>\n"
        "Пример: <code>/block 1 2026-10-20 10:00 18:00 ремонт ворот</code>")


@router.message(Command("today"), F.from_user.id.in_(config.OWNER_IDS))
async def owner_today(message: Message) -> None:
    today = datetime.now(db.TZ).date()
    await send_agenda(message, today, today, f"Сегодня, {today:%d.%m}")


@router.message(Command("week"), F.from_user.id.in_(config.OWNER_IDS))
async def owner_week(message: Message) -> None:
    today = datetime.now(db.TZ).date()
    await send_agenda(message, today, today + timedelta(days=6), "Ближайшие 7 дней")


@router.message(Command("stats"), F.from_user.id.in_(config.OWNER_IDS))
async def owner_stats(message: Message) -> None:
    today = datetime.now(db.TZ).date()
    parts = []
    for title, since in (("Последние 7 дней", today - timedelta(days=6)), ("Последние 30 дней", today - timedelta(days=29))):
        s = db.stats(since, today)
        parts.append(f"<b>{title}</b>\n"
                     f"Выполнено работ: {s['completed']} на {money(s['completed_sum'])}\n"
                     f"Отмен: {s['cancelled']}, не приехали: {s['no_show']}")
    ahead = db.stats(today, today + timedelta(days=config.MAX_DAYS_AHEAD))
    parts.append(f"<b>Впереди</b>\nЗаписей: {ahead['upcoming']}, ожидаемая сумма: {money(ahead['upcoming_sum'])}\n"
                 "<i>Это стоимость будущих записей, а не полученные деньги.</i>")
    await message.answer("\n\n".join(parts))


@router.message(Command("add"), F.from_user.id.in_(config.OWNER_IDS))
async def owner_add(message: Message, command: CommandObject) -> None:
    parts = (command.args or "").split(maxsplit=4)
    if len(parts) < 5:
        await message.answer("Формат: /add услуга ГГГГ-ММ-ДД ЧЧ:ММ телефон имя\nКлючи услуг — в /help")
        return
    key, day, hhmm, phone, name = parts
    try:
        booking = db.create_booking(user_id=None, name=name, phone=phone, car=None, service_key=key,
                                    start=parse_local(day, hhmm), by_owner=True)
    except ValueError:
        await message.answer("Не понял дату или время. Пример: 2026-10-20 14:00")
        return
    except db.BookingError as error:
        await message.answer(esc(error))
        return
    await message.answer("Записал.\n\n" + owner_line(booking), reply_markup=owner_actions_kb(booking))


@router.message(Command("block"), F.from_user.id.in_(config.OWNER_IDS))
async def owner_block(message: Message, command: CommandObject) -> None:
    parts = (command.args or "").split(maxsplit=4)
    if len(parts) < 4:
        await message.answer("Формат: /block бокс ГГГГ-ММ-ДД ЧЧ:ММ ЧЧ:ММ причина\nБокс — номер из /help")
        return
    try:
        box = config.BOXES[int(parts[0]) - 1]
        block_id = db.block_box(box, parse_local(parts[1], parts[2]), parse_local(parts[1], parts[3]),
                                parts[4] if len(parts) > 4 else "")
    except (ValueError, IndexError):
        await message.answer("Не понял бокс, дату или время. Пример: /block 1 2026-10-20 10:00 18:00 ремонт")
        return
    except db.BookingError as error:
        await message.answer(esc(error))
        return
    await message.answer(f"⛔ {esc(box)} закрыт {parts[1]} с {parts[2]} до {parts[3]}.\nСнять: /unblock {block_id}")


@router.message(Command("unblock"), F.from_user.id.in_(config.OWNER_IDS))
async def owner_unblock(message: Message, command: CommandObject) -> None:
    if not (command.args or "").strip().isdigit():
        await message.answer("Формат: /unblock номер")
        return
    removed = db.unblock(int(command.args.strip()))
    await message.answer("Блокировка снята." if removed else "Такой блокировки нет.")


@router.callback_query(F.data.startswith("o_"), F.from_user.id.in_(config.OWNER_IDS))
async def owner_action(call: CallbackQuery, bot: Bot) -> None:
    action, raw_id = call.data.split(":")
    booking_id = int(raw_id)
    try:
        if action == "o_cancel":
            booking, label, client_text = db.cancel_booking(booking_id), "❌ Отменена", "Студия отменила вашу запись"
        elif action == "o_done":
            booking, label, client_text = db.set_status(booking_id, "completed"), "✅ Выполнена", None
        else:
            booking, label, client_text = db.set_status(booking_id, "no_show"), "🚫 Не приехал", None
    except db.BookingError as error:
        await call.answer(str(error), show_alert=True)
        return
    await call.message.edit_text(f"{label}\n\n{owner_line(booking)}")
    if client_text and booking["user_id"]:
        with contextlib.suppress(TelegramAPIError):
            await bot.send_message(booking["user_id"],
                                   booking_text(booking, client_text) + f"\n\nВопросы: {esc(config.PHONE)}")
    await call.answer()


@router.callback_query(F.data.startswith("o_"))
async def not_owner(call: CallbackQuery) -> None:
    await call.answer("Это действие доступно только владельцу.", show_alert=True)


@router.message()
async def fallback(message: Message) -> None:
    await message.answer("Выберите действие в меню ниже.", reply_markup=main_menu())


# ------------------------------------------------------------------ события мини-приложения
async def on_webapp_event(bot: Bot, kind: str, booking: dict) -> None:
    """Запись, перенос или отмена сделаны в мини-приложении: пишем клиенту в чат и владельцу."""
    client = {"created": "Вы записаны ✅", "moved": "Запись перенесена", "cancelled": "Запись отменена"}[kind]
    owner = {"created": "🆕 Новая запись", "moved": "🔁 Клиент перенёс запись", "cancelled": "❌ Клиент отменил запись"}[kind]
    active = kind != "cancelled"
    text = booking_text(booking, client)
    if kind == "created" and booking["reminded"] == 0:
        text += "\n\nНапомню за сутки до визита."
    try:
        await bot.send_message(booking["user_id"], text, reply_markup=client_actions_kb(booking) if active else None)
    except TelegramAPIError as error:
        log.warning("клиенту %s не удалось написать: %s", booking["user_id"], error)
    await notify_owners(bot, f"{owner}\n\n{owner_line(booking)}", booking if active else None)


# ------------------------------------------------------------------ напоминания
async def reminder_loop(bot: Bot) -> None:
    while True:
        try:
            for booking in db.take_due_reminders():
                try:
                    await bot.send_message(booking["user_id"], booking_text(booking, "Напоминание: вы записаны"),
                                           reply_markup=client_actions_kb(booking))
                except TelegramAPIError as error:  # клиент заблокировал бота
                    log.warning("напоминание по записи %s не доставлено: %s", booking["id"], error)
        except Exception:
            log.exception("ошибка в цикле напоминаний")
        await asyncio.sleep(60)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Укажите токен от @BotFather в поле «Bot Token» на хостинге "
                         "или в файле .env при локальном запуске.")
    if not config.OWNER_IDS:
        log.warning("OWNER_IDS пуст: уведомления о записях никому не придут, кабинет недоступен.")
    db.init()
    log.info("версия кода: 3 (мини-приложение с логотипом), студия: %s", config.NAME)
    log.info("база: %s, владельцев: %d", config.DB_PATH, len(config.OWNER_IDS))
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dispatcher = Dispatcher()  # состояние диалогов хранится в памяти и сбрасывается при перезапуске
    dispatcher.include_router(router)
    reminders = asyncio.create_task(reminder_loop(bot))

    async def webapp_event(kind: str, booking: dict) -> None:
        await on_webapp_event(bot, kind, booking)

    site = await web.start(webapp_event)
    if config.WEBAPP_URL:
        try:  # кнопка «Запись» слева от поля ввода в чате с ботом
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Запись", web_app=WebAppInfo(url=config.WEBAPP_URL)))
        except TelegramAPIError as error:
            log.warning("не удалось поставить кнопку меню: %s", error)
    else:
        log.warning("WEBAPP_URL не задан: мини-приложение выключено, запись идёт кнопками в чате.")
    try:
        await dispatcher.start_polling(bot)
    finally:
        reminders.cancel()
        await site.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
