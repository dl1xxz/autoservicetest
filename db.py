"""Логика записи. Никакого Telegram здесь нет, поэтому модуль проверяется тестами (test_db.py).

Время хранится в секундах Unix (UTC). Каждое изменение идёт в транзакции BEGIN IMMEDIATE:
SQLite пускает только одного пишущего, поэтому «проверил, что бокс свободен → записал»
выполняется как одно неделимое действие и двойная запись на один бокс невозможна.
"""
import os
import sqlite3
import time as _time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import config


class BookingError(Exception):
    """Ошибка, текст которой можно показать человеку."""


class SlotTaken(BookingError):
    pass


class InvalidStart(BookingError):
    pass


class TooLate(BookingError):
    pass


class NotFound(BookingError):
    pass


TZ = ZoneInfo(config.TIMEZONE)
SERVICES = {s["key"]: s for s in config.SERVICES}

SCHEMA = """
create table if not exists bookings (
    id           integer primary key autoincrement,
    user_id      integer,              -- Telegram ID клиента; null, если записал владелец
    name         text not null,
    phone        text not null,
    car          text,
    service_key  text not null,
    service_name text not null,        -- название и цена на момент записи: правка прайса
    price        integer not null,     -- не меняет уже созданные записи
    duration     integer not null,
    buffer       integer not null,
    box          text not null,
    starts_at    integer not null,
    ends_at      integer not null,
    busy_until   integer not null,     -- ends_at + buffer
    status       text not null default 'confirmed'
                 check (status in ('confirmed', 'completed', 'cancelled', 'no_show')),
    reminded     integer not null default 0,
    request_id   text unique,          -- защита от повторного нажатия «Подтвердить»
    created_at   integer not null
);
create index if not exists bookings_box_time on bookings (box, starts_at);
create index if not exists bookings_user on bookings (user_id);

create table if not exists blocks (
    id        integer primary key autoincrement,
    box       text not null,
    starts_at integer not null,
    ends_at   integer not null,
    reason    text
);
"""


def _now() -> int:
    return int(_time.time())


