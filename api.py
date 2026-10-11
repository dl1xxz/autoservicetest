"""API мини-приложения без привязки к веб-серверу.

handle() принимает разобранный запрос и возвращает (HTTP-код, JSON, событие).
Его вызывают и боевой сервер (web.py, aiohttp), и локальный (devserver.py),
поэтому тесты test_api.py проверяют ровно тот код, который работает на хостинге.
"""
import os
import re
from datetime import date, datetime

import config
import db

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
DAYS_SHOWN = 14
WORKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp", "works")


def day_label(ts: int, now: int | None = None) -> str:
    local = db.to_local(ts)
    today = db.to_local(now or db._now()).date()
    delta = (local.date() - today).days
    if delta == 0:
        return "сегодня"
    if delta == 1:
        return "завтра"
    return f"{WEEKDAYS[local.weekday()].lower()}, {local.day} {MONTHS[local.month - 1]}"


def booking_json(b: dict, now: int | None = None) -> dict:
    now = now or db._now()
    return {
        "id": b["id"],
        "service": b["service_key"],
        "service_name": b["service_name"],
        "price": b["price"],
        "from": bool(db.SERVICES.get(b["service_key"], {}).get("from")),
        "status": b["status"],
        "starts_at": b["starts_at"],
        "day": day_label(b["starts_at"], now),
        "time": f"{db.to_local(b['starts_at']):%H:%M}",
        "ready": (f"{day_label(b['ends_at'], now)} в {db.to_local(b['ends_at']):%H:%M}"
                  if b["duration"] >= 1440 else None),
        "can_change": b["status"] == "confirmed" and now <= b["starts_at"] - config.CANCEL_UNTIL_MIN * 60,
    }


def _next_free(service_key: str, now: int) -> dict | None:
    for day in db.open_days(now)[:DAYS_SHOWN]:
        for moment, free in db.slots(service_key, day, now=now):
            if free:
                ts = int(moment.timestamp())
                return {"day": day_label(ts, now), "time": f"{moment:%H:%M}", "date": day.isoformat()}
    return None


def _config(now: int) -> dict:
    hours = []
    for weekday in range(7):
        span = config.HOURS.get(weekday)
        hours.append({"day": WEEKDAYS[weekday], "hours": f"{span[0]}–{span[1]}" if span else None})
    gallery = [{"url": "works/" + g["file"], "caption": g.get("caption", "")}
               for g in config.GALLERY
               if re.fullmatch(r"[\w.-]+", g.get("file", "")) and os.path.isfile(os.path.join(WORKS_DIR, g["file"]))]
    return {
        "name": config.NAME, "tagline": config.TAGLINE, "accent": config.ACCENT,
        "logo": config.LOGO if config.LOGO and re.fullmatch(r"[\w.-]+", config.LOGO)
        and any(os.path.isfile(os.path.join(folder, config.LOGO))
                for folder in (os.path.dirname(WORKS_DIR), os.path.dirname(os.path.dirname(WORKS_DIR)))) else None,
        "demo": bool(config.DEMO),
        "address": config.ADDRESS, "phone": config.PHONE, "map_url": config.MAP_URL,
        "hours": hours, "gallery": gallery,
        "cancel_until_min": config.CANCEL_UNTIL_MIN,
        "services": [{
            "key": s["key"], "name": s["name"], "desc": s.get("desc", ""), "price": s["price"],
            "from": bool(s.get("from")), "group": s.get("group"), "short": s.get("short"),
            "icon": s.get("icon"), "duration": s["duration"], "next": _next_free(s["key"], now),
        } for s in config.SERVICES],
    }


def _days(service_key: str, now: int, move_id: int | None) -> list[dict]:
    result = []
    for day in db.open_days(now)[:DAYS_SHOWN]:
        free = sum(1 for _, ok in db.slots(service_key, day, now=now, ignore_booking=move_id) if ok)
        result.append({"date": day.isoformat(), "dow": WEEKDAYS[day.weekday()], "day": day.day,
                       "month": MONTHS[day.month - 1], "free": free})
    return result


