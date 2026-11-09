"""Private LAN/WireGuard page for explicit onboarding and reading-progress sync."""
import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import secrets
import threading
from time import monotonic
from urllib.parse import urlsplit

import httpx

from placekeeper.sync.job import refresh_libraries, run_books
from placekeeper.web.setup import Connections
from placekeeper.errors import ReadFailure
from placekeeper.storage import ROOT, onboarding_lock, private_json, state_lock


PROGRESS_INTERVAL = 30 * 60


class Jobs:
    def __init__(self,data_root,onboard=None,sync=None,plugins=None):
        self.root=Path(data_root)
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.connections=Connections(self.root,plugins)
        self.custom_jobs=onboard is not None or sync is not None
        self.lock=threading.Lock()
        self.running_job=None
        self.phase='Ready'
        self.onboard=onboard or self.discover
        self.sync=sync or self.sync_books
        self.last=self.load('web-last-sync.json', None)
        self.last_discovery=self.load('web-last-discovery.json', None)
        self.schedule_changed=threading.Condition()
        self.schedule_enabled=self.load('web-schedule.json', {}).get('enabled') is True
        self.next_sync=monotonic()+PROGRESS_INTERVAL if self.schedule_enabled else None
        self.scheduler_stopped=False

    def load(self, name, default):
        path=self.root/name
        return json.loads(path.read_text()) if path.exists() else default

    @property
    def running(self):
        return self.running_job is not None

    def discovery_ready(self):
        plugins=self.connections.connected()
        return any(plugin.discover is not None and all(key in plugins for key in plugin.catalog_requires)
                   for plugin in plugins.values())

    def status(self):
        return {'connected':self.connections.ready(), 'services':self.connections.status(),
                'can_discover':self.discovery_ready(), 'running_job':self.running_job,
                'running':self.running,'phase':self.phase,'last':self.last,
                'last_discovery':self.last_discovery,'schedule_enabled':self.schedule_enabled}

    def discover(self):
        return refresh_libraries(self.root,self.connections.connected())

    def sync_books(self,notify):
        return run_books(self.root,self.connections.connected(),notify)

    def set_schedule(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('Choose whether to enable automatic progress sync.')
        with self.schedule_changed:
            if enabled != self.schedule_enabled:
                private_json(self.root/'web-schedule.json', {'enabled': enabled})
                self.schedule_enabled=enabled
                self.next_sync=monotonic()+PROGRESS_INTERVAL if enabled else None
                self.schedule_changed.notify()

    def schedule(self):
        with self.schedule_changed:
            while not self.scheduler_stopped:
                if self.next_sync is None:
                    self.schedule_changed.wait()
                    continue
                remaining=self.next_sync-monotonic()
                if remaining > 0:
                    self.schedule_changed.wait(remaining)
                    continue
                self.next_sync=monotonic()+PROGRESS_INTERVAL
                try:
                    self.start()
                except ValueError:
                    # Connections can be added or removed between scheduled checks.
                    continue

    def stop_scheduler(self):
        with self.schedule_changed:
            self.scheduler_stopped=True
            self.schedule_changed.notify()

    def connect(self,username,password,remember):
        # Compatibility for clients using the old CrossPoint setup endpoint.
        return self.setup_action('crosspoint','start',{'username':username,'password':password,
                                                       'consent':remember})

    def setup_action(self,service,action,data):
        if not self.lock.acquire(blocking=False):
            raise ValueError('Wait for the current sync or setup action to finish.')
        try:
            with onboarding_lock(self.root):
                return self.connections.apply(service,action,data)
        finally:
            self.lock.release()

    def start(self, job='progress'):
        if not self.lock.acquire(blocking=False):
            return False
        try:
            if not self.custom_jobs:
                if job=='discovery' and not self.discovery_ready():
                    raise ValueError('Connect a book discovery source in Setup.')
                if job=='progress' and not self.connections.ready():
                    raise ValueError('Connect two readers and a book catalog in Setup.')
            self.running_job=job
            self.phase='Finding new books…' if job=='discovery' else 'Checking progress…'
            threading.Thread(target=self.run,args=(job,),daemon=True).start()
        except BaseException:
            self.running_job=None
            self.lock.release()
            raise
        return True

    def run(self, job):
        started=datetime.now(timezone.utc).isoformat()
        discovery=job=='discovery'
        try:
            with state_lock(self.root/'manual-sync-job'):
                if discovery:
                    counts=self.onboard()
                    result={'discovery':counts}
                    failed=bool(counts.get('discovery_error') or counts.get('loan_check_failed'))
                else:
                    books=self.sync(lambda text:setattr(self,'phase',text))
                    result={'books':books}
                    failed=any(book['status'] in ('error','unconfirmed','changed') for book in books)
                result['status']='needs_attention' if failed else 'complete'
        except Exception:
            result={'status':'error','message':('Book discovery' if discovery else 'Progress sync')
                    +' could not finish. Check the connection and try again.'}
        try:
            result.update(started_at=started,finished_at=datetime.now(timezone.utc).isoformat())
            if discovery:
                private_json(self.root/'web-last-discovery.json',result)
                self.last_discovery=result
            else:
                private_json(self.root/'web-last-sync.json',result)
                self.last=result
        finally:
            self.phase='Ready'
            self.running_job=None
            self.lock.release()


TEMPLATES = Path(__file__).with_name('templates')
PAGE_SHELL = (TEMPLATES/'base.html').read_text(encoding='utf-8')
PAGE = PAGE_SHELL + (TEMPLATES/'sync.html').read_text(encoding='utf-8')
SETUP_PAGE = PAGE_SHELL + (TEMPLATES/'setup.html').read_text(encoding='utf-8')


def handler_for(jobs,hosts,networks):
    csrf=secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def allowed(self):
            try:ip=ipaddress.ip_address(self.client_address[0])
            except ValueError:return False
            return self.headers.get('Host') in hosts and any(ip in n for n in networks)
        def send(self,status,body,kind='application/json',nonce=None):
            data=body.encode() if isinstance(body,str) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type',kind+'; charset=utf-8')
            self.send_header('Content-Length',str(len(data)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',"default-src 'none'; connect-src 'self'; script-src 'nonce-"+(nonce or '')+"'; style-src 'nonce-"+(nonce or '')+"'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers();self.wfile.write(data)
        def do_GET(self):
            if not self.allowed():self.send(403,{'error':'Use the home network or WireGuard VPN.'});return
            if self.path in ('/','/setup'):
                nonce=secrets.token_urlsafe(24)
                page=SETUP_PAGE if self.path=='/setup' else PAGE
                self.send(200,page.replace('NONCE',nonce).replace('CSRF',csrf),'text/html',nonce)
            elif self.path=='/api/status':self.send(200,jobs.status())
            else:self.send(404,{'error':'Not found'})
        def do_POST(self):
            if not self.allowed():self.send(403,{'error':'Private network required'});return
            if (self.headers.get('Origin')!='http://'+self.headers.get('Host','')
                    or not secrets.compare_digest(self.headers.get('X-Placekeeper',''),csrf)):
                self.send(403,{'error':'Reload this page and try again.'});return
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=8192:raise ValueError('Invalid request size')
                data=json.loads(self.rfile.read(size))
                if not isinstance(data,dict):raise ValueError('Invalid request')
            except (ValueError,UnicodeError):self.send(400,{'error':'Invalid request'});return
            if self.path=='/api/connect':
                try:jobs.connect(str(data.get('username','')).strip(),str(data.get('password','')),data.get('remember') is True)
                except ValueError as e:self.send(400,{'error':str(e)});return
                self.send(200,{'connected':True})
            elif self.path=='/api/setup':
                try:result=jobs.setup_action(data.get('service'),data.get('action'),data)
                except (ValueError,ReadFailure) as e:self.send(400,{'error':str(e)});return
                except (httpx.HTTPError,OSError,KeyError,TypeError):self.send(502,{'error':'Could not complete setup. Check the service and try again.'});return
                self.send(200,result)
            elif self.path=='/api/schedule':
                try:jobs.set_schedule(data.get('enabled'))
                except ValueError as e:self.send(400,{'error':str(e)});return
                except OSError:self.send(500,{'error':'Could not save the schedule.'});return
                self.send(200,{'enabled':jobs.schedule_enabled})
            elif self.path in ('/api/sync','/api/discover'):
                try:started=jobs.start('discovery' if self.path=='/api/discover' else 'progress')
                except ValueError as e:self.send(400,{'error':str(e)});return
                self.send(202 if started else 409,{'started':started,'error':None if started else 'Another job or setup action is already running.'})
            else:self.send(404,{'error':'Not found'})
    return Handler


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,default=ROOT/'.private/web-config.json')
    p.add_argument('--data-root',type=Path,default=ROOT/'.private')
    args=p.parse_args();os.umask(0o077)
    config=json.loads(args.config.read_text())
    jobs=Jobs(args.data_root.resolve())
    handler=handler_for(jobs,set(config['hosts']),[ipaddress.ip_network(n) for n in config['networks']])
    server=ThreadingHTTPServer((config.get('bind','0.0.0.0'),int(config.get('port',8780))),handler)
    server.daemon_threads=True
    scheduler=threading.Thread(target=jobs.schedule,daemon=True)
    scheduler.start()
    print('Placekeeper page ready.',flush=True)
    try:server.serve_forever()
    finally:
        jobs.stop_scheduler()
        scheduler.join()
        server.server_close()


if __name__=='__main__':main()
