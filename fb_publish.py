#!/usr/bin/env python3
"""Публикатор FB-спринта. Работает НА РАННЕРЕ GitHub, не на нашей машине.

    python3 fb_publish.py --check     # что сегодня и всё ли на месте
    python3 fb_publish.py --dry       # показать запрос, не отправляя
    python3 fb_publish.py             # опубликовать то, что на сегодня

⛔ Зачем раннер: домены Meta из России закрыты, а раннеры GitHub ходят к ним
из США. Ровно по этой причине здесь живёт и `ig_publish.py`.

⚠️ Meta не принимает файл загрузкой — она СКАЧИВАЕТ медиа по адресу. Поэтому
и обложки, и ролики обязаны лежать на зеркале ДО запуска, а адрес приходит
в `fb_sprint.json`.

─── Защита от дубля, и почему она не «на всякий случай» ───

⛔ 21.08.2026 починенный токен молча включил очередь, собранную тремя неделями
раньше, и в ленту ушёл пост, решения о котором никто не принимал. Крон не
спрашивает, уместно ли сейчас то, что записано.

Поэтому успешная публикация ПРОСТАВЛЯЕТСЯ В САМОМ ЗАДАНИИ и коммитится обратно
в репозиторий. Второй запуск того же дня видит отметку и молчит. Отметка в
репозитории, а не в файле на раннере: раннер чистый при каждом запуске, и любой
журнал на нём умирает вместе с машиной.
"""
import datetime
import json
import os
import pathlib
import sys
import urllib.parse
import urllib.request

# ⛔ Задание ищем в `ig/fb/`: корень сайта каждое утро перетирается rsync-ом
# из репозитория подкаста, и отметки «опубликовано» там не пережили бы ночь.
ЗАДАНИЕ = pathlib.Path(__file__).resolve().parent / "ig" / "fb" / "fb_sprint.json"
API = "https://graph.facebook.com/v21.0"
ТОКЕН = os.environ.get("META_PAGE_TOKEN", "")
СТРАНИЦА = os.environ.get("FB_PAGE_ID", "")


def запрос(путь, **поля):
    поля["access_token"] = ТОКЕН
    данные = urllib.parse.urlencode(поля).encode()
    с = urllib.request.urlopen(f"{API}/{путь}", данные, timeout=180)
    return json.loads(с.read())


def сегодня_мск():
    """МСК = UTC+3. Раннер живёт в UTC, а расписание написано по-московски:
    в 07:00 UTC у нас уже 10:00, и день обязан считаться нашим, а не гринвичским."""
    сейчас = datetime.datetime.now(datetime.timezone.utc)
    return (сейчас + datetime.timedelta(hours=3)).date().isoformat()


def пункт_на_сегодня(задание, дата):
    for п in задание["items"]:
        if п["дата"] == дата:
            return п
    return None


def главная():
    если_сухо = "--dry" in sys.argv
    только_проверка = "--check" in sys.argv

    if not ЗАДАНИЕ.exists():
        raise SystemExit(f"❌ нет задания {ЗАДАНИЕ.name}")
    задание = json.loads(ЗАДАНИЕ.read_text(encoding="utf-8"))
    дата = сегодня_мск()
    п = пункт_на_сегодня(задание, дата)

    if not п:
        print(f"на {дата} публикаций не запланировано — молчу")
        return
    if п.get("опубликовано"):
        print(f"⛔ {дата} уже опубликовано ({п['опубликовано']['id']}) — второй раз не шлю")
        return

    print(f"📅 {дата} · {п['тип']} · {п['media']}")
    print(f"📝 {п['подпись'][:90]}...")
    if только_проверка or если_сухо:
        print("(сухой прогон — ничего не отправлено)")
        return
    if not ТОКЕН or not СТРАНИЦА:
        raise SystemExit("❌ нет META_PAGE_TOKEN или FB_PAGE_ID в окружении")

    if п["тип"] == "фото":
        ответ = запрос(f"{СТРАНИЦА}/photos", url=п["media"], caption=п["подпись"])
    else:
        # ⚠️ Именно `/videos` с `file_url`, а не загрузка файлом: так 10.09.2026
        # на Страницу лёг «Макаллан», когда выяснилось, что кросс-постинг из
        # Instagram на Страницу больше не работает и причина не найдена.
        ответ = запрос(f"{СТРАНИЦА}/videos", file_url=п["media"], description=п["подпись"])

    ид = ответ.get("id") or ответ.get("post_id")
    if not ид:
        raise SystemExit(f"❌ Meta не вернула id: {ответ}")
    п["опубликовано"] = {"id": ид, "когда": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    ЗАДАНИЕ.write_text(json.dumps(задание, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"✅ опубликовано: {ид}")
    print("📌 отметка записана в fb_sprint.json — workflow обязан её закоммитить, "
          "иначе завтрашний запуск увидит пункт неопубликованным")


if __name__ == "__main__":
    главная()
