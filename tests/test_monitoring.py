"""Health checks and retries must be persistent, bounded, and write-safe."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from placekeeper.monitoring import DEFAULTS, Monitor, discovery_problem, pending_write
from placekeeper.storage import private_json
from placekeeper.web.app import Jobs


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.now = 1000
        self.monitor = Monitor(self.root, clock=lambda:self.now)
        self.enabled = {'progress':True,'discovery':True}
        self.monitor.tick(self.enabled, self.enabled)

    def fail(self, job='progress', retry=False, scheduled=True, retryable=True):
        self.monitor.begin(job, retry)
        self.monitor.finish(job, {'status':'error'}, scheduled=scheduled, retryable=retryable)

    def test_unchanged_health_state_is_not_rewritten_on_restart(self):
        with patch('placekeeper.monitoring.private_json',wraps=private_json) as save:
            Monitor(self.root,clock=lambda:self.now)
            save.assert_not_called()

    def test_backoff_is_bounded_persisted_and_reset_by_new_cycle(self):
        self.fail()
        self.assertEqual(self.monitor.retries()['progress'], 1300)
        self.now=1300
        self.fail(retry=True)
        self.assertEqual(self.monitor.retries()['progress'],1900)
        self.now=1900
        self.fail(retry=True)
        self.assertEqual(self.monitor.retries(),{})
        self.assertTrue(self.monitor.snapshot()['jobs']['progress']['retry_exhausted'])
        restored=Monitor(self.root,clock=lambda:self.now)
        self.assertEqual(restored.snapshot()['jobs']['progress']['retry_count'],2)
        self.fail()
        self.assertEqual(self.monitor.retries()['progress'],2200)

    def test_success_resolves_incident_and_duplicate_failure_does_not_spam(self):
        self.fail();self.fail(retry=True)
        self.assertEqual(len(self.monitor.snapshot()['alerts']),1)
        self.monitor.begin('progress',True)
        self.monitor.finish('progress',{'status':'complete'},scheduled=True,retryable=False)
        snapshot=self.monitor.snapshot()
        self.assertEqual(snapshot['status'],'healthy')
        self.assertIsNotNone(snapshot['alerts'][0]['resolved_at'])
        self.assertEqual(snapshot['jobs']['progress']['last_success'],self.now)
        self.assertEqual(self.monitor.retries(),{})

    def test_manual_jobs_and_unsafe_failures_never_queue_retries(self):
        self.fail(scheduled=False)
        self.assertEqual(self.monitor.retries(),{})
        self.fail(retryable=False)
        self.assertEqual(self.monitor.retries(),{})
        self.monitor.tick({'progress':False,'discovery':True},self.enabled)
        self.fail()
        self.assertEqual(self.monitor.retries(),{})

    def test_stale_checks_ignore_disabled_jobs_and_start_a_grace_window_on_enable(self):
        self.now+=5401
        self.monitor.tick(self.enabled,self.enabled)
        self.assertEqual(len(self.monitor.snapshot()['issues']),1)
        disabled={'progress':False,'discovery':False}
        self.monitor.tick(disabled,self.enabled)
        self.assertEqual(self.monitor.snapshot()['issues'],[])
        self.monitor.tick(self.enabled,self.enabled)
        self.assertEqual(self.monitor.snapshot()['issues'],[])

    def test_missing_connections_and_interruption_are_visible(self):
        self.monitor.tick(self.enabled,{'progress':False,'discovery':True})
        self.assertIn('connections',self.monitor.snapshot()['issues'][0])
        self.monitor.begin('discovery')
        restored=Monitor(self.root,clock=lambda:self.now)
        self.assertTrue(any('interrupted' in issue for issue in restored.snapshot()['issues']))
        self.assertEqual(restored.retries(),{})

    def test_config_disables_retries_and_history_is_bounded(self):
        self.fail()
        config={**DEFAULTS,'retries_enabled':False,'alerts_enabled':False}
        self.monitor.configure(config)
        self.assertEqual(self.monitor.retries(),{})
        for i in range(60):
            self.monitor.finish('progress',{'status':'complete'},scheduled=False,retryable=False)
            self.fail()
        self.assertEqual(len(self.monitor.state['alerts']),1)
        self.monitor.configure(DEFAULTS)
        for i in range(60):
            self.monitor.finish('progress',{'status':'complete'},scheduled=False,retryable=False)
            self.fail()
        self.assertEqual(len(self.monitor.state['alerts']),50)
        self.assertEqual((self.root/'health-config.json').stat().st_mode & 0o777,0o600)
        for values in ({}, {**DEFAULTS,'max_retries':True},{**DEFAULTS,'retry_minutes':0}):
            with self.assertRaises(ValueError):self.monitor.configure(values)

    def test_discovery_differentiates_transient_failures_from_manual_review(self):
        self.assertEqual(discovery_problem({'cached':2}), (False,False))
        self.assertEqual(discovery_problem({'retry_deferred':1}), (True,True))
        self.assertEqual(discovery_problem({'matches':[{'status':'edition_mismatch'}]}),(True,False))
        self.assertEqual(discovery_problem({'matches':[{'status':'open_libby'}]}),(False,False))

    def test_malformed_journal_blocks_automatic_progress_retries(self):
        path=self.root/'progress/book.json'
        private_json(path,[])
        self.assertTrue(pending_write(self.root))
        path.write_text('not json')
        self.assertTrue(pending_write(self.root))
        private_json(path,{'receipts':{}})
        self.assertFalse(pending_write(self.root))

    def test_pending_write_blocks_live_job_retry(self):
        private_json(self.root/'progress/book.json',{'pending':{'device':'reader'}})
        jobs=Jobs(self.root,sync=lambda notify:[{'title':'Book','status':'error','writes':0,'retryable':True}])
        jobs.set_schedule(True)
        jobs.monitor.begin('progress')
        jobs.lock.acquire()
        jobs.run('progress',scheduled=True)
        self.assertEqual(jobs.monitor.retries(),{})
        self.assertEqual(jobs.monitor.snapshot()['status'],'needs_attention')

    def test_pending_retry_survives_restart_and_disabling_alerts_keeps_health(self):
        self.fail()
        restored=Monitor(self.root,clock=lambda:self.now)
        self.assertEqual(restored.retries(),self.monitor.retries())
        restored.configure({**DEFAULTS,'alerts_enabled':False})
        self.assertEqual(restored.snapshot()['status'],'needs_attention')
        with self.assertRaises(ValueError):restored.test_alert()

    def test_scheduler_executes_due_retry_without_waiting_for_regular_interval(self):
        ran=threading.Event()
        jobs=Jobs(self.root,sync=lambda notify:(ran.set() or []))
        jobs.set_schedule(True)
        jobs.monitor.begin('progress')
        jobs.monitor.finish('progress',{'status':'error'},scheduled=True,retryable=True)
        jobs.monitor.defer_retry('progress',seconds=-1)
        scheduler=threading.Thread(target=jobs.schedule,daemon=True);scheduler.start()
        try:
            self.assertTrue(ran.wait(2))
            self.assertTrue(jobs.lock.acquire(timeout=2));jobs.lock.release()
            self.assertEqual(jobs.monitor.snapshot()['jobs']['progress']['retry_count'],1)
            self.assertEqual(jobs.monitor.retries(),{})
        finally:
            jobs.stop_scheduler();scheduler.join(timeout=2)
        self.assertFalse(scheduler.is_alive())
