# -*- coding: utf-8 -*-
"""Разложить окна VS Code по экрану — для горячей клавиши (ярлык в меню
«Пуск» с сочетанием клавиш). Без аргумента — VS Code, иначе имя процесса:
tile_code.py chrome."""

import sys

import windows

exe = (sys.argv[1] if len(sys.argv) > 1 else "code").lower()
windows.tile([w for w in windows.list_windows() if w.exe == exe])
