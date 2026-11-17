"""#979: техническая приёмка относится к конкретной публикации, а не имени mp4.

Один контракт для IG/FB Reels. Не вводит новые требования для фото/каруселей.
Штамп — редакционная запись со ссылкой на существующее одобрение, не подпись
против администратора репозитория. Публикации здесь нет; проверка публичного медиа делает только GET.
"""
from __future__ import annotations
import datetime
import hashlib
import json
from pathlib import Path
import re
import urllib.parse
import urllib.request
from publication_receipts import _open

REQUIRED = frozenset({'хук', 'финал', 'полоса', 'подпись'})
DESTINATIONS = frozenset({'instagram', 'facebook'})
BASE = 'https://sklleon.github.io/dadetka/'


class ApprovalError(ValueError):
    pass


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def is_reel(item):
    return item.get('format')=='reels' or item.get('тип')=='reels'


def caption(item):
    if 'caption' in item and 'подпись' in item:
        raise ApprovalError('Неоднозначные поля подписи')
    value = item.get('caption') if 'caption' in item else item.get('подпись')
    if not _text(value):
        raise ApprovalError('У ролика нет текста публикации')
    return value


def payload(item, destination):
    if destination not in DESTINATIONS:
        raise ApprovalError('Неизвестное назначение публикации')
    if 'format' in item and 'тип' in item:
        raise ApprovalError('Неоднозначные поля формата')
    # Дата очереди не часть содержания: перенос слота не требует повторного
    # решения о том же тексте. Все реально отправляемые текст/медиа включены.
    return {'destination':destination, **{key:item.get(key) for key in (
        'key','format','тип','media','cover','caption','подпись','comment','facebook','fb_message')}}


def local_file(url, root):
    if not isinstance(url, str) or not url.startswith(BASE):
        raise ApprovalError('Медиа должно находиться в зеркале проекта')
    parsed = urllib.parse.urlsplit(url)
    if parsed.query or parsed.fragment:
        raise ApprovalError('Неоднозначный адрес медиа')
    relative = urllib.parse.unquote(parsed.path[len('/dadetka/'):])
    if not relative or '\\' in relative:
        raise ApprovalError('Некорректный путь медиа')
    base = Path(root).resolve()
    file = (base/relative).resolve()
    if not file.is_relative_to(base) or not file.is_file():
        raise ApprovalError('Файл медиа отсутствует или находится вне зеркала')
    return file


def media_hashes(item, root):
    urls = item.get('media')
    urls = [urls] if isinstance(urls, str) else urls
    if not isinstance(urls, list) or len(urls)!=1 or not _text(urls[0]):
        raise ApprovalError('У Reels должен быть один видеофайл')
    urls = urls + ([item['cover']] if item.get('cover') else [])
    result = []
    for url in urls:
        file = local_file(url, root)
        with file.open('rb') as stream:
            sha = hashlib.file_digest(stream, 'sha256').hexdigest()
        result.append({'url':url, 'sha256':sha})
    return result


def checked_points(points):
    if not isinstance(points, dict) or set(points)!=REQUIRED:
        raise ApprovalError('Нужны все четыре пункта приёмки: хук, финал, полоса, подпись')
    for value in points.values():
        if not isinstance(value, dict) or type(value.get('ok')) is not bool or not _text(value.get('что')):
            raise ApprovalError('Пункт приёмки должен содержать bool ok и объяснение')
    return {key for key,value in points.items() if not value['ok']}


def checked_override(override, failed, source):
    if override is None:
        if failed:
            raise ApprovalError('Приёмка не пройдена; нет конкретного решения об исключении')
        return
    if (not isinstance(override, dict) or not _text(override.get('причина'))
            or override.get('источник')!=source
            or not isinstance(override.get('пункты'), list)
            or set(override['пункты'])!=failed or not failed):
        raise ApprovalError('Исключение требует причины, источника и точного списка непройденных пунктов')


