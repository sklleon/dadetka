#!/usr/bin/env python3
"""Публикация в Instagram и на Страницу Facebook через Graph API.

⚠️ ДО 12.09.2026 ЗДЕСЬ СТОЯЛО «не работает с домашней машины: домены Meta из России
закрыты». Это оказалось неверно. Замер 12.09.2026: facebook.com отдаёт 200,
instagram.com — 200, а `--check` с домашней машины проходит и показывает оба аккаунта.
Настоящей причиной отказов был отсутствующий набор корневых сертификатов у системного
python3 — ошибка выглядела как обрыв связи, а скрипт объяснял её блокировкой и отправлял
чинить не то. Теперь сертификаты берутся из certifi (см. `_ssl_контекст`).

Скрипт живёт и в репозитории сайта (github.com/sklleon/dadetka), где его запускает
GitHub Actions — этот путь остаётся рабочим и нужен для публикации по расписанию.
Сюда, в Диктовка/, положен исходник: отсюда его копирует в site/ скрипт
ig_post.py, а дальше он уезжает на GitHub вместе с зеркалом (deploy_site.sh).

    python3 ig_publish.py --check          # проверить токен и связь, ничего не публикуя
    python3 ig_publish.py --today          # опубликовать то, что очередь назначила на сегодня
    python3 ig_publish.py --key эп5        # опубликовать конкретный выпуск, не глядя на дату
    python3 ig_publish.py --today --dry    # показать, что ушло бы, и выйти

Очередь — ig_queue.json рядом со скриптом, её собирает Диктовка/ig_post.py.
Секреты — три переменные окружения (в Actions это Secrets репозитория):
    META_PAGE_TOKEN · IG_USER_ID · FB_PAGE_ID

Как устроена публикация в Meta (важно для понимания кода ниже):
любой пост — это ДВА шага. Сначала создаётся «контейнер» (media container),
и только потом он публикуется отдельным вызовом. Для видео между шагами нужно
дождаться, пока Meta его обработает, иначе публикация падает с «Media ID is
not available». Для карусели контейнеров столько же, сколько слайдов, плюс
один общий.
"""
import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://graph.facebook.com/v25.0"
HERE = pathlib.Path(__file__).resolve().parent
QUEUE = HERE / "ig_queue.json"
MSK = timezone(timedelta(hours=3))

НАСТРОЙКИ = HERE.parent / ".claude" / "settings.local.json"


def _секрет(переменная: str, ключ_в_файле: str) -> str:
    """Значение из окружения, а если его нет — из settings.local.json.

    ⚠️ Ключи лежат в `Podcast/.claude/settings.local.json`, а скрипты читают только
    окружение — расхождение известно как задача #195. В Actions переменные есть всегда
    и файла там нет вовсе, поэтому чтение файла включается ровно в локальном запуске
    и ничего не меняет для расписания.
    """
    из_окружения = os.environ.get(переменная, "")
    if из_окружения:
        return из_окружения
    try:
        данные = json.loads(НАСТРОЙКИ.read_text(encoding="utf-8"))
    except Exception:
        return ""
    return (данные.get("env", данные) or {}).get(ключ_в_файле, "") or ""


def _ssl_контекст():
    """Корневые сертификаты из certifi.

    ⛔ 12.09.2026: у системного python3 на macOS их нет, и любой вызов падал с
    CERTIFICATE_VERIFY_FAILED. Без этого контекста скрипт «не работал с домашней машины»
    полтора месяца — не из-за блокировки, а из-за отсутствующего файла сертификатов.
    """
    try:
        import ssl

        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


TOKEN = _секрет("META_PAGE_TOKEN", "FB_PAGE_ACCESS_TOKEN")
IG_USER = _секрет("IG_USER_ID", "INSTAGRAM_ACCOUNT_ID")
FB_PAGE = _секрет("FB_PAGE_ID", "FB_PAGE_ID")
SSL_КОНТЕКСТ = _ssl_контекст()

# Сколько ждём обработку видео: Reels на минуту-полторы обычно готовы за 20-40 сек,
# но Meta не обещает ничего — поэтому терпим до пяти минут и только потом сдаёмся.
VIDEO_TIMEOUT = 300
VIDEO_POLL = 10


class MetaError(RuntimeError):
    """Ошибка от Graph API, уже переведённая на человеческий."""


