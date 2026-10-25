# -*- coding: utf-8 -*-
"""Мост к расширению Chrome «Джарвис для ютуба» (папка chrome-extension).

Громкость внутри плеера ютуба снаружи Windows не достать — только кодом на
самой странице. Расширение подключается к Джарвису по WebSocket
(ws://127.0.0.1:8765), получает команды и выполняет их в плеере.
"""

import asyncio
import itertools
import json
import logging
import threading
from pathlib import Path

log = logging.getLogger("jarvis.youtube")

PORT = 8765
PING_EVERY = 20     # с: без сообщений Chrome усыпляет фон расширения через 30 с
MANIFEST = Path(__file__).resolve().parent / "chrome-extension" / "manifest.json"

_loop = None
_client = None      # соединение с расширением — последнее подключившееся
_waiting = {}       # номер команды -> Future с ответом
_ids = itertools.count(1)


def start(port=PORT):
    """Поднять сервер в своём потоке. Повторный вызов ничего не делает."""
    global _loop
    if _loop is not None:
        return
    _loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run():
        asyncio.set_event_loop(_loop)
        try:
            _loop.run_until_complete(_serve(port, ready))
        except Exception:
            log.exception("Мост к расширению ютуба не поднялся")
        finally:
            ready.set()

    threading.Thread(target=run, daemon=True, name="youtube-bridge").start()
    ready.wait(5)


async def _serve(port, ready):
    from websockets.asyncio.server import serve

    async with serve(_handler, "127.0.0.1", port) as server:
        log.info("Жду расширение ютуба на порту %d", port)
        ready.set()
        await server.serve_forever()


async def _handler(ws):
    global _client
    # подключиться может любая программа или страница — пускаем только расширения
    origin = ws.request.headers.get("Origin", "")
    if not origin.startswith("chrome-extension://"):
        log.warning("Чужое подключение к мосту ютуба (%s) — закрываю", origin or "без Origin")
        await ws.close(1008, "only extension")
        return
    _client = ws
    log.info("Расширение ютуба подключилось")
    pinger = asyncio.create_task(_ping(ws))
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("type") == "hello":
                await _check_version(ws, msg.get("version"))
                continue
            fut = _waiting.pop(msg.get("id"), None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
    except Exception as exc:
        log.info("Связь с расширением ютуба оборвалась: %s", exc)
    finally:
        pinger.cancel()
        if _client is ws:
            _client = None
            log.info("Расширение ютуба отключилось")


async def _check_version(ws, version):
    """Распакованное расширение Chrome сам не перечитывает — после обновления
    файлов просим его перезагрузиться."""
    try:
        disk = json.loads(MANIFEST.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        return
    log.info("Расширение ютуба: версия %s", version)
    if version and disk and version != disk:
        log.info("Расширение ютуба версии %s, на диске %s — прошу перезагрузиться", version, disk)
        await ws.send(json.dumps({"type": "reload"}))


async def _ping(ws):
    while True:
        await asyncio.sleep(PING_EVERY)
        await ws.send(json.dumps({"type": "ping"}))


async def _call(cmd, timeout):
    ws = _client
    if ws is None:
        return None
    n = next(_ids)
    fut = asyncio.get_running_loop().create_future()
    _waiting[n] = fut
    try:
        await ws.send(json.dumps({"id": n, **cmd}, ensure_ascii=False))
        return await asyncio.wait_for(fut, timeout)
    except asyncio.TimeoutError:
        log.warning("Расширение ютуба не ответило за %.0f с", timeout)
        return None
    except Exception:
        log.exception("Не смог передать команду расширению ютуба")
        return None
    finally:
        _waiting.pop(n, None)


def connected():
    return _client is not None


def call(cmd, timeout=3.0):
    """Отправить команду расширению и дождаться ответа. Нет связи — None."""
    if _loop is None or _client is None:
        return None
    try:
        return asyncio.run_coroutine_threadsafe(_call(cmd, timeout), _loop).result(timeout + 1)
    except Exception:
        log.exception("Мост к расширению ютуба")
        return None
