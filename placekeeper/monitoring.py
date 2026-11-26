"""Persistent health incidents and bounded retries for scheduled jobs."""
from copy import deepcopy
from datetime import datetime
import json
import threading
import time

from placekeeper.storage import private_json

DEFAULTS = {'alerts_enabled': True, 'retries_enabled': True, 'max_retries': 2,
            'retry_minutes': 5, 'progress_stale_minutes': 90, 'discovery_stale_hours': 18}
LIMITS = {'max_retries': (0, 5), 'retry_minutes': (1, 60),
          'progress_stale_minutes': (30, 10080), 'discovery_stale_hours': (6, 720)}
JOBS = ('progress', 'discovery')


TRANSIENT_DISCOVERY = frozenset(('discovery_error', 'loan_check_failed', 'retry', 'retry_deferred', 'audio_unavailable'))
REVIEW_DISCOVERY = frozenset(('edition_mismatch', 'needs_review', 'audio_mapping_required',
                              'delivery_unavailable', 'mapping_required'))


def discovery_problem(counts):
    reported = {key for key, value in counts.items() if key != 'matches' and value}
    reported.update(match.get('status') for match in counts.get('matches', []))
    retryable = bool(reported & TRANSIENT_DISCOVERY)
    return retryable or bool(reported & REVIEW_DISCOVERY), retryable


def pending_write(root):
    try:
        for path in (root/'progress').glob('*.json'):
            try:
                saved = json.loads(path.read_text())
                if not isinstance(saved, dict) or saved.get('pending'):
                    return True
            except (OSError, ValueError):
                return True
    except OSError:
        return True
    return False


class Monitor:
    def __init__(self, root, *, clock=None):
        self.root = root
        self.clock = clock or time.time
        self.lock = threading.RLock()
        self.config = self._load('health-config.json', DEFAULTS.copy())
        self.state = self._load('health-state.json', {'jobs': {}, 'issues': {}, 'alerts': [], 'sequence': 0})
        self.config = {**DEFAULTS, **self.config}
        self._saved = json.dumps(self.state, sort_keys=True)
        for job in JOBS:
            entry = self.state['jobs'].setdefault(job, {'retry_count': 0, 'retry_at': None})
            if entry.get('active'):
                entry.update(active=False, retry_at=None)
                self._issue(job+'.failure', job.title()+' was interrupted. Check the last result before retrying.')
            if 'last_success' not in entry:
                last = self._load('web-last-'+('sync' if job == 'progress' else 'discovery')+'.json', {}) or {}
                if last.get('status') == 'complete' and last.get('finished_at'):
                    entry['last_success'] = datetime.fromisoformat(last['finished_at']).timestamp()
        self._persist()

    def _load(self, name, default):
        path = self.root/name
        return json.loads(path.read_text()) if path.exists() else deepcopy(default)

    def _persist(self):
        serialized = json.dumps(self.state, sort_keys=True)
        if serialized != self._saved:
            private_json(self.root/'health-state.json', self.state)
            self._saved = serialized

    def _issue(self, key, message=None):
        old = self.state['issues'].get(key)
        if message is None:
            self.state['issues'].pop(key, None)
            for alert in self.state['alerts']:
                if alert['key'] == key and alert.get('resolved_at') is None:
                    alert['resolved_at'] = self.clock()
            return
        self.state['issues'][key] = message
        if self.config['alerts_enabled'] and (old != message or not any(
                a['key'] == key and a.get('resolved_at') is None for a in self.state['alerts'])):
            self.state['sequence'] += 1
            self.state['alerts'].append({'id': self.state['sequence'], 'key': key, 'message': message,
                                         'opened_at': self.clock(), 'resolved_at': None})
            self.state['alerts'] = self.state['alerts'][-50:]

    def configure(self, values):
        if not isinstance(values, dict) or set(values) != set(DEFAULTS):
            raise ValueError('Provide all health and retry settings.')
        for key in ('alerts_enabled', 'retries_enabled'):
            if type(values[key]) is not bool:
                raise ValueError('Choose whether to enable alerts and retries.')
        for key, (low, high) in LIMITS.items():
            if type(values[key]) is not int or not low <= values[key] <= high:
                raise ValueError(f'{key.replace("_", " ")} must be between {low} and {high}.')
        with self.lock:
            private_json(self.root/'health-config.json', values)
            self.config = values.copy()
            for entry in self.state['jobs'].values():
                if not values['retries_enabled'] or entry['retry_count'] >= values['max_retries']:
                    entry['retry_at'] = None
                if not values['retries_enabled']:
                    entry['retry_exhausted'] = False
            for key, message in list(self.state['issues'].items()):
                self._issue(key, message)
            self._persist()

    def tick(self, enabled, ready):
        with self.lock:
            now = self.clock()
            for job in JOBS:
                entry = self.state['jobs'][job]
                if entry.get('enabled') != enabled[job]:
                    entry.update(enabled=enabled[job], enabled_since=now)
                if not enabled[job] or not ready[job]:
                    entry['retry_at'] = None
                    entry['retry_exhausted'] = False
                self._issue(job+'.setup', job.title()+' is scheduled but its required connections are missing.'
                            if enabled[job] and not ready[job] else None)
                limit = (self.config['progress_stale_minutes']*60 if job == 'progress'
                         else self.config['discovery_stale_hours']*3600)
                reference = max(entry.get('last_success', 0), entry.get('enabled_since', now))
                stale = enabled[job] and ready[job] and now-reference > limit
                self._issue(job+'.stale', job.title()+' has not completed successfully within its health threshold.'
                            if stale else None)
            self._persist()

    def begin(self, job, retry=False):
        with self.lock:
            entry = self.state['jobs'][job]
            entry.update(active=True, last_started=self.clock(), retry_at=None, retry_exhausted=False,
                         retry_count=entry['retry_count']+1 if retry else 0)
            self._persist()

    def finish(self, job, result, *, scheduled, retryable):
        with self.lock:
            entry = self.state['jobs'][job]
            entry.update(active=False, last_finished=self.clock(), last_status=result['status'], retry_at=None, retry_exhausted=False)
            if result['status'] == 'complete':
                entry['last_success'] = self.clock()
                self._issue(job+'.failure')
                self._issue(job+'.stale')
            else:
                self._issue(job+'.failure', job.title()+' needs attention. Check the latest job result.')
                if (scheduled and entry.get('enabled') and retryable and self.config['retries_enabled']
                        and entry['retry_count'] < self.config['max_retries']):
                    delay = min(60, self.config['retry_minutes']*2**entry['retry_count'])*60
                    entry['retry_at'] = self.clock()+delay
                elif scheduled and entry.get('enabled') and retryable and self.config['retries_enabled']:
                    entry['retry_exhausted'] = True
            self._persist()

    def retries(self):
        with self.lock:
            return {job: entry['retry_at'] for job, entry in self.state['jobs'].items()
                    if entry.get('retry_at') is not None}

    def defer_retry(self, job, seconds=60):
        with self.lock:
            self.state['jobs'][job]['retry_at'] = self.clock()+seconds
            self._persist()

    def test_alert(self):
        with self.lock:
            if not self.config['alerts_enabled']:
                raise ValueError('Enable on-page alerts before creating a test alert.')
            self._issue('test', 'Test failure alert. This test has recovered; no sync was changed.')
            self._issue('test')
            self._persist()

    def snapshot(self):
        with self.lock:
            return deepcopy({'status': 'needs_attention' if self.state['issues'] else 'healthy',
                             'config': self.config, 'jobs': self.state['jobs'],
                             'issues': list(self.state['issues'].values()), 'alerts': self.state['alerts'][-10:]})