def объяснить_сетевую_ошибку(причина) -> str:
    """Текст отказа называет причину, а не единственную заготовленную версию.

    ⛔ 12.09.2026 скрипт на CERTIFICATE_VERIFY_FAILED отвечал «домены Meta из России
    закрыты, публикация идёт только из GitHub Actions». Сеть при этом была открыта —
    facebook.com отдавал 200, — а не хватало корневых сертификатов у системного python3.
    Полтора месяца публикация с этой машины считалась невозможной, и правка пошла не
    туда: чинили Actions вместо сертификатов.

    📌 Та же ошибка уже была с Яндекс.Вебмастером: 403 объясняли нехваткой прав, хотя
    причина бывала другая, — и отказ отправлял чинить не то. Отказ обязан называть обе
    возможности и порядок проверки.
    """
    текст = str(причина)
    if "CERTIFICATE_VERIFY_FAILED" in текст or "unable to get local issuer" in текст:
        return (f"Не проверился сертификат graph.facebook.com ({текст}). "
                "Это НЕ блокировка: связь с Meta есть, а у интерпретатора нет набора "
                "корневых сертификатов. Запускать питоном с certifi — например "
                "~/Projects/vtoroy-ya/.venv/bin/python — либо задать "
                "SSL_CERT_FILE=$(python3 -m certifi).")
    return (f"Не достучались до graph.facebook.com ({текст}). "
            "Сертификаты тут ни при чём — причина другая. Проверять по порядку: "
            "есть ли сеть вообще, отвечает ли curl -I https://www.facebook.com/, "
            "и только если домен действительно закрыт — публиковать из GitHub Actions.")


def _call(method, path, params):
    url = f"{API}/{path}"
    data = dict(params, access_token=TOKEN)
    body = urllib.parse.urlencode(data).encode()
    req = (urllib.request.Request(url, data=body, method="POST") if method == "POST"
           else urllib.request.Request(f"{url}?{urllib.parse.urlencode(data)}"))
    try:
        with urllib.request.urlopen(req, timeout=120, context=SSL_КОНТЕКСТ) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            err = json.loads(raw)["error"]
        except Exception:
            raise MetaError(f"HTTP {e.code}: {raw[:400]}")
        code, msg = err.get("code"), err.get("message", "")
        if code == 190:
            raise MetaError(
                "Токен недействителен (код 190). Так бывает после смены пароля Facebook, "
                "отзыва прав или блокировки. Лечится перевыпуском по шагу 6 инструкции "
                "09_Промпты/Instagram_Facebook/ИНСТРУКЦИЯ_Instagram_Facebook.txt — и заменой секрета META_PAGE_TOKEN.")
        if code == 200:
            raise MetaError(
                f"Нет прав на это действие (код 200): {msg}\n"
                "Проверь, что при выпуске токена были отмечены все шесть разрешений и что "
                "на Странице пройдена Page Publishing Authorization.")
        if code in (4, 17, 32, 613):
            raise MetaError(f"Упёрлись в лимит Meta (код {code}): {msg}. Повторим в следующий слот.")
        raise MetaError(f"Graph API вернул ошибку {code}: {msg}")
    except urllib.error.URLError as e:
        raise MetaError(объяснить_сетевую_ошибку(e.reason))


def get(path, **params):
    return _call("GET", path, params)


def post(path, **params):
    return _call("POST", path, params)


# ─────────────────────────── публикация ───────────────────────────

def container(**params):
    """Создать медиа-контейнер и вернуть его id."""
    return post(f"{IG_USER}/media", **params)["id"]


def wait_ready(cid):
    """Дождаться, пока Meta обработает видео. Для фото не нужно."""
    waited = 0
    while waited < VIDEO_TIMEOUT:
        st = get(cid, fields="status_code,status").get("status_code")
        if st == "FINISHED":
            return
        if st == "ERROR":
            raise MetaError(f"Meta не смогла обработать видео: {get(cid, fields='status').get('status')}")
        time.sleep(VIDEO_POLL)
        waited += VIDEO_POLL
        print(f"   … обработка видео, {waited} сек")
    raise MetaError(f"Видео не обработалось за {VIDEO_TIMEOUT} сек — публикацию не делаем.")


def publish(cid):
    """Второй шаг: опубликовать готовый контейнер."""
    return post(f"{IG_USER}/media_publish", creation_id=cid)["id"]


def post_image(url, caption):
    return publish(container(image_url=url, caption=caption))


def post_carousel(urls, caption):
    if not 2 <= len(urls) <= 10:
        raise MetaError(f"В карусели должно быть от 2 до 10 слайдов, а их {len(urls)}.")
    children = [container(image_url=u, is_carousel_item="true") for u in urls]
    print(f"   слайдов подготовлено: {len(children)}")
    return publish(container(media_type="CAROUSEL", children=",".join(children), caption=caption))