@contextmanager
def _tx():
    conn = sqlite3.connect(config.DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def init() -> None:
    os.makedirs(os.path.dirname(os.path.abspath(config.DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def to_local(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, TZ)


def normalize_phone(raw: str) -> str:
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    if not 10 <= len(digits) <= 15:
        raise BookingError("Не похоже на номер телефона. Пример: +7 916 000-00-00")
    return "+" + digits


# ------------------------------------------------------------------ расписание
def day_hours(day: date):
    """(open, close) как datetime в поясе студии или None, если выходной."""
    if day.isoformat() in config.CLOSED_DATES:
        return None
    hours = config.HOURS.get(day.weekday())
    if not hours:
        return None
    opens = datetime.combine(day, datetime.strptime(hours[0], "%H:%M").time(), TZ)
    closes = datetime.combine(day, datetime.strptime(hours[1], "%H:%M").time(), TZ)
    return opens, closes


def _box_free(conn, box: str, start: int, end: int, ignore_booking: int | None = None) -> bool:
    busy = conn.execute(
        "select 1 from bookings where box = ? and status = 'confirmed' "
        "and starts_at < ? and busy_until > ? and id is not ? limit 1",
        (box, end, start, ignore_booking),
    ).fetchone()
    if busy:
        return False
    blocked = conn.execute(
        "select 1 from blocks where box = ? and starts_at < ? and ends_at > ? limit 1",
        (box, end, start),
    ).fetchone()
    return blocked is None


def _find_box(conn, service: dict, start: int, end: int, ignore_booking=None, prefer=None):
    boxes = list(service["boxes"])
    if prefer in boxes:
        boxes.remove(prefer)
        boxes.insert(0, prefer)
    for box in boxes:
        if _box_free(conn, box, start, end, ignore_booking):
            return box
    return None


def _check_start(start: int, now: int) -> None:
    """Правила для клиента: сетка, часы приёма, запас по времени, горизонт записи."""
    local = to_local(start)
    hours = day_hours(local.date())
    if hours is None:
        raise InvalidStart("В этот день студия не работает.")
    opens, closes = hours
    if not opens <= local < closes:
        raise InvalidStart("В это время машины не принимаем.")
    if int((local - opens).total_seconds()) % (config.SLOT_STEP_MIN * 60):
        raise InvalidStart("Выберите время из списка.")
    if start < now + config.MIN_LEAD_MIN * 60:
        raise InvalidStart("Это время уже слишком близко. Выберите более позднее.")
    if start > now + config.MAX_DAYS_AHEAD * 86400:
        raise InvalidStart("Так далеко запись пока не открыта.")


def open_days(now: int | None = None) -> list[date]:
    today = to_local(now or _now()).date()
    days = (today + timedelta(days=i) for i in range(config.MAX_DAYS_AHEAD + 1))
    return [d for d in days if day_hours(d)]


def slots(service_key: str, day: date, now: int | None = None, ignore_booking: int | None = None):
    """Все моменты приёма за день: [(datetime, свободно ли)]. Занятые тоже возвращаются."""
    now = now or _now()
    service = SERVICES[service_key]
    hours = day_hours(day)
    if hours is None:
        return []
    opens, closes = hours
    occupied = (service["duration"] + service["buffer"]) * 60
    result = []
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        moment = opens
        while moment < closes:
            start = int(moment.timestamp())
            free = (
                now + config.MIN_LEAD_MIN * 60 <= start <= now + config.MAX_DAYS_AHEAD * 86400
                and _find_box(conn, service, start, start + occupied, ignore_booking) is not None
            )
            result.append((moment, free))
            moment += timedelta(minutes=config.SLOT_STEP_MIN)
    finally:
        conn.close()
    return result


# ------------------------------------------------------------------ записи
def _already_close(start: int, now: int) -> int:
    """1, если до визита меньше срока напоминания: «напоминать за сутки» уже не о чем."""
    return int(start - now <= config.REMIND_BEFORE_MIN * 60)


def create_booking(*, user_id, name, phone, car, service_key, start: int,
                   request_id: str | None = None, by_owner: bool = False, now: int | None = None) -> dict:
    now = now or _now()
    service = SERVICES.get(service_key)
    if service is None:
        raise NotFound("Такой услуги нет.")
    name = (name or "").strip()
    if not name:
        raise BookingError("Укажите имя.")
    phone = normalize_phone(phone)
    if not by_owner:
        _check_start(start, now)
    ends = start + service["duration"] * 60
    busy_until = ends + service["buffer"] * 60

    with _tx() as conn:
        if request_id:
            again = conn.execute("select * from bookings where request_id = ?", (request_id,)).fetchone()
            if again:
                return dict(again)  # повтор того же запроса: возвращаем уже созданную запись
        box = _find_box(conn, service, start, busy_until)
        if box is None:
            raise SlotTaken("Это время только что заняли. Выберите другое.")
        cur = conn.execute(
            "insert into bookings (user_id, name, phone, car, service_key, service_name, price, duration,"
            " buffer, box, starts_at, ends_at, busy_until, request_id, created_at, reminded)"
            " values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (user_id, name, phone, (car or "").strip() or None, service_key, service["name"],
             service["price"], service["duration"], service["buffer"], box, start, ends, busy_until,
             request_id, now, _already_close(start, now)),
        )
        return dict(conn.execute("select * from bookings where id = ?", (cur.lastrowid,)).fetchone())


def get_booking(booking_id: int) -> dict:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("select * from bookings where id = ?", (booking_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise NotFound("Запись не найдена.")
    return dict(row)


def find_by_request(request_id: str) -> dict | None:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("select * from bookings where request_id = ?", (request_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def _owned(conn, booking_id: int, user_id: int | None) -> sqlite3.Row:
    """Запись по id; для клиента — только его собственная."""
    row = conn.execute("select * from bookings where id = ?", (booking_id,)).fetchone()
    if row is None or (user_id is not None and row["user_id"] != user_id):
        raise NotFound("Запись не найдена.")
    return row


def _check_change_window(row, now: int) -> None:
    if now > row["starts_at"] - config.CANCEL_UNTIL_MIN * 60:
        raise TooLate(f"Изменить запись онлайн уже нельзя. Позвоните нам: {config.PHONE}")


def cancel_booking(booking_id: int, user_id: int | None = None, now: int | None = None) -> dict:
    """user_id задан — отменяет клиент (свою запись и не позже срока); None — владелец."""
    now = now or _now()
    with _tx() as conn:
        row = _owned(conn, booking_id, user_id)
        if row["status"] == "cancelled":
            return dict(row)
        if row["status"] != "confirmed":
            raise BookingError("Эту запись уже нельзя отменить.")
        if user_id is not None:
            _check_change_window(row, now)
        conn.execute("update bookings set status = 'cancelled' where id = ?", (booking_id,))
        return dict(conn.execute("select * from bookings where id = ?", (booking_id,)).fetchone())


def reschedule_booking(booking_id: int, start: int, user_id: int | None = None,
                       now: int | None = None) -> dict:
    """Перенос «всё или ничего»: если новое время занято, запись остаётся как была."""
    now = now or _now()
    with _tx() as conn:
        row = _owned(conn, booking_id, user_id)
        if row["status"] != "confirmed":
            raise BookingError("Эту запись уже нельзя перенести.")
        if row["starts_at"] == start:
            return dict(row)
        if user_id is not None:
            _check_change_window(row, now)
            _check_start(start, now)
        service = SERVICES.get(row["service_key"]) or {"boxes": [row["box"]]}
        ends = start + row["duration"] * 60          # длительность — та, что была при записи
        busy_until = ends + row["buffer"] * 60
        box = _find_box(conn, service, start, busy_until, ignore_booking=booking_id, prefer=row["box"])
        if box is None:
            raise SlotTaken("Это время занято. Выберите другое.")
        conn.execute(
            "update bookings set starts_at = ?, ends_at = ?, busy_until = ?, box = ?, reminded = ? where id = ?",
            (start, ends, busy_until, box, _already_close(start, now), booking_id),
        )
        return dict(conn.execute("select * from bookings where id = ?", (booking_id,)).fetchone())


def set_status(booking_id: int, status: str, now: int | None = None) -> dict:
    """Владелец отмечает: completed (машина готова) или no_show (не приехал)."""
    now = now or _now()
    if status not in ("completed", "no_show"):
        raise BookingError("Неизвестный статус.")
    with _tx() as conn:
        row = _owned(conn, booking_id, None)
        if row["status"] == status:
            return dict(row)
        if row["status"] != "confirmed":
            raise BookingError("Статус этой записи уже нельзя изменить.")
        if status == "completed" and now < row["busy_until"]:
            # Закончили раньше — освобождаем остаток времени бокса.
            end = max(now, row["starts_at"] + 60)
            conn.execute("update bookings set status = ?, ends_at = min(ends_at, ?), busy_until = ? where id = ?",
                         (status, end, end, booking_id))
        else:
            conn.execute("update bookings set status = ? where id = ?", (status, booking_id))
        return dict(conn.execute("select * from bookings where id = ?", (booking_id,)).fetchone())


def user_bookings(user_id: int, now: int | None = None) -> list[dict]:
    now = now or _now()
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "select * from bookings where user_id = ? and status = 'confirmed' and busy_until > ? order by starts_at",
            (user_id, now),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ владелец
def _day_range(day_from: date, day_to: date) -> tuple[int, int]:
    start = datetime.combine(day_from, datetime.min.time(), TZ)
    end = datetime.combine(day_to + timedelta(days=1), datetime.min.time(), TZ)
    return int(start.timestamp()), int(end.timestamp())


def agenda(day_from: date, day_to: date) -> tuple[list[dict], list[dict]]:
    """Записи и блокировки, которые пересекаются с периодом (включая многодневные)."""
    start, end = _day_range(day_from, day_to)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        bookings = conn.execute(
            "select * from bookings where status in ('confirmed', 'completed') "
            "and starts_at < ? and busy_until > ? order by starts_at, box", (end, start)).fetchall()
        blocks = conn.execute(
            "select * from blocks where starts_at < ? and ends_at > ? order by starts_at", (end, start)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in bookings], [dict(r) for r in blocks]


def block_box(box: str, start: int, end: int, reason: str = "") -> int:
    if box not in config.BOXES:
        raise NotFound("Такого бокса нет. Есть: " + ", ".join(config.BOXES))
    if start >= end:
        raise BookingError("Конец должен быть позже начала.")
    with _tx() as conn:
        if not _box_free(conn, box, start, end):
            raise SlotTaken("На это время в боксе уже есть запись или блокировка.")
        cur = conn.execute("insert into blocks (box, starts_at, ends_at, reason) values (?,?,?,?)",
                           (box, start, end, reason.strip() or None))
        return cur.lastrowid


def unblock(block_id: int) -> bool:
    with _tx() as conn:
        return conn.execute("delete from blocks where id = ?", (block_id,)).rowcount > 0


def stats(day_from: date, day_to: date) -> dict:
    """Считает только факты: выполненные работы и их стоимость. Будущие записи — отдельно."""
    start, end = _day_range(day_from, day_to)
    conn = sqlite3.connect(config.DB_PATH)
    try:
        done, done_sum = conn.execute(
            "select count(*), coalesce(sum(price), 0) from bookings "
            "where status = 'completed' and starts_at >= ? and starts_at < ?", (start, end)).fetchone()
        upcoming, upcoming_sum = conn.execute(
            "select count(*), coalesce(sum(price), 0) from bookings "
            "where status = 'confirmed' and starts_at >= ? and starts_at < ?", (start, end)).fetchone()
        cancelled = conn.execute(
            "select count(*) from bookings where status = 'cancelled' and starts_at >= ? and starts_at < ?",
            (start, end)).fetchone()[0]
        no_show = conn.execute(
            "select count(*) from bookings where status = 'no_show' and starts_at >= ? and starts_at < ?",
            (start, end)).fetchone()[0]
    finally:
        conn.close()
    return {"completed": done, "completed_sum": done_sum, "upcoming": upcoming,
            "upcoming_sum": upcoming_sum, "cancelled": cancelled, "no_show": no_show}


# ------------------------------------------------------------------ напоминания
def take_due_reminders(now: int | None = None) -> list[dict]:
    """Забирает записи, которым пора напомнить, и сразу помечает их: дважды не уйдёт."""
    now = now or _now()
    with _tx() as conn:
        rows = conn.execute(
            "select * from bookings where status = 'confirmed' and reminded = 0 and user_id is not null "
            "and starts_at > ? and starts_at <= ?", (now, now + config.REMIND_BEFORE_MIN * 60)).fetchall()
        for row in rows:
            conn.execute("update bookings set reminded = 1 where id = ?", (row["id"],))
        return [dict(r) for r in rows]
