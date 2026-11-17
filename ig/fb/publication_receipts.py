"""#858, 17.11.2026: принятие API до записи журнала порождало повтор.

Intent сохраняется ДО вызова. Неопределённый исход не повторяется автоматически.
LocalStore — приватная папка mini; GitHubStore — отдельная ветка receipts,
не Pages/main. В записи только идентичность, хеш payload и ответ (без текста/токена).
"""
from __future__ import annotations
import base64
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request


class ReceiptError(RuntimeError):
    pass


class UnknownOutcome(ReceiptError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class LocalStore:
    def __init__(self, root):
        self.root = Path(root)

    @contextlib.contextmanager
    def locked(self, key):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / (key + '.lock')).open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ReceiptError('Другая попытка ещё выполняется') from None
            yield

    def read(self, key):
        path = self.root / (key + '.json')
        return json.loads(path.read_text()) if path.exists() else None

    def write(self, key, value):
        atomic_json(self.root / (key + '.json'), value)


class GitHubStore:
    """CAS через Contents API: следующий раннер читает сохранённый intent.

    Ветка publication-receipts создаётся при подготовке выкладки, а не лениво
    после отправки. 404 на read проверяет существование ветки; потеря ветки — отказ.
    """
    def __init__(self, repo, token, branch='publication-receipts', opener=None):
        if not re.fullmatch(r'[\w.-]+/[\w.-]+', repo or '') or not token:
            raise ReceiptError('Не настроено долговременное хранилище GitHub')
        self.repo, self.token, self.branch = repo, token, branch
        self.opener = opener or urllib.request.urlopen
        self.shas = {}

    @contextlib.contextmanager
    def locked(self, key):
        # Конкурирующие раннеры отсекаются CAS первоначального intent, не таймером.
        yield

    def request(self, path, data=None):
        request = urllib.request.Request('https://api.github.com/repos/' + self.repo + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={'Authorization': 'Bearer ' + self.token,
                     'Accept': 'application/vnd.github+json',
                     'X-GitHub-Api-Version': '2026-03-10'},
            method='PUT' if data is not None else 'GET')
        try:
            with self.opener(request, timeout=45) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            # Ни тело ответа, ни request/token не выводятся в диагностику.
            if exc.code == 404 and data is None:
                return None
            raise ReceiptError('GitHub receipt: HTTP ' + str(exc.code)) from None
        except Exception:
            raise ReceiptError('GitHub receipt: результат записи/чтения неизвестен') from None

    def path(self, key):
        return '/contents/receipts/' + key + '.json'

    def read(self, key):
        value = self.request(self.path(key) + '?ref=' + urllib.parse.quote(self.branch, safe=''))
        if value is None:
            branch = self.request('/branches/' + urllib.parse.quote(self.branch, safe=''))
            if not branch:
                raise ReceiptError('Отсутствует ветка хранилища квитанций')
            self.shas[key] = None
            return None
        self.shas[key] = value['sha']
        return json.loads(base64.b64decode(value['content']))

    def write(self, key, value):
        data = {'message': 'receipt: ' + key[:12] + ' ' + value['status'],
                'content': base64.b64encode(json.dumps(value, ensure_ascii=False).encode()).decode(),
                'branch': self.branch}
        if self.shas.get(key):
            data['sha'] = self.shas[key]
        result = self.request(self.path(key), data)
        self.shas[key] = result['content']['sha']


def store(root=None):
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        return GitHubStore(os.environ.get('GITHUB_REPOSITORY'), os.environ.get('RECEIPTS_TOKEN'))
    return LocalStore(root or Path(__file__).parent / '.publication-receipts')


def identity(platform, destination, material):
    return {'platform': platform, 'destination': str(destination), 'material': material}


def key_for(platform, destination, material):
    return digest(identity(platform, destination, material))


def validate(record, expected=None):
    if not isinstance(record, dict) or record.get('status') not in {
            'pending', 'confirmed', 'handed_off', 'unknown', 'not_sent'}:
        raise ReceiptError('Повреждена запись квитанции — требуется сверка')
    if expected and record.get('identity') != expected:
        raise ReceiptError('Квитанция относится к другому материалу')
    if record['status'] in {'confirmed', 'handed_off'} and not record.get('external_id'):
        raise ReceiptError('Квитанция без ID не подтверждает доставку')


def once(backend, platform, destination, material, payload, send, status='confirmed'):
    """send() возвращает только ID и ссылку, никогда не сам токен/полный ответ API."""
    if status not in {'confirmed', 'handed_off'}:
        raise ReceiptError('Неверный статус результата')
    ident = identity(platform, destination, material)
    key, fingerprint = digest(ident), digest(payload)
    with backend.locked(key):
        previous = backend.read(key)
        if previous is not None:
            validate(previous, ident)
            if previous.get('fingerprint') != fingerprint:
                raise ReceiptError('Материал изменён после попытки — нужна сверка версии')
            if previous['status'] in {'confirmed', 'handed_off'}:
                return previous
            if previous['status'] != 'not_sent':
                raise UnknownOutcome('Исход предыдущей отправки неизвестен; повтор запрещён: ' + key)
        record = {'schema': 1, 'identity': ident, 'fingerprint': fingerprint,
                  'status': 'pending', 'updated_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
        # Если эта запись не стала долговременной, send вызывать нельзя.
        backend.write(key, record)
        try:
            result = send()
            if not isinstance(result, dict) or not result.get('external_id'):
                raise UnknownOutcome('API не вернула ID')
        except Exception:
            record['status'] = 'unknown'
            try:
                backend.write(key, record)
            except Exception:
                pass  # Сохранённый pending тоже блокирует повтор.
            raise UnknownOutcome('Отправка не подтверждена; требуется сверка: ' + key) from None
        record.update(status=status, external_id=str(result['external_id']),
                      permalink=result.get('permalink'),
                      updated_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
        backend.write(key, record)
        return record


def resolve(backend, key, *, external_id=None, permalink=None, not_sent=False, evidence=None):
    """Только после ручной сверки. Нет тайм-аута, автоматически разрешающего повтор."""
    if not evidence or bool(external_id) == bool(not_sent):
        raise ReceiptError('Нужны основание сверки и ровно один исход')
    with backend.locked(key):
        record = backend.read(key)
        validate(record)
        if record['status'] not in {'pending', 'unknown'}:
            raise ReceiptError('Разрешать можно только неизвестный исход')
        record.update(status='not_sent' if not_sent else
                      ('handed_off' if record['identity']['platform']=='shorts-handoff' else 'confirmed'),
                      external_id=str(external_id) if external_id else None,
                      permalink=permalink, resolution=evidence,
                      updated_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
        backend.write(key, record)
        return record


def telegram_link(chat_id, message_id):
    text = str(chat_id)
    return 'https://t.me/c/' + text[4:] + '/' + str(message_id) if text.startswith('-100') else None


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Сверка исхода; команд публикации здесь нет')
    parser.add_argument('--root', type=Path)
    parser.add_argument('--key', required=True)
    parser.add_argument('--id')
    parser.add_argument('--url')
    parser.add_argument('--not-sent', action='store_true')
    parser.add_argument('--evidence')
    args = parser.parse_args()
    if not re.fullmatch('[a-f0-9]{64}', args.key):
        parser.error('key должен быть SHA-256')
    backend = store(args.root)
    result = resolve(backend, args.key, external_id=args.id, permalink=args.url,
                     not_sent=args.not_sent, evidence=args.evidence) if args.evidence else backend.read(args.key)
    print(json.dumps(result, ensure_ascii=False, indent=2))
