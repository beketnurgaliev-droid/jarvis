# -*- coding: utf-8 -*-
"""ИИ, которых Джарвис спрашивает, когда команда не из его списка.

Подключить можно то, что есть у человека: бесплатный ключ (Gemini, Groq,
OpenRouter), платный ключ (OpenAI, DeepSeek, Claude API), подписку через
программу (Claude Code, Codex CLI для ChatGPT, Gemini CLI) или свою модель на
компьютере (Ollama). В config.json это список "ai" по порядку: кто выше, того
спрашивают первым. Ключи лежат в keys.py — зашифрованы, в config.json их нет.

Виды ("type"):
  gemini       — Google AI Studio по ключу (gemini.py)
  openai       — любой OpenAI-совместимый адрес: OpenAI, Groq, OpenRouter,
                 DeepSeek, Ollama, LM Studio…
  anthropic    — Claude API по ключу (официальный SDK anthropic)
  claude_code  — подписка Claude через программу Claude Code
  cli          — другая программа, которая отвечает на вопрос в командной
                 строке (Codex CLI, Gemini CLI)
"""

import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import gemini
import keys

log = logging.getLogger("jarvis.ai")

HOME = Path(__file__).resolve().parent
# папка агента — пустая, чтобы Claude Code не подхватывал чужие CLAUDE.md
AGENT_HOME = HOME / "agent-home"
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010

_NODE = ("if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { "
         "winget install -e --id OpenJS.NodeJS.LTS --accept-source-agreements "
         "--accept-package-agreements; $env:Path += ';' + $env:ProgramFiles + '\\nodejs' }; ")

