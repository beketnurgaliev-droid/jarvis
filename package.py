# -*- coding: utf-8 -*-
"""Архив Джарвиса для друга — без личных данных, и выпуск версии на GitHub.

Берём только код, страницу панели, расширение для ютуба и установщик.
Не берём: ключ Gemini, config.json (там свои программы и пути), выученные
ослышки, журнал, найденные игры, окружение Python и модели распознавания —
установщик скачает и соберёт всё сам на месте.

    venv\\Scripts\\python.exe package.py            — архив на рабочий стол
    venv\\Scripts\\python.exe package.py --release  — и выпустить: друг обновится сам
    venv\\Scripts\\python.exe package.py --friend   — архив для первой установки у
                                                     друга: со своим Python и ключом
    venv\\Scripts\\python.exe package.py --runtime  — собрать свой Python (runtime) и
                                                     выпустить его: у друга подтянется сам

Копия каждой версии остаётся в versions\\ — понять потом, что было в какой.
Ключ для друга (на чтение репозитория) хранится в keys.dat под именем
friend_github; в репозиторий, релиз и versions\\ он не попадает.

Свой Python (runtime.py): встроенный Python с python.org той же версии, что в
venv, + библиотеки из venv\\Lib\\site-packages. Собирается прямо в
runtime\\<runtime.WANTED> — на этой машине его можно сразу запустить и проверить.
Поменял библиотеки — новое имя runtime.WANTED и снова --runtime.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

import runtime
import version

HOME = Path(__file__).resolve().parent
VERSIONS = HOME / "versions"
BUILD = HOME / "build"

FILES = [
    "jarvis.py", "skills.py", "speech.py", "brain.py", "ai.py", "keys.py", "gemini.py", "games.py",
    "memory.py", "info.py", "reminders.py", "version.py", "browser.py", "perf.py",
    "look.py", "pcstatus.py", "dictation.py", "sendkeys.py",
    "windows.py", "youtube_bridge.py", "panel.py", "open_panel.py", "package.py", "tile_code.py",
    "updater.py", "update.py", "models.py", "runtime.py",
    "selftest.py", "install.ps1", "Установить.bat", "start-jarvis.vbs", "run.vbs",
    "requirements.txt", "README.md", "config.example.json", ".gitignore", "jarvis.ico",
]
# из venv в свой Python не берём: ставить пакеты ему не нужно (новые
# библиотеки — новый runtime целиком)
RUNTIME_SKIP = {"pip", "setuptools", "wheel", "_distutils_hack", "pkg_resources",
                "distutils-precedence.pth"}
DIRS = ["panel", "chrome-extension"]
# что в архив не должно попасть ни при каких условиях
PRIVATE = {"gemini.key", "keys.dat", "update.key", "config.json", "config.json.bak",
           "learned.json", "games.json", "jarvis.log", "panel.url", "memory.json",
           "reminders.json"}


def desktop():
    try:
        from win32com.shell import shell, shellcon

        return Path(shell.SHGetFolderPath(0, shellcon.CSIDL_DESKTOPDIRECTORY, None, 0))
    except Exception:
        return Path.home() / "Desktop"


def _runtime_files(name):
    """Файлы своего Python для архива: (путь на диске, путь в архиве от runtime/)."""
    root = runtime.ROOT / name
    for path in sorted(root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.name != "checked":
            yield path, f"runtime/{name}/{path.relative_to(root).as_posix()}"


def build(target=None, extra=None, with_runtime=False):
    """extra — {имя: текст}: добавить в архив нарочно (ключ обновлений для друга).
    with_runtime — положить и свой Python: тогда установщику Python в системе не нужен."""
    keep = target is None             # пробная сборка в свой путь — не версия
    target = Path(target) if target else desktop() / f"Джарвис-{version.VERSION}.zip"
    extra = extra or {}
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for name in FILES:
            path = HOME / name
            if path.exists():
                z.write(path, f"Jarvis/{name}")
        for folder in DIRS:
            for path in sorted((HOME / folder).rglob("*")):
                if path.is_file() and path.name not in PRIVATE and "__pycache__" not in path.parts:
                    z.write(path, f"Jarvis/{path.relative_to(HOME).as_posix()}")
        for name, text in extra.items():
            z.writestr(f"Jarvis/{name}", text)
        if with_runtime:
            build_runtime()
            for path, arc in _runtime_files(runtime.WANTED):
                z.write(path, f"Jarvis/{arc}")
            z.writestr("Jarvis/runtime/current.txt", runtime.WANTED)
    with zipfile.ZipFile(target) as z:
        # в своём Python у библиотек бывают свои config.json — это не личное
        leaked = [n for n in z.namelist() if Path(n).name in PRIVATE and Path(n).name not in extra
                  and not n.startswith("Jarvis/runtime/")]
    if leaked:
        target.unlink()
        raise RuntimeError(f"в архив попало личное: {leaked}")
    if keep:
        VERSIONS.mkdir(exist_ok=True)
        shutil.copy2(target, VERSIONS / target.name)
    return target


# --------------------------------------------------------------------------
# Свой Python (runtime)
# --------------------------------------------------------------------------

def build_runtime(name=None):
    """Собрать свой Python в runtime\\<name> (если ещё нет) и проверить. → папка."""
    name = name or runtime.WANTED
    target = runtime.ROOT / name
    if (target / "pythonw.exe").is_file():
        problem = runtime.check(name)
        if problem:
            raise RuntimeError(f"свой Python {name} не проходит проверку: {problem}")
        return target
    have = sys.version.split()[0]
    if have != runtime.PYTHON:
        raise RuntimeError(f"venv на Python {have}, а свой Python — {runtime.PYTHON}: "
                           "библиотеки собраны под другой")
    BUILD.mkdir(exist_ok=True)
    embed = BUILD / f"python-{runtime.PYTHON}-embed-amd64.zip"
    if not embed.exists():
        url = f"https://www.python.org/ftp/python/{runtime.PYTHON}/{embed.name}"
        print("скачиваю", url)
        req = urllib.request.Request(url, headers={"User-Agent": f"Jarvis-package/{version.VERSION}"})
        part = embed.with_suffix(".part")
        with urllib.request.urlopen(req, timeout=120) as r, part.open("wb") as f:
            shutil.copyfileobj(r, f)
        os.replace(part, embed)
    staging = BUILD / f"runtime-{name}"
    shutil.rmtree(staging, ignore_errors=True)
    with zipfile.ZipFile(embed) as z:
        z.extractall(staging)
    stdlib = next(staging.glob("python3*.zip")).name
    pth = next(staging.glob("python3*._pth"))
    # где искать модули: стандартная библиотека, библиотеки Джарвиса и сама
    # папка Джарвиса (runtime\<имя>\..\.. ) — там jarvis.py, skills.py и пр.
    pth.write_text(f"{stdlib}\n.\nLib\\site-packages\n..\\..\nimport site\n", encoding="ascii")
    src = HOME / "venv" / "Lib" / "site-packages"

    def ignore(folder, names):
        out = {n for n in names if n == "__pycache__" or n.endswith(".pyc")}
        if Path(folder) == src:
            out |= {n for n in names if n.split("-")[0].lower() in RUNTIME_SKIP
                    or n.lower() in RUNTIME_SKIP}
        return out

    shutil.copytree(src, staging / "Lib" / "site-packages", ignore=ignore)
    runtime.ROOT.mkdir(exist_ok=True)
    os.replace(staging, target)
    problem = runtime.check(name)
    if problem:
        raise RuntimeError(f"свой Python {name} собран, но не проходит проверку: {problem}")
    runtime.mark_checked(name)
    return target


def runtime_zip(name=None):
    """Архив своего Python для релиза → versions\\Jarvis-runtime-<name>.zip."""
    name = name or runtime.WANTED
    build_runtime(name)
    VERSIONS.mkdir(exist_ok=True)
    target = VERSIONS / runtime.asset_name(name)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for path, arc in _runtime_files(name):
            z.write(path, arc)
    return target


def release_runtime(name=None):
    """Свой Python — отдельным релизом runtime-<name>. «Последним» его не
    помечаем: releases/latest должен оставаться версией Джарвиса."""
    import updater

    name = name or runtime.WANTED
    path = runtime_zip(name)
    gh, _git, run = _runner()
    tag = runtime.tag(name)
    if run(gh, "release", "view", tag, "--repo", updater.REPO, check=False).returncode == 0:
        return f"https://github.com/{updater.REPO}/releases/tag/{tag} (уже был)"
    run(gh, "release", "create", tag, str(path), "--repo", updater.REPO,
        "--title", f"Свой Python для Джарвиса ({name})", "--latest=false",
        "--notes", "Встроенный Python с библиотеками Джарвиса. Джарвис скачивает его сам — "
                   "после этого Python в системе ему не нужен.")
    return f"https://github.com/{updater.REPO}/releases/tag/{tag}"


# --------------------------------------------------------------------------
# Выпуск на GitHub
# --------------------------------------------------------------------------

def _tool(name):
    """git и gh: из PATH или из %LOCALAPPDATA%\\Programs (ставились без админа)."""
    found = shutil.which(name)
    if found:
        return found
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs"
    for path in (base / "gh" / "bin" / "gh.exe", base / "MinGit" / "cmd" / "git.exe"):
        if path.stem == name and path.exists():
            return str(path)
    raise RuntimeError(f"не найден {name} — нужен для выпуска версии")


def _runner():
    gh, git = _tool("gh"), _tool("git")
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(Path(gh).parent), str(Path(git).parent), env.get("PATH", "")])

    def run(*cmd, check=True):
        r = subprocess.run(cmd, cwd=HOME, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if check and r.returncode != 0:
            raise RuntimeError(f"{Path(cmd[0]).stem} {cmd[1]}: {(r.stderr or r.stdout).strip()}")
        return r
    return gh, git, run


def release(path):
    """Код — в репозиторий, архив — релизом vX.Y. Джарвис у друга найдёт его
    сам (updater.py). Тот же номер второй раз не выпустить — сначала новая
    запись в version.py."""
    import updater

    gh, git, run = _runner()
    tag = f"v{version.VERSION}"
    if run(gh, "release", "view", tag, "--repo", updater.REPO, check=False).returncode == 0:
        raise RuntimeError(f"версия {version.VERSION} уже выпущена — добавь новую в version.py")
    run(git, "add", "-A")
    if run(git, "status", "--porcelain").stdout.strip():
        run(git, "commit", "-q", "-m", f"Версия {version.VERSION}")
    run(git, "tag", "-f", tag)
    run(git, "push", "-q", "origin", "HEAD")
    run(git, "push", "-q", "-f", "origin", tag)
    # имя файла в релизе — латиницей: русские буквы GitHub в именах заменяет
    with tempfile.TemporaryDirectory() as tmp:
        asset = Path(tmp) / f"Jarvis-{version.VERSION}.zip"
        shutil.copy2(path, asset)
        notes = "\n".join(f"- {item}" for item in version.CHANGES[0][2])
        run(gh, "release", "create", tag, str(asset), "--repo", updater.REPO,
            "--title", f"Джарвис {version.VERSION}", "--notes", notes, "--verify-tag")
    return f"https://github.com/{updater.REPO}/releases/tag/{tag}"


def friend():
    """Архив для первой установки у друга: как обычный, плюс update.key —
    ключ на чтение репозитория, чтобы дальше обновлялся сам."""
    import keys

    token = keys.get("friend_github")
    if not token:
        raise RuntimeError("нет ключа для друга: keys.put('friend_github', 'github_pat_…')")
    return build(desktop() / f"Джарвис-{version.VERSION}-для-друга.zip",
                 extra={"update.key": token + "\n"}, with_runtime=True)


if __name__ == "__main__":
    if "--runtime" in sys.argv:
        print(release_runtime())
    elif "--friend" in sys.argv:
        print(friend())
    else:
        built = build()
        print(built)
        if "--release" in sys.argv:
            print(release(built))
