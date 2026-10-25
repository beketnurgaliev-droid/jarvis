import json, os, sys
os.chdir(r"C:\Users\beket\projects\jarvis")
sys.path.insert(0, os.getcwd())
import games, skills, windows

cfg = json.load(open("config.json", encoding="utf-8"))
games.refresh(cfg)
skills.index_programs()

wins = windows.list_windows()
print("Открытые окна:")
for w in wins:
    print("  ", w.exe, "|", w.title[:60])

# ничего не трогаем по-настоящему — только смотрим, что было бы сделано
done = []
windows.focus = lambda w: done.append(("вперёд", w.exe))
windows.minimize = lambda w: done.append(("свернуть", w.exe))
windows.close = lambda w: done.append(("закрыть", w.exe))
windows.minimize_all = lambda: done.append(("свернуть всё",))
skills.launch = lambda t: done.append(("запуск", t))
skills.game_mode_on = lambda cfg, closing: f"(включил бы, закрыв {list(closing)})"

for phrase in ["переключись на хром", "перейди в телеграм", "сверни дискорд", "сверни все",
               "закрой код", "переключись на один эс", "закрой окно", "покажи стим",
               "переключись на дискорд", "игровой режим", "обычный режим", "закрой блаблабла"]:
    done.clear()
    r = skills.handle(phrase, cfg)
    print(f"{phrase:26} -> {(r.ask or r.say) if r else '(агенту)'}  {done}")

print("Схема питания сейчас:", skills._power_scheme())