# Что можно подключить — панель показывает это в «+ Подключить ИИ».
# group: free — бесплатный ключ, key — платный ключ, sub — подписка, own — своё
PRESETS = [
    {"preset": "gemini", "type": "gemini", "group": "free", "name": "Gemini",
     "badge": "бесплатный ключ", "key_url": "https://aistudio.google.com/apikey",
     "about": "Ключ Google AI Studio — бесплатно, отвечает за секунду. Подписка Gemini "
              "для этого не нужна. Искать в интернете на бесплатном ключе не умеет.",
     "model": gemini.DEFAULT_MODEL, "backup_models": list(gemini.BACKUP_MODELS),
     "thinking": "minimal", "search": False, "timeout": 3},
    {"preset": "groq", "type": "openai", "group": "free", "name": "Groq",
     "badge": "бесплатный ключ", "key_url": "https://console.groq.com/keys",
     "about": "Бесплатный ключ, очень быстрые открытые модели: Llama, Qwen и другие.",
     "base_url": "https://api.groq.com/openai/v1", "search": False, "timeout": 8},
    {"preset": "openrouter", "type": "openai", "group": "free", "name": "OpenRouter",
     "badge": "есть бесплатные модели", "key_url": "https://openrouter.ai/keys",
     "about": "Один ключ — сотни моделей: GPT, Claude, Gemini, DeepSeek, Llama. "
              "Модели с пометкой «:free» бесплатны.",
     "base_url": "https://openrouter.ai/api/v1", "search": False, "timeout": 15},
    {"preset": "openai", "type": "openai", "group": "key", "name": "ChatGPT (ключ API)",
     "badge": "платный ключ", "key_url": "https://platform.openai.com/api-keys",
     "about": "Ключ API OpenAI с пополненным балансом. Подписка ChatGPT Plus сюда не "
              "подходит — для неё «ChatGPT по подписке».",
     "base_url": "https://api.openai.com/v1", "search": False, "timeout": 15},
    {"preset": "deepseek", "type": "openai", "group": "key", "name": "DeepSeek",
     "badge": "недорогой ключ", "key_url": "https://platform.deepseek.com/api_keys",
     "about": "Недорогой ключ, хорошо понимает русский.",
     "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
     "search": False, "timeout": 15},
    {"preset": "anthropic", "type": "anthropic", "group": "key", "name": "Claude (ключ API)",
     "badge": "платный ключ", "key_url": "https://console.anthropic.com/settings/keys",
     "about": "Ключ API Anthropic с пополненным балансом. Умеет искать в интернете. "
              "Подписка Claude Pro сюда не подходит — для неё «Claude по подписке».",
     "model": "claude-opus-5-5", "search": True, "timeout": 30},
    {"preset": "claude_code", "type": "claude_code", "group": "sub",
     "name": "Claude по подписке", "badge": "Claude Pro / Max",
     "about": "Через программу Claude Code: установить, войти своим аккаунтом Claude, "
              "проверить. Отвечает медленнее (3–10 с), зато ищет в интернете.",
     "install": "irm https://claude.ai/install.ps1 | iex",
     "search": True, "timeout": 240, "effort": "low",
     "readonly_tools": "Read Glob Grep WebSearch WebFetch",
     "action_tools": "Read Glob Grep WebSearch WebFetch Bash Edit Write"},
    {"preset": "codex", "type": "cli", "group": "sub", "name": "ChatGPT по подписке",
     "badge": "ChatGPT Plus / Pro", "experimental": True,
     "about": "Через программу Codex CLI от OpenAI: установить, войти аккаунтом ChatGPT, "
              "проверить. Для установки нужен Node.js — поставится сам. Медленнее, чем "
              "по ключу.",
     "command": ["codex", "exec", "--skip-git-repo-check", "{prompt}"],
     "install": _NODE + "npm install -g @openai/codex",
     "login": ["codex", "login"], "search": False, "timeout": 90},
    {"preset": "gemini_cli", "type": "cli", "group": "sub", "name": "Gemini по подписке",
     "badge": "Google AI Pro", "experimental": True,
     "about": "Через программу Gemini CLI: установить, войти аккаунтом Google — "
              "работают лимиты подписки. Медленнее, чем по ключу, зато ищет в интернете.",
     "command": ["gemini", "-p", "{prompt}"],
     "install": _NODE + "npm install -g @google/gemini-cli",
     "login": ["gemini"], "search": True, "timeout": 90},
    {"preset": "ollama", "type": "openai", "group": "own", "name": "Ollama",
     "badge": "без интернета", "no_key": True,
     "about": "Своя модель на этом компьютере через Ollama (ollama.com) — бесплатно и "
              "без интернета, но нужна мощная видеокарта.",
     "base_url": "http://localhost:11434/v1", "search": False, "timeout": 30},
    {"preset": "custom", "type": "openai", "group": "own", "name": "Свой адрес",
     "badge": "OpenAI-совместимый", "edit_url": True,
     "about": "Любой сервис с OpenAI-совместимым API: LM Studio, YandexGPT, Mistral, "
              "xAI и другие. Укажите адрес, ключ и модель.",
     "base_url": "", "search": False, "timeout": 20},
]
PRESET = {p["preset"]: p for p in PRESETS}
# поля пресета, которые попадают в настройки самого подключения
_COPY = ("type", "name", "model", "backup_models", "thinking", "search", "timeout",
         "base_url", "command", "effort", "readonly_tools", "action_tools")


def providers(cfg):
    return cfg.setdefault("ai", [])


def preset_of(p):
    return PRESET.get(p.get("preset"), {})


def add(cfg, preset_id):
    """Подключить новый ИИ из пресета, в конец списка. Вернуть его."""
    preset = PRESET[preset_id]
    taken = {p["id"] for p in providers(cfg)}
    pid, n = preset_id, 2
    while pid in taken:
        pid, n = f"{preset_id}-{n}", n + 1
    item = {"id": pid, "preset": preset_id, "enabled": True}
    item.update({k: preset[k] for k in _COPY if k in preset})
    providers(cfg).append(item)
    return item


def needs_key(p):
    return p.get("type") in ("gemini", "anthropic") or (
        p.get("type") == "openai" and not preset_of(p).get("no_key"))


# --------------------------------------------------------------------------
# Готов ли
# --------------------------------------------------------------------------

def find_claude(configured=""):
    """claude.exe: путь из настроек, PATH, обычные места установки."""
    if configured and Path(configured).exists():
        return configured
    found = shutil.which("claude")
    if found:
        return found
    for candidate in (
        Path.home() / ".local/bin/claude.exe",
        Path(os.environ.get("APPDATA", "")) / "npm/claude.cmd",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/claude/claude.exe",
    ):
        if candidate.exists():
            return str(candidate)
    return ""


def _cli_exe(p):
    cmd = p.get("command") or []
    return shutil.which(cmd[0]) if cmd else None


