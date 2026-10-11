"""Проверка, что запрос мини-приложения действительно пришёл из Telegram.

Telegram подписывает данные запуска (initData) токеном бота. Мы пересчитываем подпись,
поэтому подделать чужой user_id и отменить чужую запись нельзя.
Алгоритм: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


def validate(init_data: str, bot_token: str, max_age: int = 24 * 3600, now: int | None = None) -> dict | None:
    """Возвращает данные пользователя Telegram или None, если подпись неверна или устарела."""
    if not init_data or not bot_token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    received = pairs.pop("hash", "")
    if not received:
        return None
    check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        if (now or int(time.time())) - int(pairs.get("auth_date", "0")) > max_age:
            return None
        user = json.loads(pairs.get("user", ""))
    except (ValueError, TypeError):
        return None
    return user if isinstance(user, dict) and isinstance(user.get("id"), int) else None