def make_stamp(item, root, destination, points, source, override_reason=None):
    if not is_reel(item):
        raise ApprovalError('Этот штамп предназначен для Reels')
    if not _text(source):
        raise ApprovalError('Укажи источник существующего одобрения владельца')
    caption(item)
    failed = checked_points(points)
    override = None
    if override_reason is not None:
        if not _text(override_reason):
            raise ApprovalError('Причина исключения пуста')
        override = {'причина':override_reason.strip(), 'источник':source.strip(), 'пункты':sorted(failed)}
    checked_override(override, failed, source.strip())
    return {'schema':2, 'площадка':'мета', 'назначение':destination,
            'источник':source.strip(), 'когда':datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'payload_sha256':_digest(payload(item,destination)),
            'media':media_hashes(item,root), 'пункты':points, 'исключение':override}


def require(item, root, destination):
    if destination=='facebook' and item.get('тип') not in {'фото','reels'}:
        raise ApprovalError('Неизвестный формат Facebook')
    if not is_reel(item):
        return  # Не расширяем правила приёмки видео на существующие карусели/фото.
    stamp = item.get('приёмка')
    if not isinstance(stamp, dict) or stamp.get('schema')!=2:
        raise ApprovalError('Нет актуального штампа приёмки версии 2')
    if stamp.get('площадка')!='мета' or stamp.get('назначение')!=destination:
        raise ApprovalError('Штамп относится к другой площадке')
    if not _text(stamp.get('источник')) or not _text(stamp.get('когда')):
        raise ApprovalError('В штампе отсутствует источник или дата одобрения')
    try:
        date = datetime.datetime.fromisoformat(stamp['когда'])
        if date.utcoffset() is None:
            raise ValueError('missing timezone')
    except ValueError:
        raise ApprovalError('Некорректная дата штампа') from None
    caption(item)
    failed = checked_points(stamp.get('пункты'))
    checked_override(stamp.get('исключение'),failed,stamp['источник'])
    if stamp.get('payload_sha256')!=_digest(payload(item,destination)):
        raise ApprovalError('Текст или задание изменились после одобрения')
    if stamp.get('media')!=media_hashes(item,root):
        raise ApprovalError('Видео или обложка изменились после приёмки')


def check(item, root, destination):
    try:
        require(item,root,destination)
    except (ApprovalError,OSError,ValueError,TypeError) as exc:
        return False, str(exc)
    return True, 'приёмка конкретной версии пройдена' if is_reel(item) else 'не ролик — правила без изменений'


def carry(old_items, new_items, root, destination):
    """Пересборка не теряет подходящий штамп и не переносит его на изменённый текст."""
    def ident(item):
        return _digest(payload(item,destination))
    previous = {ident(item):item for item in old_items if item.get('приёмка')}
    for item in new_items:
        old = previous.get(ident(item))
        if not old:
            continue
        candidate = dict(item, приёмка=old['приёмка'])
        if check(candidate,root,destination)[0]:
            item['приёмка'] = old['приёмка']


def verify_public(item, root, destination, opener=None):
    """Pages может отставать от checkout: не публикуем новый штамп со старым URL-файлом."""
    if not is_reel(item):
        return
    require(item,root,destination)
    open_url = opener or _open
    for media in item['приёмка']['media']:
        expected_size = local_file(media['url'],root).stat().st_size
        sha = hashlib.sha256();count=0
        request = urllib.request.Request(media['url'],headers={'Accept-Encoding':'identity'})
        try:
            with open_url(request,timeout=90) as response:
                if not response.geturl().startswith(BASE):
                    raise ApprovalError('Публичное медиа перенаправлено за пределы зеркала')
                while True:
                    part=response.read(1024*1024)
                    if not part:break
                    count+=len(part)
                    if count>expected_size:
                        raise ApprovalError('Публичное медиа не соответствует принятому файлу')
                    sha.update(part)
        except ApprovalError:
            raise
        except Exception:
            raise ApprovalError('Публичное медиа не удалось проверить — отправка остановлена') from None
        if count!=expected_size or sha.hexdigest()!=media['sha256']:
            raise ApprovalError('Публичное медиа отличается от принятой версии')