def status(p):
    """Готов ли к работе и чего не хватает: need — key / url / model / install."""
    t = p.get("type")
    if t in ("gemini", "anthropic", "openai"):
        if t == "openai" and not p.get("base_url"):
            return {"ready": False, "text": "нужен адрес", "need": "url"}
        if needs_key(p) and not keys.get(p["id"]):
            return {"ready": False, "text": "нужен ключ", "need": "key"}
        if t == "openai" and not p.get("model"):
            return {"ready": False, "text": "выберите модель", "need": "model"}
        return {"ready": True, "text": "готов", "need": None}
    if t == "claude_code":
        if find_claude(p.get("exe", "")):
            return {"ready": True, "text": "установлен", "need": None}
        return {"ready": False, "text": "не установлен", "need": "install"}
    if t == "cli":
        if _cli_exe(p):
            return {"ready": True, "text": "установлен", "need": None}
        return {"ready": False, "text": "не установлен", "need": "install"}
    return {"ready": False, "text": "неизвестный вид", "need": None}


# --------------------------------------------------------------------------
# Спросить
# --------------------------------------------------------------------------

def ask(p, prompt, system, actions=False):
    """Вернуть (текст, None) или (None, причина)."""
    started = time.monotonic()
    try:
        text, err = _ASK[p["type"]](p, prompt, system, actions)
    except Exception as exc:
        log.exception("%s упал", p.get("name"))
        return None, f"ошибка: {exc}"
    log.info("%s: %s за %.1f с", p.get("name"), "ответ" if text else err,
             time.monotonic() - started)
    return text, err


def _ask_gemini(p, prompt, system, _actions):
    return gemini.ask(prompt, system, p, keys.get(p["id"]))


def _post_json(url, body, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url, headers, timeout=10):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


_HTTP_REASON = {400: "запрос не принят", 401: "ключ не подходит", 402: "закончились деньги на счёте",
                403: "доступ запрещён", 404: "такой модели или адреса нет",
                429: "кончился лимит запросов", 500: "сервис сломался",
                502: "сервис недоступен", 503: "сервис перегружен"}


def _openai_headers(p):
    # без своего User-Agent Cloudflare (Groq и др.) отвечает 403 «error code: 1010»
    headers = {"Content-Type": "application/json", "User-Agent": "Jarvis-voice-assistant/1.0"}
    key = keys.get(p["id"])
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _ask_openai(p, prompt, system, _actions):
    base = (p.get("base_url") or "").rstrip("/")
    messages = ([{"role": "system", "content": system}] if system else []) + \
        [{"role": "user", "content": prompt}]
    # у новых моделей OpenAI свои имена параметров — пробуем по очереди
    attempts = [
        {"model": p.get("model"), "messages": messages, "max_tokens": p.get("max_tokens", 400),
         "temperature": 0.3},
        {"model": p.get("model"), "messages": messages,
         "max_completion_tokens": p.get("max_tokens", 2000)},
    ]
    for body in attempts:
        try:
            data = _post_json(base + "/chat/completions", body, _openai_headers(p),
                              p.get("timeout", 15))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            log.warning("%s: HTTP %s: %s", p.get("name"), exc.code, detail)
            if exc.code == 400 and body is attempts[0]:
                continue
            return None, _HTTP_REASON.get(exc.code, f"ошибка HTTP {exc.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log.warning("%s недоступен: %s", p.get("name"), exc)
            return None, "нет связи или не уложился во время"
        msg = ((data.get("choices") or [{}])[0].get("message") or {})
        # «думающие» модели (DeepSeek R1, Qwen) пишут рассуждение в <think>
        text = re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S).strip()
        return (text, None) if text else (None, "пустой ответ")
    return None, "запрос не принят"


# модели, у которых на отказ есть запасная модель на стороне Anthropic
_CLAUDE_FALLBACK = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
# поддерживают output_config.effort
_CLAUDE_EFFORT = ("claude-fable-5", "claude-opus-5", "claude-sonnet-5", "claude-haiku-5",
                  "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6")
# поиск с фильтрацией — только у новых старших моделей, остальным — простой
_CLAUDE_NEW_SEARCH = ("claude-opus-5", "claude-sonnet-5", "claude-opus-4-8", "claude-opus-4-7",
                      "claude-opus-4-6", "claude-sonnet-4-6")


