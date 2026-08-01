#!/usr/bin/env python3
"""Публикация в Instagram и на Страницу Facebook через Graph API.

⚠️ ЭТОТ СКРИПТ НЕ РАБОТАЕТ С ДОМАШНЕЙ МАШИНЫ: домены Meta из России закрыты.
Он живёт в репозитории сайта (github.com/sklleon/dadetka) и запускается
GitHub Actions — их раннеры ходят к Meta из США, VPN не нужен вообще.
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

TOKEN = os.environ.get("META_PAGE_TOKEN", "")
IG_USER = os.environ.get("IG_USER_ID", "")
FB_PAGE = os.environ.get("FB_PAGE_ID", "")

# Сколько ждём обработку видео: Reels на минуту-полторы обычно готовы за 20-40 сек,
# но Meta не обещает ничего — поэтому терпим до пяти минут и только потом сдаёмся.
VIDEO_TIMEOUT = 300
VIDEO_POLL = 10


class MetaError(RuntimeError):
    """Ошибка от Graph API, уже переведённая на человеческий."""


def _call(method, path, params):
    url = f"{API}/{path}"
    data = dict(params, access_token=TOKEN)
    body = urllib.parse.urlencode(data).encode()
    req = (urllib.request.Request(url, data=body, method="POST") if method == "POST"
           else urllib.request.Request(f"{url}?{urllib.parse.urlencode(data)}"))
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
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
                "ИНСТРУКЦИЯ_Instagram_Facebook.txt — и заменой секрета META_PAGE_TOKEN.")
        if code == 200:
            raise MetaError(
                f"Нет прав на это действие (код 200): {msg}\n"
                "Проверь, что при выпуске токена были отмечены все шесть разрешений и что "
                "на Странице пройдена Page Publishing Authorization.")
        if code in (4, 17, 32, 613):
            raise MetaError(f"Упёрлись в лимит Meta (код {code}): {msg}. Повторим в следующий слот.")
        raise MetaError(f"Graph API вернул ошибку {code}: {msg}")
    except urllib.error.URLError as e:
        raise MetaError(
            f"Не достучались до graph.facebook.com ({e.reason}). "
            "Если это домашняя машина — так и должно быть, домены Meta из России закрыты: "
            "публикация идёт только из GitHub Actions.")


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


def check():
    """Проверка связи: кто мы, что за Страница, сколько уже постов."""
    me = get(IG_USER, fields="username,name,followers_count,media_count")
    print(f"✅ Instagram: @{me.get('username')} · {me.get('name')} · "
          f"подписчиков {me.get('followers_count')} · постов {me.get('media_count')}")
    if FB_PAGE:
        page = get(FB_PAGE, fields="name,fan_count,link")
        print(f"✅ Facebook: {page.get('name')} · подписчиков {page.get('fan_count')} · {page.get('link')}")
    q = load_queue()
    today = datetime.now(MSK).strftime("%Y-%m-%d")
    due = [i for i in q["items"] if i["date"] == today]
    print(f"✅ очередь: {len(q['items'])} записей, собрана {q.get('built')}, на сегодня — {len(due)}")


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
        today = datetime.now(MSK).strftime("%Y-%m-%d")
        items = [i for i in items if i["date"] == today]
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
