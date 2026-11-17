# -*- coding: utf-8 -*-
"""Всё, что не попало в локальные команды, уходит сюда — к подключённым ИИ
(ai.py) по очереди, сверху вниз. Кто первый ответил, тот и отвечает.

ИИ без поиска в интернете на вопросы про свежие данные отвечает «НУЖЕН ПОИСК»
— тогда вопрос уходит тому, кто искать умеет.

По умолчанию всё ТОЛЬКО НА ЧТЕНИЕ: отвечает, смотрит, ищет — но ничего не
меняет на компьютере. Распознавание ошибается, а голосом отдаются поручения,
которые могут дойти до прод-серверов и баз 1С: ослышка не должна становиться
действием. Режим действий есть только у Claude Code ("allow_actions": true в
его настройках в config.json) — тогда Джарвис переспрашивает вслух.
"""

import collections
import logging
import re
import threading
import time

import ai
import memory

log = logging.getLogger("jarvis.brain")

# недавний разговор — чтобы понимать продолжения («а завтра?»). Только в
# памяти процесса, на диск не пишется
_history = collections.deque(maxlen=6)
_history_lock = threading.Lock()
HISTORY_TTL = 300           # с: дольше тишина — разговор начат заново

SYSTEM = (
    "Ты — Джарвис, голосовой помощник у компьютера. Твой ответ зачитает вслух "
    "синтезатор речи, поэтому: отвечай по-русски, одно-два коротких предложения, "
    "обычным текстом. Никакого markdown, списков, кода, ссылок и длинных путей. "
    "Если выполнил действие — скажи об этом одной фразой. Если не смог — скажи "
    "коротко, почему. Не переспрашивай без крайней необходимости. "
    "Обращайся к собеседнику «сэр», сдержанно и без лишней услужливости — "
    "но не в каждой фразе, иначе выходит навязчиво. "
    "Отвечай быстро: файлы на диске не смотри, если об этом прямо не просят; "
    "в сети — не больше одного поиска, страницы открывай только если без этого "
    "никак. Фраза похожа на обрывок или ослышку и не похожа ни на одну команду "
    "Джарвиса — сразу скажи, что не расслышал."
)

SEARCH_MARK = "НУЖЕН ПОИСК"
NO_SEARCH = (
    "Доступа в интернет у тебя нет. Если для ответа нужны свежие данные из сети "
    "(погода, новости, курсы валют, цены, расписания, результаты матчей, события "
    f"последних месяцев) — ответь ровно двумя словами: {SEARCH_MARK}."
)


def agent_conf(cfg):
    """Общее для всех ИИ: фраза «секунду» и когда её говорить, короткие фразы."""
    return cfg.get("agent", {})


def needs_confirmation(cfg):
    """Включён режим действий — перед каждым поручением переспрашиваем вслух."""
    return any(p.get("type") == "claude_code" and p.get("enabled", True)
               and p.get("allow_actions") for p in ai.providers(cfg))


def ready(cfg):
    """Кого можно спрашивать, по порядку. В режиме действий — только тех, кто
    умеет действовать (Claude Code с разрешением)."""
    out = [p for p in ai.providers(cfg) if p.get("enabled", True) and ai.status(p)["ready"]]
    if needs_confirmation(cfg):
        out = [p for p in out if p.get("type") == "claude_code" and p.get("allow_actions")]
    return out


def available(cfg):
    return bool(ready(cfg))


def clean(text):
    """Убрать то, что синтезатор прочитает как мусор: список источников,
    звёздочки markdown, ссылки."""
    text = re.split(r"\n?\s*(?:Sources|Источники)\s*:", text)[0]
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_#`]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def note(said, answer):
    """Запомнить реплику и ответ — и локальной команды, и ИИ."""
    if said and answer:
        with _history_lock:
            _history.append((time.monotonic(), said, answer))


def _recent():
    with _history_lock:
        now = time.monotonic()
        while _history and now - _history[-1][0] > HISTORY_TTL:
            _history.clear()
        turns = list(_history)
    if not turns:
        return ""
    lines = "\n".join(f"Собеседник: {s}\nДжарвис: {a}" for _, s, a in turns)
    return ("Недавний разговор (новая фраза может его продолжать — тогда отвечай с "
            "учётом сказанного):\n" + lines)