def post_reel(video_url, caption, cover_url=None):
    params = dict(media_type="REELS", video_url=video_url, caption=caption)
    if cover_url:
        params["cover_url"] = cover_url
    cid = container(**params)
    wait_ready(cid)
    return publish(cid)


def post_story(url, is_video=False):
    params = {"media_type": "STORIES"}
    params["video_url" if is_video else "image_url"] = url
    cid = container(**params)
    if is_video:
        wait_ready(cid)
    return publish(cid)


def comment(media_id, text):
    """Хэштеги идут первым комментарием, а не в подписи — так чище выглядит пост."""
    return post(f"{media_id}/comments", message=text)["id"]


def fb_post(message, link=None, photo_url=None):
    """Зеркало на Страницу Facebook. Падение здесь не должно ронять пост в IG."""
    if not FB_PAGE:
        return None
    if photo_url:
        return post(f"{FB_PAGE}/photos", url=photo_url, caption=message)["id"]
    params = dict(message=message)
    if link:
        params["link"] = link
    return post(f"{FB_PAGE}/feed", **params)["id"]


# ─────────────────────────── очередь ───────────────────────────

def load_queue():
    if not QUEUE.exists():
        raise SystemExit(f"❌ нет файла очереди {QUEUE.name} — собери его: python3 Диктовка/ig_post.py")
    return json.loads(QUEUE.read_text(encoding="utf-8"))


def already_posted(caption):
    """Защита от дубля при повторном запуске: ищем ту же подпись среди последних постов."""
    head = caption.strip()[:60]
    try:
        media = get(f"{IG_USER}/media", fields="caption,timestamp", limit=25).get("data", [])
    except MetaError:
        return False        # не смогли проверить — не повод отменять публикацию
    return any((m.get("caption") or "").strip()[:60] == head for m in media)


def run_item(item, dry=False):
    fmt = item["format"]
    caption = item["caption"]
    print(f"\n▶ {item['key']} · {fmt}")
    if dry:
        print(f"   медиа: {item.get('media')}")
        print(f"   подпись ({len(caption)} зн.):\n{caption}")
        print(f"   первым комментарием: {item.get('comment', '—')}")
        return None
    if already_posted(caption):
        print("   ⏭  такой пост уже есть в ленте — пропускаем (защита от дубля)")
        return None

    if fmt == "carousel":
        mid = post_carousel(item["media"], caption)
    elif fmt == "cover":
        mid = post_image(item["media"][0], caption)
    elif fmt == "reels":
        mid = post_reel(item["media"][0], caption, item.get("cover"))
    elif fmt == "story":
        mid = post_story(item["media"][0], is_video=item.get("video", False))
    else:
        raise MetaError(f"неизвестный формат: {fmt}")
    print(f"   ✅ Instagram: {mid}")

    if item.get("comment"):
        try:
            comment(mid, item["comment"])
            print("   ✅ хэштеги первым комментарием")
        except MetaError as e:
            print(f"   ⚠️ комментарий не ушёл: {e}")

    if item.get("facebook", True) and fmt != "story":
        try:
            fid = fb_post(item.get("fb_message", caption),
                          photo_url=item["media"][0] if fmt != "reels" else None)
            print(f"   ✅ Facebook: {fid}")
        except MetaError as e:
            print(f"   ⚠️ Facebook не принял ({e}) — в Instagram при этом всё вышло")
    return mid


def головы_ленты(limit=50):
    """Первые 60 знаков подписей последних постов Instagram. None — лента не ответила.

    Журналом служит сама лента: раннер GitHub чистый при каждом запуске, а
    отметка в очереди потребовала бы коммита обратно в репозиторий — лишний
    шаг, который может не доехать. Лента врать не может, её ведёт площадка.
    """
    try:
        медиа = get(f"{IG_USER}/media", fields="caption,timestamp", limit=limit).get("data", [])
    except MetaError:
        return None
    return [((m.get("caption") or "").strip()[:60], (m.get("timestamp") or "")[:10])
            for m in медиа]


