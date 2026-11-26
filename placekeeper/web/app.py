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
import shutil
from urllib.parse import urlsplit
from time import monotonic, time

from placekeeper.sync.worker import run_worker
from placekeeper.monitoring import Monitor, discovery_problem, pending_write
from placekeeper.sync import book_status
from placekeeper.web.setup import Connections
from placekeeper.errors import ReadFailure
from placekeeper.storage import default_data_root, onboarding_lock, private_json, state_lock


PROGRESS_INTERVAL = 30 * 60
DISCOVERY_INTERVAL = 6 * 60 * 60


class Jobs:
    def __init__(self,data_root,onboard=None,sync=None,plugins=None):
        self.root=Path(data_root)
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.connections=Connections(self.root,plugins)
        self.custom_jobs=onboard is not None or sync is not None
        self.use_worker=not self.custom_jobs and plugins is None
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
        self.discovery_enabled=self.load('web-schedule.json', {}).get('discovery_enabled') is True
        self.next_discovery=monotonic() if self.discovery_enabled else None
        self.scheduler_stopped=False
        self.monitor=Monitor(self.root)
        self.next_health=0

    def load(self, name, default):
        path=self.root/name
        return json.loads(path.read_text()) if path.exists() else default

    @property
    def running(self):
        return self.running_job is not None

    def discovery_ready(self, plugins=None):
        plugins=self.connections.connected() if plugins is None else plugins
        return any(plugin.discover is not None and all(key in plugins for key in plugin.catalog_requires)
                   for plugin in plugins.values())

    def status(self):
        books=list(book_status.load(self.root).values())
        services=self.connections.connected()
        return {'health':self.monitor.snapshot(),
                'books':[book for book in books if not book.get('removed')],
                'removed_books':[book for book in books if book.get('removed')],
                'connected':self.connections.ready(services), 'services':self.connections.status(services),
                'can_discover':self.discovery_ready(services), 'running_job':self.running_job,
                'running':self.running,'phase':self.phase,'last':self.last,
                'last_discovery':self.last_discovery,'schedule_enabled':self.schedule_enabled, 'discovery_enabled':self.discovery_enabled}

    def discover(self):
        if self.use_worker:
            return run_worker(self.root, 'discovery', lambda phase:setattr(self, 'phase', phase))
        from placekeeper.sync.job import refresh_libraries
        return refresh_libraries(self.root,self.connections.connected())

    def sync_books(self,notify):
        if self.use_worker:
            return run_worker(self.root, 'progress', notify)
        from placekeeper.sync.job import run_books
        return run_books(self.root,self.connections.connected(),notify)

    def set_book_status(self, key, mode):
        self.change_book(book_status.save_mode, key, mode)

    def set_book_removed(self, key, removed):
        self.change_book(book_status.save_removed, key, removed)

    def change_book(self, action, key, value):
        if not self.lock.acquire(blocking=False):
            raise ValueError('Wait for the current job to finish.')
        try:
            with state_lock(self.root/'manual-sync-job'), onboarding_lock(self.root):
                action(self.root, key, value)
        finally:
            self.lock.release()

    def check_health(self):
        enabled={'progress':self.schedule_enabled, 'discovery':self.discovery_enabled}
        services=self.connections.connected()
        ready={'progress':self.custom_jobs or self.connections.ready(services),
               'discovery':self.custom_jobs or self.discovery_ready(services)}
        self.monitor.tick(enabled, ready)

    def set_health(self, data):
        if not self.lock.acquire(blocking=False):
            raise ValueError('Wait for the current job to finish.')
        try:
            self.monitor.configure(data)
            self.check_health()
            with self.schedule_changed:
                self.schedule_changed.notify()
        finally:
            self.lock.release()

    def set_schedule(self, enabled, discovery_enabled=None):
        if type(enabled) is not bool:
            raise ValueError('Choose whether to enable automatic progress sync.')
        if discovery_enabled is None:
            discovery_enabled = self.discovery_enabled
        if type(discovery_enabled) is not bool:
            raise ValueError('Choose whether to discover books automatically.')
        with self.schedule_changed:
            if enabled != self.schedule_enabled or discovery_enabled != self.discovery_enabled:
                private_json(self.root/'web-schedule.json', {'enabled': enabled, 'discovery_enabled': discovery_enabled})
                if discovery_enabled != self.discovery_enabled:
                    self.discovery_enabled=discovery_enabled
                    self.next_discovery=monotonic() if discovery_enabled else None
                if enabled != self.schedule_enabled:
                    self.schedule_enabled=enabled
                    self.next_sync=monotonic()+PROGRESS_INTERVAL if enabled else None
                self.check_health()
                self.schedule_changed.notify()

    def schedule(self):
        with self.schedule_changed:
            while not self.scheduler_stopped:
                now=monotonic()
                if now>=self.next_health:
                    try:self.check_health()
                    except (OSError, ValueError):pass
                    self.next_health=now+60
                due=[(stamp,job,False) for stamp,job in
                     ((self.next_sync,'progress'),(self.next_discovery,'discovery')) if stamp is not None]
                due.extend((now+max(0,stamp-time()),job,True) for job,stamp in self.monitor.retries().items())
                if not due:
                    self.schedule_changed.wait(max(0,self.next_health-monotonic()))
                    continue
                stamp,job,retry=min(due)
                remaining=min(stamp,self.next_health)-monotonic()
                if remaining>0:
                    self.schedule_changed.wait(remaining)
                    continue
                if stamp>monotonic():
                    continue
                if retry:
                    self.monitor.defer_retry(job)
                elif job=='discovery':self.next_discovery=monotonic()+DISCOVERY_INTERVAL
                else:self.next_sync=monotonic()+PROGRESS_INTERVAL
                try:
                    started=self.start(job, scheduled=True, retry=retry)
                    if not started and not retry:
                        if job=='discovery':self.next_discovery=monotonic()+60
                        else:self.next_sync=monotonic()+60
                except ValueError:
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
                result=self.connections.apply(service,action,data)
                self.check_health()
                return result
        finally:
            self.lock.release()

    def start(self, job='progress', *, scheduled=False, retry=False):
        if not self.lock.acquire(blocking=False):
            return False
        try:
            if not self.custom_jobs:
                if job=='discovery' and not self.discovery_ready():
                    raise ValueError('Connect a book discovery source in Setup.')
                if job=='progress' and not self.connections.ready():
                    raise ValueError('Connect two readers and a book catalog in Setup.')
            self.monitor.begin(job, retry)
            self.running_job=job
            self.phase='Finding new books…' if job=='discovery' else 'Checking progress…'
            threading.Thread(target=self.run,args=(job,scheduled),daemon=True).start()
        except BaseException:
            self.running_job=None
            self.lock.release()
            raise
        return True

    def run(self, job, scheduled=False):
        started=datetime.now(timezone.utc).isoformat()
        discovery=job=='discovery'
        retryable=False
        try:
            with state_lock(self.root/'manual-sync-job'):
                if discovery:
                    counts=self.onboard()
                    result={'discovery':counts}
                    failed,retryable=discovery_problem(counts)
                else:
                    books=self.sync(lambda text:setattr(self,'phase',text))
                    result={'books':books}
                    failures=[book for book in books if book['status'] in ('error','unconfirmed','changed')]
                    failed=bool(failures)
                    retryable=bool(failures) and all(book.get('retryable') is True and book.get('writes',0)==0 for book in failures)
                result['status']='needs_attention' if failed else 'complete'
        except Exception:
            retryable=True
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
            retryable=retryable and (discovery or not pending_write(self.root))
            self.monitor.finish(job,result,scheduled=scheduled,retryable=retryable)
            with self.schedule_changed:
                self.schedule_changed.notify()
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
        timeout=15
        def log_message(self,*_):pass
        def log_request(self, code='-', size='-'):
            # Catalog diagnostics omit query strings, credentials and book paths.
            if not hasattr(self,'path'):
                tls=getattr(self,'raw_requestline',b'').startswith(b'\x16\x03')
                print('Catalog invalid HTTP request tls='+str(tls),flush=True)
                return
            path=urlsplit(self.path).path
            if path in ('/', '/opds', '/opds/'):
                print('Catalog HTTP '+path+' status='+str(code)+' host='+self.headers.get('Host','')[:100],flush=True)
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
            elif urlsplit(self.path).path in ('/opds','/opds/'):
                from placekeeper.plugins.library.catalog import feed
                self.send(200,feed(jobs.root),'application/atom+xml;profile=opds-catalog;kind=acquisition')
            elif self.path.startswith('/epubs/'):
                from placekeeper.plugins.library.storage import book_path, records
                parts=urlsplit(self.path).path.split('/')
                try:
                    if len(parts)!=4:raise ValueError('Unknown EPUB.')
                    path=book_path(jobs.root,parts[2])
                    if not path.is_file():raise ValueError('Unknown EPUB.')
                    book=next(b for b in records(jobs.root) if b['id']==parts[2])
                except (ValueError,StopIteration):self.send(404,{'error':'Not found'});return
                self.send_response(200)
                self.send_header('Content-Type','application/epub+zip')
                self.send_header('Content-Length',str(path.stat().st_size))
                from urllib.parse import quote
                self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+quote(book['filename']))
                self.send_header('X-Content-Type-Options','nosniff')
                self.end_headers()
                with path.open('rb') as source:shutil.copyfileobj(source,self.wfile,65536)
            elif self.path=='/api/library':
                from placekeeper.plugins.library.storage import records
                self.send(200,{'books':records(jobs.root)})
            elif self.path=='/api/status':self.send(200,jobs.status())
            elif self.path=='/api/health':
                jobs.check_health()
                health=jobs.monitor.snapshot()
                self.send(200 if health['status']=='healthy' else 503,health)
            else:self.send(404,{'error':'Not found'})
        def do_POST(self):
            if not self.allowed():self.send(403,{'error':'Private network required'});return
            if (self.headers.get('Origin')!='http://'+self.headers.get('Host','')
                    or not secrets.compare_digest(self.headers.get('X-Placekeeper',''),csrf)):
                self.send(403,{'error':'Reload this page and try again.'});return
            if self.path=='/api/library/upload':
                from placekeeper.plugins.library.storage import save
                if not jobs.lock.acquire(blocking=False):
                    self.send(409,{'error':'Wait for the current job to finish.'});return
                try:
                    if self.headers.get('Content-Type')!='application/epub+zip':raise ValueError('Choose an EPUB file.')
                    size=int(self.headers.get('Content-Length','0'))
                    with onboarding_lock(jobs.root):book=save(jobs.root,self.rfile,size)
                    self.send(201,book)
                except ValueError as e:self.close_connection=True;self.send(400,{'error':str(e)})
                except OSError:self.close_connection=True;self.send(500,{'error':'Could not store the EPUB.'})
                finally:jobs.lock.release()
                return
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
                import httpx
                try:result=jobs.setup_action(data.get('service'),data.get('action'),data)
                except (ValueError,ReadFailure) as e:self.send(400,{'error':str(e)});return
                except (httpx.HTTPError,OSError,KeyError,TypeError):self.send(502,{'error':'Could not complete setup. Check the service and try again.'});return
                self.send(200,result)
            elif self.path=='/api/health-test':
                try:jobs.monitor.test_alert()
                except ValueError as e:self.send(400,{'error':str(e)});return
                except OSError:self.send(500,{'error':'Could not save the test alert.'});return
                self.send(200,{'saved':True})
            elif self.path=='/api/health-config':
                try:jobs.set_health(data)
                except ValueError as e:self.send(400,{'error':str(e)});return
                except OSError:self.send(500,{'error':'Could not save health settings.'});return
                self.send(200,jobs.monitor.snapshot()['config'])
            elif self.path=='/api/book-removed':
                try:jobs.set_book_removed(data.get('key'), data.get('removed'))
                except ValueError as e:self.send(400,{'error':str(e)});return
                except OSError:self.send(500,{'error':'Could not save book removal.'});return
                self.send(200,{'saved':True})
            elif self.path=='/api/book-status':
                try:jobs.set_book_status(data.get('key'), data.get('mode'))
                except ValueError as e:self.send(400,{'error':str(e)});return
                except OSError:self.send(500,{'error':'Could not save book status.'});return
                self.send(200,{'saved':True})
            elif self.path=='/api/schedule':
                try:jobs.set_schedule(data.get('enabled'), data.get('discovery_enabled'))
                except ValueError as e:self.send(400,{'error':str(e)});return
                except OSError:self.send(500,{'error':'Could not save the schedule.'});return
                self.send(200,{'enabled':jobs.schedule_enabled, 'discovery_enabled':jobs.discovery_enabled})
            elif self.path in ('/api/sync','/api/discover'):
                try:started=jobs.start('discovery' if self.path=='/api/discover' else 'progress')
                except ValueError as e:self.send(400,{'error':str(e)});return
                self.send(202 if started else 409,{'started':started,'error':None if started else 'Another job or setup action is already running.'})
            else:self.send(404,{'error':'Not found'})
    return Handler


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, help='Override the web configuration file')
    p.add_argument('--data-root',type=Path,default=default_data_root())
    args=p.parse_args();os.umask(0o077)
    data_root = args.data_root.expanduser().resolve()
    config_path = args.config or data_root/'web-config.json'
    config = json.loads(config_path.expanduser().read_text())
    jobs=Jobs(data_root)
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