def _ask_anthropic(p, prompt, system, _actions):
    import anthropic

    model = p.get("model") or "claude-opus-5-5"
    client = anthropic.Anthropic(api_key=keys.get(p["id"]), timeout=p.get("timeout", 30),
                                 max_retries=1)
    kwargs = {"model": model, "max_tokens": 2048, "system": system,
              "messages": [{"role": "user", "content": prompt}]}
    if model.startswith(_CLAUDE_EFFORT):
        kwargs["output_config"] = {"effort": "low"}     # голосу хватает короткого ответа
    if p.get("search"):
        kwargs["tools"] = [{"type": "web_search_20260209" if model.startswith(_CLAUDE_NEW_SEARCH)
                            else "web_search_20250305", "name": "web_search", "max_uses": 1}]
    try:
        if model in _CLAUDE_FALLBACK:
            # если модель откажется отвечать, Anthropic сам переспросит запасную
            resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"],
                                               fallbacks="default", **kwargs)
        else:
            resp = client.messages.create(**kwargs)
    except anthropic.AuthenticationError:
        return None, "ключ не подходит"
    except anthropic.PermissionDeniedError:
        return None, "доступ запрещён"
    except anthropic.NotFoundError:
        return None, "такой модели нет"
    except anthropic.RateLimitError:
        return None, "кончился лимит запросов"
    except anthropic.BadRequestError as exc:
        return None, f"запрос не принят: {exc.message}"[:200]
    except anthropic.APIStatusError as exc:
        return None, f"ошибка Claude {exc.status_code}"
    except anthropic.APIConnectionError:
        return None, "нет связи или не уложился во время"
    if resp.stop_reason == "refusal":
        return "На это я не отвечу, сэр.", None
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    return (text, None) if text else (None, "пустой ответ")