def _slots(service_key: str, day: date, now: int, move_id: int | None) -> list[dict]:
    return [{"time": f"{m:%H:%M}", "start": int(m.timestamp()), "free": ok}
            for m, ok in db.slots(service_key, day, now=now, ignore_booking=move_id)]


def _own_move_id(raw, user: dict | None) -> int | None:
    """move=<id> учитывается, только если это запись самого пользователя."""
    if not raw or not user or not str(raw).isdigit():
        return None
    try:
        booking = db.get_booking(int(raw))
    except db.NotFound:
        return None
    return booking["id"] if booking["user_id"] == user["id"] else None


def handle(method: str, path: str, query: dict, body: dict, user: dict | None, now: int | None = None):
    """→ (status, payload, event). event = (вид, запись) для уведомлений в чат или None."""
    now = now or db._now()
    try:
        if method == "GET" and path == "/api/config":
            return 200, _config(now), None

        if method == "GET" and path in ("/api/days", "/api/slots"):
            service = query.get("service", "")
            if service not in db.SERVICES:
                return 404, {"error": "Такой услуги нет."}, None
            move_id = _own_move_id(query.get("move"), user)
            if path == "/api/days":
                return 200, {"days": _days(service, now, move_id)}, None
            try:
                day = date.fromisoformat(query.get("day", ""))
            except ValueError:
                return 400, {"error": "Не понял дату."}, None
            return 200, {"slots": _slots(service, day, now, move_id)}, None

        # Всё, что ниже, — личные данные и изменения: только для подтверждённого пользователя Telegram.
        if user is None:
            return 401, {"error": "Откройте приложение из Telegram: кнопка «Запись» в чате с ботом."}, None

        if method == "GET" and path == "/api/bookings":
            return 200, {"bookings": [booking_json(b, now) for b in db.user_bookings(user["id"], now=now)]}, None

        if method == "POST" and path == "/api/bookings":
            start = body.get("start")
            if not isinstance(start, int) or isinstance(start, bool):
                return 400, {"error": "Выберите время."}, None
            request_id = str(body.get("request_id") or "")
            if not 8 <= len(request_id) <= 64:
                return 400, {"error": "Обновите страницу и попробуйте ещё раз."}, None
            request_id = f"{user['id']}:{request_id}"
            repeat = db.find_by_request(request_id) is not None
            booking = db.create_booking(
                user_id=user["id"], name=str(body.get("name") or "")[:60], phone=str(body.get("phone") or ""),
                car=str(body.get("car") or "")[:60], service_key=str(body.get("service") or ""),
                start=start, request_id=request_id, now=now)
            # Повтор того же запроса возвращает ту же запись; уведомлять второй раз не нужно.
            return 200, {"booking": booking_json(booking, now)}, None if repeat else ("created", booking)

        match = re.fullmatch(r"/api/bookings/(\d+)/(cancel|move)", path)
        if method == "POST" and match:
            booking_id, action = int(match.group(1)), match.group(2)
            before = db.get_booking(booking_id)
            if action == "cancel":
                booking = db.cancel_booking(booking_id, user_id=user["id"], now=now)
                changed = before["status"] != booking["status"]
                return 200, {"booking": booking_json(booking, now)}, ("cancelled", booking) if changed else None
            start = body.get("start")
            if not isinstance(start, int) or isinstance(start, bool):
                return 400, {"error": "Выберите время."}, None
            booking = db.reschedule_booking(booking_id, start, user_id=user["id"], now=now)
            changed = before["starts_at"] != booking["starts_at"]
            return 200, {"booking": booking_json(booking, now)}, ("moved", booking) if changed else None

        return 404, {"error": "Не найдено."}, None
    except db.NotFound as error:
        return 404, {"error": str(error)}, None
    except (db.SlotTaken, db.TooLate) as error:
        return 409, {"error": str(error)}, None
    except db.BookingError as error:
        return 400, {"error": str(error)}, None
