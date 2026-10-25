# -*- coding: utf-8 -*-
"""Архив Джарвиса для друга — без личных данных, и выпуск версии на GitHub.

Берём только код, страницу панели, расширение для ютуба и установщик.
Не берём: ключ Gemini, config.json (там свои программы и пути), выученные
ослышки, журнал, найденные игры, окружение Python и модели распознавания —
установщик скачает и соберёт всё сам на месте.

    venv\\Scripts\\python.exe package.py            — архив на рабочий стол
    venv\\Scripts\\python.exe package.py --release  — и выпустить: друг обновится сам
    venv\\Scripts\\python.exe package.py --friend   — архив для первой установки у
                                                     друга, с ключом обновлений внутри

Копия каждой версии остаётся в versions\\ — понять потом, что было в какой.
Ключ для друга (на чтение репозитория) хранится в keys.dat под именем
friend_github; в репозиторий, релиз и versions\\ он не попадает.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import version

HOME = Path(__file__).resolve().parent
VERSIONS = HOME / "versions"

FILES = [
    "jarvis.py", "skills.py", "speech.py", "brain.py", "ai.py", "keys.py", "gemini.py", "games.py",
    "memory.py", "info.py", "reminders.py", "version.py", "browser.py", "perf.py",
    "windows.py", "youtube_bridge.py", "panel.py", "open_panel.py", "package.py", "tile_code.py",
    "updater.py", "update.py",
    "selftest.py", "install.ps1", "Установить.bat", "start-jarvis.vbs",
    "requirements.txt", "README.md", "config.example.json", ".gitignore", "jarvis.ico",
]
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


def build(target=None, extra=None):
    """extra — {имя: текст}: добавить в архив нарочно (ключ обновлений для друга)."""
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
    with zipfile.ZipFile(target) as z:
        leaked = [n for n in z.namelist() if Path(n).name in PRIVATE and Path(n).name not in extra]
    if leaked:
        target.unlink()
        raise RuntimeError(f"в архив попало личное: {leaked}")
    if keep:
        VERSIONS.mkdir(exist_ok=True)
        shutil.copy2(target, VERSIONS / target.name)
    return target


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
                 extra={"update.key": token + "\n"})


if __name__ == "__main__":
    if "--friend" in sys.argv:
        print(friend())
    else:
        built = build()
        print(built)
        if "--release" in sys.argv:
            print(release(built))
