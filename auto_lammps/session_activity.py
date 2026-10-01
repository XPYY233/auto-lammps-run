"""Recent page activity for the desktop supervisor; no task or simulation state.

The recorder is enabled only when the local entry passes ``--session-activity-file``, so an
ordinary launch keeps exactly the previous behaviour. It writes one small JSON file with the
last time each open page reported itself; the desktop supervisor watches that file and stops
the service through ``scripts/stop_local.py`` when the page is gone. No task, ledger,
snapshot or run record is read or written here.
"""
import json
import os
import re
from pathlib import Path
import time

MAXIMUM_SESSIONS = 64
SAFE_SESSION = re.compile(r'[^A-Za-z0-9_-]')


def session_id(value):
    """Bounded, filesystem-friendly identifier for one open page."""
    cleaned = SAFE_SESSION.sub('', value or '')[:64]
    return cleaned or 'anonymous'


class SessionActivity:
    """Small JSON table of ``{session: {at, hidden}}`` with atomic, owner-only writes."""

    def __init__(self, path, *, clock=time.time):
        self.path = Path(path)
        self.clock = clock

    def _empty(self):
        return {'updated_at': None, 'closed_at': None, 'closed_session': None, 'sessions': {}}

    def snapshot(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return self._empty()
        if not isinstance(data, dict) or not isinstance(data.get('sessions'), dict):
            return self._empty()
        return data

    def _store(self, data):
        data['sessions'] = dict(sorted(data['sessions'].items(),
                                       key=lambda item: item[1].get('at') or 0)[-MAXIMUM_SESSIONS:])
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self.path.with_name(self.path.name + '.tmp')
            handle = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            with os.fdopen(handle, 'w', encoding='utf-8') as stream:
                stream.write(payload)
            os.replace(temporary, self.path)
        except OSError:
            return False
        return True

    def reset(self):
        """Drop every remembered page before a new supervised launch."""
        return self._store(self._empty())

    def heartbeat(self, session, hidden=False):
        name = session_id(session)
        data = self.snapshot()
        data['sessions'][name] = {'at': self.clock(), 'hidden': bool(hidden)}
        data['updated_at'] = self.clock()
        recorded = self._store(data)
        return {'recorded': recorded, 'session': name, 'open_pages': len(data['sessions'])}

    def close(self, session):
        name = session_id(session)
        data = self.snapshot()
        data['sessions'].pop(name, None)
        data['closed_at'] = self.clock()
        data['closed_session'] = name
        data['updated_at'] = self.clock()
        recorded = self._store(data)
        return {'recorded': recorded, 'session': name, 'open_pages': len(data['sessions'])}
