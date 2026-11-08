from email.message import Message
import io
import ipaddress
import json
from pathlib import Path
import tempfile
import threading
import re
import unittest
from unittest.mock import patch,Mock
import httpx

from placekeeper.web.app import Jobs,handler_for
from placekeeper.storage import private_json


class PageTests(unittest.TestCase):
    def handler(self,jobs,path,*,ip='127.0.0.1',host='127.0.0.1:8780',origin=None,body=b'{}',token=None):
        cls=handler_for(jobs,{'127.0.0.1:8780'},[ipaddress.ip_network('127.0.0.0/8')])
        h=cls.__new__(cls);h.path=path;h.client_address=(ip,1234);h.headers=Message()
        h.headers['Host']=host;h.headers['Content-Length']=str(len(body))
        if origin:h.headers['Origin']=origin
        if token:h.headers['X-Placekeeper']=token
        h.rfile=io.BytesIO(body);h.wfile=io.BytesIO();h.code=None;h.response_headers={}
        h.send_response=lambda status:setattr(h,'code',status)
        h.send_header=lambda k,v:h.response_headers.update({k:v})
        h.end_headers=lambda:None
        return h

    def test_getting_page_or_status_never_starts_a_sync(self):
        jobs=Mock();jobs.status.return_value={'running':False}
        for path in ('/','/setup','/api/status'):
            h=self.handler(jobs,path);h.do_GET();self.assertEqual(h.code,200)
        jobs.start.assert_not_called()
        jobs.setup_action.assert_not_called()

    def test_public_sources_and_unexpected_hosts_are_rejected(self):
        for opts in ({'ip':'203.0.113.9'},{'host':'evil.example:8780'}):
            h=self.handler(Mock(),'/api/status',**opts);h.do_GET();self.assertEqual(h.code,403)

    def test_post_from_other_origin_or_without_csrf_is_rejected(self):
        for origin in ('http://evil.example','http://127.0.0.1:8780'):
            jobs=Mock();h=self.handler(jobs,'/api/sync',origin=origin);h.do_POST()
            self.assertEqual(h.code,403);jobs.start.assert_not_called()

    def test_setup_post_needs_same_origin_and_page_token(self):
        jobs=Mock()
        jobs.setup_action.return_value={'code':'12345678'}
        body=b'{"service":"libby","action":"start","consent":true}'
        h=self.handler(jobs,'/setup',origin='http://127.0.0.1:8780',body=body)
        h.do_GET()
        token=re.search(rb"const csrf='([^']+)'",h.wfile.getvalue())[1].decode()
        h.path='/api/setup'
        h.do_POST()
        self.assertEqual(h.code,403)
        jobs.setup_action.assert_not_called()
        h.headers['X-Placekeeper']=token
        h.do_POST()
        self.assertEqual(h.code,200)
        jobs.setup_action.assert_called_once()

    def test_concurrent_clicks_share_one_job(self):
        started=threading.Event();finish=threading.Event()
        def onboard():started.set();finish.wait(2);return {'cached':1}
        with tempfile.TemporaryDirectory() as d:
            private_json(Path(d)/'crosspoint-auth.json',{'username':'test','auth_key':'test'})
            jobs=Jobs(d,onboard=onboard,sync=lambda notify:[{'title':'Test','status':'in_sync','message':'Already together','writes':0}])
            self.assertTrue(jobs.start('discovery'));self.assertTrue(started.wait(2))
            self.assertFalse(jobs.start());finish.set()
            self.assertTrue(jobs.lock.acquire(timeout=3));jobs.lock.release()
            self.assertIsNone(jobs.last)
            self.assertEqual(jobs.last_discovery['discovery'], {'cached': 1})
            self.assertTrue(jobs.start())
            self.assertTrue(jobs.lock.acquire(timeout=3));jobs.lock.release()
            self.assertFalse(jobs.running)
            self.assertEqual(jobs.last['status'],'complete')
            self.assertEqual((Path(d)/'web-last-sync.json').stat().st_mode&0o777,0o600)

    def test_schedule_settings_require_csrf_and_persist(self):
        with tempfile.TemporaryDirectory() as d:
            jobs = Jobs(d)
            self.assertFalse(jobs.status()['schedule_enabled'])
            h = self.handler(jobs, '/setup', origin='http://127.0.0.1:8780',
                             body=b'{"enabled":true}')
            h.do_GET()
            token = re.search(rb"const csrf='([^']+)'", h.wfile.getvalue())[1].decode()
            h.path = '/api/schedule'
            h.do_POST()
            self.assertEqual(h.code, 403)
            self.assertFalse(jobs.schedule_enabled)
            h.headers['X-Placekeeper'] = token
            h.do_POST()
            self.assertEqual(h.code, 200)
            self.assertTrue(Jobs(d).status()['schedule_enabled'])
            self.assertEqual((Path(d)/'web-schedule.json').stat().st_mode & 0o777, 0o600)
            jobs.set_schedule(False)
            self.assertFalse(Jobs(d).status()['schedule_enabled'])

    def test_scheduler_runs_only_progress(self):
        ran = threading.Event()
        onboard = Mock()
        def sync(notify):
            ran.set()
            return []
        with tempfile.TemporaryDirectory() as d:
            jobs = Jobs(d, onboard=onboard, sync=sync)
            with patch('placekeeper.web.app.monotonic', return_value=100) as clock:
                jobs.set_schedule(True)
                self.assertEqual(jobs.next_sync, 1900)
                clock.return_value = 1900
                scheduler = threading.Thread(target=jobs.schedule, daemon=True)
                scheduler.start()
                try:
                    self.assertTrue(ran.wait(2))
                    self.assertTrue(jobs.lock.acquire(timeout=2))
                    jobs.lock.release()
                    self.assertEqual(jobs.last['books'], [])
                    onboard.assert_not_called()
                    jobs.set_schedule(False)
                    self.assertIsNone(jobs.next_sync)
                finally:
                    jobs.stop_scheduler()
                    scheduler.join(timeout=2)
                self.assertFalse(scheduler.is_alive())

    def test_saving_connection_needs_explicit_consent_and_does_not_save_password(self):
        with tempfile.TemporaryDirectory() as d:
            jobs=Jobs(d)
            with self.assertRaises(ValueError):jobs.connect('user','private-password',False)
            response=Mock(status_code=200);response.json.return_value={'authorized':'OK'}
            with patch('placekeeper.plugins.crosspoint.client.httpx.get',return_value=response):jobs.connect('user','private-password',True)
            data=(Path(d)/'crosspoint-auth.json').read_text()
            self.assertNotIn('private-password',data)
            self.assertEqual((Path(d)/'crosspoint-auth.json').stat().st_mode&0o777,0o600)

    def test_transient_dns_failure_is_retried_before_saving_connection(self):
        with tempfile.TemporaryDirectory() as d:
            jobs=Jobs(d)
            response=Mock(status_code=200);response.json.return_value={'authorized':'OK'}
            with patch('placekeeper.plugins.crosspoint.client.httpx.get',side_effect=[httpx.ConnectError('DNS busy'),response]) as get, \
                    patch('placekeeper.plugins.crosspoint.client.time.sleep'):
                jobs.connect('user','private-password',True)
            self.assertEqual(get.call_count,2)
            self.assertTrue((Path(d)/'crosspoint-auth.json').exists())

    def test_rejected_credentials_are_not_retried_or_saved(self):
        with tempfile.TemporaryDirectory() as d:
            response=Mock(status_code=401)
            with patch('placekeeper.plugins.crosspoint.client.httpx.get',return_value=response) as get:
                with self.assertRaisesRegex(ValueError,'did not accept'):
                    Jobs(d).connect('user','wrong',True)
            self.assertEqual(get.call_count,1)
            self.assertFalse((Path(d)/'crosspoint-auth.json').exists())


if __name__=='__main__':unittest.main()