def _system(cfg, context):
    parts = [SYSTEM, memory.prompt(), _recent(), context]
    if cfg.get("memory", {}).get("auto", True):
        parts.append(memory.AUTO_RULE)
    return "\n\n".join(p for p in parts if p)


def _remember(text, cfg):
    """Ответ ИИ мог закончиться строкой «ЗАПОМНИТЬ: …» — сохранить и убрать."""
    text, found = memory.extract(text)
    if cfg.get("memory", {}).get("auto", True):
        for fact in found:
            memory.add(fact, source="auto")
    return text


def ask(prompt, cfg, context=""):
    """Вопрос подключённым ИИ — короткий ответ для озвучки.
    context — что полезно знать о компьютере (команды Джарвиса, игры)."""
    system = _system(cfg, context)
    actions = needs_confirmation(cfg)
    need_search, errors = False, []
    for p in ready(cfg):
        if need_search and not p.get("search"):
            continue
        log.info("Спрашиваю %s: %s", p["name"], prompt)
        text, err = ai.ask(p, prompt, system if p.get("search") else system + "\n\n" + NO_SEARCH,
                           actions)
        if text and SEARCH_MARK in text.upper() and not p.get("search"):
            log.info("%s: нужен поиск в сети — дальше по списку", p["name"])
            need_search = True
            continue
        if text:
            log.info("Ответ %s: %s", p["name"], text[:300])
            return clean(_remember(text, cfg)) or "Готово"
        log.warning("%s не ответил: %s", p["name"], err)
        errors.append(f"{p['name']} — {err}")
    if need_search:
        return "Для этого нужен поиск в интернете, а подключённого ИИ с поиском нет, сэр"
    if errors:
        return f"Не получилось: {errors[0]}"
    return "Мне некого спросить, сэр. Подключите ИИ в панели, во вкладке «ИИ»"


TASK_SYSTEM = (
    "Ты — Джарвис, голосовой помощник у компьютера. Ответ зачитает вслух синтезатор "
    "речи: по-русски (если задание не просит другой язык), обычным текстом, без "
    "markdown, списков, таблиц, ссылок и кода. Делай ровно то, что просят, без "
    "вступлений вроде «Конечно» и без пояснений о себе."
)


def task(prompt, cfg, image=None, fast=False, max_tokens=1200, timeout=15):
    """Задание посложнее одной фразы: перевести выделенное, разобрать снимок
    экрана, расставить знаки в диктовке. Вернуть сырой текст (для озвучки —
    через clean) или "" — никто не смог.

    image — ai.image(): тогда спрашиваем только тех, кто видит картинки.
    fast — только быстрых (по ключу): диктовку ждут, пока печатается.
    Содержимое (выделенный текст, ответ по экрану) в журнал не пишем — там
    бывают пароли и чужие данные; только кто отвечал и сколько."""
    for p in ready(cfg):
        if image and not ai.sees_images(p):
            continue
        if fast and p.get("type") in ("claude_code", "cli"):
            continue
        # бесплатному Gemini для коротких ответов хватает 3 с, а картинке или
        # абзацу перевода — нет
        conf = dict(p, search=False, max_tokens=max(p.get("max_tokens", 0), max_tokens),
                    timeout=max(p.get("timeout", 0), timeout))
        log.info("Задание для %s%s", p["name"], " (с картинкой)" if image else "")
        text, err = ai.ask(conf, prompt, TASK_SYSTEM, False, image)
        if text:
            return text.strip()         # как есть: диктовке нужны переводы строк
        log.warning("%s не справился: %s", p["name"], err)
    return ""


def complete(prompt, cfg):
    """Служебный запрос без голосовых правил — например, подобрать русские
    названия игр. Вернуть сырой текст или пустую строку."""
    for p in ready(cfg):
        text, _err = ai.ask(dict(p, search=False, readonly_tools=""), prompt, "")
        if text:
            return text
    return ""