def _ask_claude_code(p, prompt, system, actions):
    exe = find_claude(p.get("exe", ""))
    if not exe:
        return None, "Claude Code не установлен"
    tools = p.get("action_tools" if actions else "readonly_tools",
                  "Read Glob Grep WebSearch WebFetch")
    cmd = [exe, "-p", prompt, "--output-format", "text",
           # только эти инструменты вообще существуют для агента — иначе он
           # пробует Bash, упирается в запрет и тратит время на объяснения
           "--tools", ",".join(tools.split()),
           # не подключать MCP-серверы и навыки: на каждый вызов это секунды
           "--strict-mcp-config", "--disable-slash-commands"]
    if tools.strip():
        cmd += ["--allowedTools", tools]
    if system:
        cmd += ["--append-system-prompt", system]
    if p.get("model"):
        cmd += ["--model", p["model"]]
    if p.get("effort"):
        cmd += ["--effort", p["effort"]]
    AGENT_HOME.mkdir(exist_ok=True)
    try:
        proc = subprocess.run(cmd, cwd=str(AGENT_HOME), capture_output=True,
                              timeout=p.get("timeout", 240), creationflags=CREATE_NO_WINDOW,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, "не уложился во время"
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        log.error("Claude Code: код %s: %s", proc.returncode, err[:500])
        if re.search(r"log ?in|logged|auth|credential|/login", (err + out).lower()):
            return None, "не выполнен вход — нажмите «Войти в аккаунт»"
        return None, f"ошибка (код {proc.returncode})"
    return (out, None) if out else (None, "пустой ответ")


def _flat(text):
    """Одной строкой и без символов, которые портит командная строка
    (программы из npm запускаются через cmd)."""
    return re.sub(r'["%^\r\n]+', " ", text).strip()


def _ask_cli(p, prompt, system, _actions):
    exe = _cli_exe(p)
    if not exe:
        return None, "программа не установлена"
    text = _flat(f"{system}\n\nВопрос: {prompt}" if system else prompt)
    args = [exe] + [text if a == "{prompt}" else a for a in p["command"][1:]]
    AGENT_HOME.mkdir(exist_ok=True)
    try:
        proc = subprocess.run(args, cwd=str(AGENT_HOME), capture_output=True,
                              timeout=p.get("timeout", 90), creationflags=CREATE_NO_WINDOW,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, "не уложился во время"
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        log.error("%s: код %s: %s", p.get("name"), proc.returncode, err[:500])
        if re.search(r"log ?in|auth|credential|sign ?in", (err + out).lower()):
            return None, "не выполнен вход — нажмите «Войти в аккаунт»"
        return None, f"ошибка (код {proc.returncode})"
    return (out, None) if out else (None, "пустой ответ")


_ASK = {"gemini": _ask_gemini, "openai": _ask_openai, "anthropic": _ask_anthropic,
        "claude_code": _ask_claude_code, "cli": _ask_cli}


# --------------------------------------------------------------------------
# Список моделей, установка, вход
# --------------------------------------------------------------------------

def models(p):
    t = p.get("type")
    if t == "gemini":
        return gemini.models(keys.get(p["id"]))
    if t == "openai":
        data = _get_json((p.get("base_url") or "").rstrip("/") + "/models", _openai_headers(p))
        names = sorted({m.get("id", "") for m in data.get("data", []) if m.get("id")})
        # бесплатные — первыми (у OpenRouter их помечают «:free»)
        return sorted(names, key=lambda n: (not n.endswith(":free"), n))
    if t == "anthropic":
        import anthropic

        client = anthropic.Anthropic(api_key=keys.get(p["id"]), timeout=10, max_retries=1)
        return [m.id for m in client.models.list()]
    return []


def install(p):
    """Открыть окно PowerShell с установкой программы — видно, как идёт."""
    command = preset_of(p).get("install")
    if not command:
        raise ValueError("Этому ИИ ничего ставить не нужно")
    subprocess.Popen(["powershell", "-NoExit", "-ExecutionPolicy", "Bypass", "-Command",
                      f"Write-Host 'Устанавливаю {p.get('name')}…'; {command}; "
                      "Write-Host ''; Write-Host 'Готово. Окно можно закрыть и нажать «Войти в аккаунт».'"],
                     creationflags=CREATE_NEW_CONSOLE)


def login(p):
    """Открыть окно с программой — первый запуск сам попросит войти в аккаунт."""
    if p.get("type") == "claude_code":
        exe = find_claude(p.get("exe", ""))
        args = [exe] if exe else []
    else:
        cmd = preset_of(p).get("login") or []
        exe = shutil.which(cmd[0]) if cmd else None
        args = [exe] + cmd[1:] if exe else []
    if not args:
        raise ValueError("Сначала установите программу")
    subprocess.Popen(["cmd", "/k"] + args, cwd=str(HOME), creationflags=CREATE_NEW_CONSOLE)


# --------------------------------------------------------------------------
# Переход со старых настроек (разделы "gemini" и "claude")
# --------------------------------------------------------------------------

def migrate(cfg):
    """Старый config.json → список "ai" и раздел "agent". Вернуть True, если
    что-то поменяли (тогда настройки надо сохранить)."""
    if "ai" in cfg:
        return False
    items = []
    g = cfg.pop("gemini", None)
    if g is not None or keys.get("gemini"):
        g = g or {}
        item = {"id": "gemini", "preset": "gemini", "type": "gemini", "name": "Gemini",
                "enabled": g.get("enabled", True)}
        for k in ("model", "backup_models", "thinking", "search", "timeout"):
            item[k] = g.get(k, PRESET["gemini"].get(k))
        items.append(item)
    c = cfg.pop("claude", None) or {}
    items.append({
        "id": "claude_code", "preset": "claude_code", "type": "claude_code",
        "name": "Claude по подписке", "enabled": c.get("enabled", True),
        "exe": c.get("exe", ""), "model": c.get("model", ""), "effort": c.get("effort", "low"),
        "timeout": c.get("timeout", 240), "search": True,
        "allow_actions": c.get("allow_actions", False),
        "readonly_tools": c.get("readonly_tools", PRESET["claude_code"]["readonly_tools"]),
        "action_tools": c.get("action_tools", PRESET["claude_code"]["action_tools"]),
    })
    cfg["ai"] = items
    cfg["agent"] = {"min_words": c.get("min_words", 2),
                    "thinking_phrase": c.get("thinking_phrase", "Секунду, разбираюсь"),
                    "thinking_delay": c.get("thinking_delay", 1.5)}
    log.info("Настройки ИИ переведены на новый вид: %s", [i["name"] for i in items])
    return True
