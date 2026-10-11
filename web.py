"""Веб-сервер мини-приложения. Работает в одном процессе с ботом (см. bot.py).

Отдаёт страницу webapp/index.html и API. Кто пользователь, решает только проверенная
подпись Telegram в заголовке X-Telegram-Init-Data — присланному «от руки» id сервер не верит.
"""
import json
import logging
import os

from aiohttp import web

import api
import config
import webauth

log = logging.getLogger("web")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")


def build_app(on_event) -> web.Application:
    """on_event(kind, booking) — корутина бота: написать клиенту и владельцу."""

    async def handle_api(request: web.Request) -> web.Response:
        user = webauth.validate(request.headers.get("X-Telegram-Init-Data", ""), config.BOT_TOKEN)
        body = {}
        if request.method == "POST":
            try:
                body = json.loads(await request.text() or "{}")
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                body = {}
        try:
            status, payload, event = api.handle(request.method, request.path, dict(request.query), body, user)
        except Exception:
            log.exception("ошибка API %s %s", request.method, request.path)
            return web.json_response({"error": "Что-то пошло не так. Попробуйте ещё раз."}, status=500)
        if event:
            try:
                await on_event(*event)
            except Exception:
                log.exception("не удалось отправить уведомление о событии %s", event[0])
        return web.json_response(payload, status=status, headers={"Cache-Control": "no-store"})

    async def index(_: web.Request) -> web.Response:
        # Страница лежит в webapp/; если её по ошибке залили в корень репозитория — берём оттуда.
        for folder in (ROOT, os.path.dirname(ROOT)):
            path = os.path.join(folder, "index.html")
            if os.path.isfile(path):
                return web.FileResponse(path, headers={"Cache-Control": "no-cache"})
        log.error("не найден webapp/index.html — мини-приложение не откроется")
        return web.Response(status=404, text="Не найден файл webapp/index.html. Проверьте, что папка webapp залита в репозиторий.")

    async def asset(request: web.Request) -> web.Response:
        # Только файлы-картинки прямо из папки webapp: логотип и подобное.
        name = request.match_info["name"]
        for folder in (ROOT, os.path.dirname(ROOT)):
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                break
        else:
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "max-age=3600"})

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application(client_max_size=64 * 1024)
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get(r"/{name:[\w-]+\.(?:png|jpg|jpeg|webp|svg)}", asset)
    app.router.add_route("*", "/api/{tail:.*}", handle_api)
    os.makedirs(os.path.join(ROOT, "works"), exist_ok=True)
    app.router.add_static("/works/", os.path.join(ROOT, "works"))
    return app


async def start(on_event) -> web.AppRunner:
    runner = web.AppRunner(build_app(on_event), access_log=None)
    await runner.setup()
    # 0.0.0.0, а не 127.0.0.1: иначе хостинг не достучится до сервера снаружи контейнера.
    await web.TCPSite(runner, "0.0.0.0", config.PORT).start()
    log.info("мини-приложение слушает порт %s, адрес для Telegram: %s",
             config.PORT, config.WEBAPP_URL or "не задан (WEBAPP_URL)")
    return runner