def решить(очередь, лента, сегодня):
    """Что публиковать сегодня. → (пункт или None, объяснение).

    Чистая функция: сеть и файл снаружи, чтобы каждое «нет» проверялось тестом.

    ⛔ 21.09.2026 здесь стоял отбор `[i for i in items if i["date"] == today]`.
    Строгое равенство означает, что пропущенный день теряется навсегда: запись
    остаётся в очереди, но её дата уже не наступит. К 21.09 так накопилось 12
    записей возрастом от 31 до 54 дней — весь хвост сериала.

    📌 Поэтому берём ПЕРВЫЙ НЕВЫЛОЖЕННЫЙ, а не «назначенный на сегодня» — тот же
    приём, что у очереди Страницы (`fb_queue_publish.решить`, 15.09.2026). Дата в
    записи остаётся планом для доски, а очередью правит порядок.

    ⛔ Лента не прочиталась — не публикуем вовсе. Сверка, которую пропустили
    молча, хуже отсутствующей: она выглядит сделанной.
    """
    старт = очередь.get("старт")
    if старт and сегодня.isoformat() < старт:
        return None, f"очередь стартует {старт} — до этого молчу"
    дни = очередь.get("дни")
    if дни is not None and сегодня.weekday() not in дни:
        return None, "сегодня не день очереди — молчу"
    if лента is None:
        return None, "ленту Instagram прочитать не удалось — без сверки не публикую"
    if any(д == сегодня.isoformat() for _г, д in лента):
        return None, "сегодня в ленте уже есть пост — второй за день не шлю"
    головы = {г for г, _д in лента}
    for п in очередь["items"]:
        if п.get("format") == "story":
            continue                    # сторис живёт сутки, очередью не ведётся
        if п["caption"].strip()[:60] in головы:
            continue                    # уже в ленте
        return п, f"следующий: {п['key']} ({п['format']}, план {п['date']})"
    return None, "очередь пуста — весь хвост уже в ленте Instagram"


def check():
    """Проверка связи: кто мы, что за Страница, сколько уже постов."""
    me = get(IG_USER, fields="username,name,followers_count,media_count")
    print(f"✅ Instagram: @{me.get('username')} · {me.get('name')} · "
          f"подписчиков {me.get('followers_count')} · постов {me.get('media_count')}")
    if FB_PAGE:
        page = get(FB_PAGE, fields="name,fan_count,link")
        print(f"✅ Facebook: {page.get('name')} · подписчиков {page.get('fan_count')} · {page.get('link')}")
    q = load_queue()
    лента = головы_ленты()
    п, почему = решить(q, лента, datetime.now(MSK).date())
    print(f"✅ очередь: {len(q['items'])} записей, собрана {q.get('built')}, "
          f"старт {q.get('старт', '—')}")
    print(f"📅 сегодня: {почему}")
    if лента is None:
        print("⛔ лента Instagram не прочиталась — публикация была бы отменена")
    else:
        осталось = sum(1 for i in q["items"] if i.get("format") != "story"
                       and i["caption"].strip()[:60] not in {г for г, _ in лента})
        print(f"✅ в ленте {len(лента)} постов · в очереди ждут {осталось}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="проверить связь и выйти")
    ap.add_argument("--today", action="store_true", help="опубликовать назначенное на сегодня")
    ap.add_argument("--key", help="опубликовать конкретный выпуск")
    ap.add_argument("--format", help="только этот формат (carousel/cover/reels/story)")
    ap.add_argument("--dry", action="store_true", help="показать, но не публиковать")
    args = ap.parse_args()

    missing = [n for n, v in (("META_PAGE_TOKEN", TOKEN), ("IG_USER_ID", IG_USER)) if not v]
    if missing and not args.dry:
        raise SystemExit(f"❌ не заданы переменные окружения: {', '.join(missing)}")

    if args.check:
        return check()

    items = load_queue()["items"]
    if args.key:
        items = [i for i in items if i["key"] == args.key]
    elif args.today:
        # Один выпуск за запуск: очередь догоняет хвост по дню, а не заливает
        # двенадцать постов подряд.
        q = load_queue()
        п, почему = решить(q, головы_ленты(), datetime.now(MSK).date())
        print(f"📅 {почему}")
        items = [п] if п else []
    else:
        raise SystemExit("нужен --today, --key или --check")
    if args.format:
        items = [i for i in items if i["format"] == args.format]

    if not items:
        print("на сегодня публикаций нет — это нормально, выходим")
        return

    errors = 0
    for item in items:
        try:
            run_item(item, dry=args.dry)
        except MetaError as e:
            errors += 1
            print(f"   ❌ {e}")
    if errors:
        raise SystemExit(f"\n❌ с ошибками: {errors} из {len(items)}")
    print(f"\n✅ готово: {len(items)}")


if __name__ == "__main__":
    try:
        main()
    except MetaError as e:
        sys.exit(f"❌ {e}")
