"""Настройки студии. Чтобы «переодеть» бота под другой бизнес, меняется только этот файл."""
import os
from pathlib import Path

# Читает файл .env рядом с ботом (строки ВИДА=значение), если он есть.
_env = Path(__file__).with_name(".env")
if _env.exists():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        if "=" in _line and not _line.lstrip().startswith("#"):
            _key, _value = _line.split("=", 1)
            os.environ.setdefault(_key.strip(), _value.strip())

# Токен в коде не хранится. На хостинге он приходит из поля «Bot Token»
# (bothost сам кладёт его в BOT_TOKEN), локально — из файла .env.
BOT_TOKEN = (os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("API_TOKEN") or "").strip()
OWNER_IDS = {int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",") if x}

# База лежит в папке data рядом с ботом. На bothost это /app/data — единственное место,
# которое не перезаписывается при обновлении из Git. В корень проекта базу класть нельзя.
DB_PATH = (os.getenv("DB_PATH") or os.getenv("DATABASE_PATH")
           or str(Path(__file__).resolve().parent / "data" / "bookings.sqlite3"))

# Адрес мини-приложения. На bothost включите «Использовать домен» в дополнительных
# настройках бота и впишите выданный адрес в переменную WEBAPP_URL (https://...).
# Пока адреса нет, бот работает по-старому: запись кнопками прямо в чате.
WEBAPP_URL = (os.getenv("WEBAPP_URL") or "").strip().rstrip("/")
if not WEBAPP_URL and os.getenv("DOMAIN"):
    WEBAPP_URL = "https://" + os.getenv("DOMAIN").strip().removeprefix("https://").rstrip("/")
PORT = int(os.getenv("PORT") or 3000)

NAME = "Мат и Глянец"
TAGLINE = "Студия автоэстетики"
ACCENT = "#3D7BFF"
LOGO = "logo.png"         # файл в папке webapp; пусто — вместо логотипа будет название текстом
TIMEZONE = "Europe/Moscow"
ADDRESS = "Анапа, с. Варваровка, ул. Центральная, 4А"
PHONE = "+7 918 063-01-33"
MAP_URL = "https://yandex.ru/maps/?text=Анапа%2C%20село%20Варваровка%2C%20Центральная%20улица%2C%204А"

# Демо-режим: в приложении и в приветствии бота видна пометка, что это образец и записи
# в студию не передаются. Выключите (переменная DEMO=0), когда студия начнёт принимать записи.
DEMO = (os.getenv("DEMO") or "1").strip().lower() not in ("0", "false", "no", "off")

BOXES = ["Пост 1", "Пост 2"]

# group — услуги с одинаковой группой показываются в приложении одной выделенной панелью,
#         short — короткое имя строки внутри неё. icon — значок: film, palette, seat, sparkle,
#         headlight, shield, window, drop, car.
# price — цена из прайса студии; "from": True показывает её как «от 8 000 ₽».
# duration — сколько пост занят работой, buffer — уборка/подготовка после неё (минуты).
# Услуга может длиться несколько дней: 4320 = трое суток, пост занят и ночью.
# ВНИМАНИЕ: длительности, буферы и распределение по постам — предварительные,
# их нужно уточнить у студии. От них зависит, какое время бот покажет свободным.
SERVICES = [
    {"key": "ppf-sedan", "group": "Оклейка полиуретаном", "short": "Седан", "icon": "film",
     "name": "Оклейка полиуретаном: седан", "from": True,
     "desc": "Защитная полиуретановая плёнка на кузов.",
     "price": 130000, "duration": 4320, "buffer": 60, "boxes": ["Пост 1"]},
    {"key": "ppf-crossover", "group": "Оклейка полиуретаном", "short": "Кроссовер", "icon": "film",
     "name": "Оклейка полиуретаном: кроссовер", "from": True,
     "desc": "Защитная полиуретановая плёнка на кузов.",
     "price": 150000, "duration": 4320, "buffer": 60, "boxes": ["Пост 1"]},
    {"key": "ppf-suv", "group": "Оклейка полиуретаном", "short": "Джип", "icon": "film",
     "name": "Оклейка полиуретаном: джип", "from": True,
     "desc": "Защитная полиуретановая плёнка на кузов.",
     "price": 170000, "duration": 5760, "buffer": 60, "boxes": ["Пост 1"]},
    {"key": "ppf-color", "icon": "palette", "name": "Оклейка цветным полиуретаном", "from": True,
     "desc": "Смена цвета кузова плёнкой.",
     "price": 150000, "duration": 5760, "buffer": 60, "boxes": ["Пост 1"]},
    {"key": "interior", "icon": "seat", "name": "Химчистка салона", "from": True, "desc": "",
     "price": 8000, "duration": 360, "buffer": 30, "boxes": ["Пост 1", "Пост 2"]},
    {"key": "polish", "icon": "sparkle", "name": "Полировка кузова", "from": True, "desc": "",
     "price": 12000, "duration": 480, "buffer": 30, "boxes": ["Пост 1", "Пост 2"]},
    {"key": "headlights", "icon": "headlight", "name": "Полировка фар", "from": True, "desc": "",
     "price": 2000, "duration": 60, "buffer": 0, "boxes": ["Пост 1", "Пост 2"]},
    {"key": "ceramic", "icon": "shield", "name": "Керамика кузова", "from": True, "desc": "",
     "price": 5000, "duration": 480, "buffer": 30, "boxes": ["Пост 1", "Пост 2"]},
    {"key": "tint", "icon": "window", "name": "Тонировка", "from": True, "desc": "",
     "price": 2000, "duration": 120, "buffer": 0, "boxes": ["Пост 1", "Пост 2"]},
    {"key": "wash", "icon": "drop", "name": "Мойка машины", "from": True, "desc": "",
     "price": 1500, "duration": 60, "buffer": 0, "boxes": ["Пост 1", "Пост 2"]},
]

# Фото работ для мини-приложения: файлы кладутся в папку webapp/works рядом с ботом.
# Пример: {"file": "bmw-m5.jpg", "caption": "BMW M5 — оклейка полиуретаном"}
GALLERY = []

# Часы приёма машин по дням недели: 0 = понедельник. None = выходной.
HOURS = {day: ("10:00", "20:00") for day in range(7)}
CLOSED_DATES = set()      # отдельные выходные дни, "ГГГГ-ММ-ДД"

SLOT_STEP_MIN = 60        # шаг сетки времени
MIN_LEAD_MIN = 120        # за сколько минут до начала ещё можно записаться
MAX_DAYS_AHEAD = 30       # на сколько дней вперёд открыта запись
CANCEL_UNTIL_MIN = 180    # клиент может отменить/перенести не позже чем за столько минут
REMIND_BEFORE_MIN = 24 * 60
