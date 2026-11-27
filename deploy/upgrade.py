"""Transactional checkout upgrade. Standard library only; may run from /tmp.

Never registers accounts, sends mail, or changes reading positions.
"""
import argparse
from contextlib import contextmanager
import fcntl
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen


def run(*args, cwd=None, capture=False, check=True, env=None):
    return subprocess.run(args,cwd=cwd,check=check,env=env,text=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def data_directory(checkout, override=None):
    if override:return Path(override).expanduser().resolve()
    if os.environ.get('PLACEKEEPER_DATA_DIR'):
        return Path(os.environ['PLACEKEEPER_DATA_DIR']).expanduser().resolve()
    legacy=checkout/'.private'
    return legacy.resolve() if legacy.exists() else Path.home()/'.config/placekeeper'


def copy_private(source, destination):
    """Preserve modes, symlinks and service-account ownership during rollback."""
    shutil.copytree(source,destination,symlinks=True)
    if hasattr(os,'geteuid') and os.geteuid()==0:
        for base,dirs,files in os.walk(source,followlinks=False):
            for name in ['.',*dirs,*files]:
                original=Path(base)/name;target=destination/original.relative_to(source)
                info=original.lstat();os.chown(target,info.st_uid,info.st_gid,follow_symlinks=False)


def owns_data(root):
    if hasattr(os,'geteuid') and os.geteuid()==0:
        owner=root.stat()
        for base,dirs,files in os.walk(root,followlinks=False):
            for name in ['.',*dirs,*files]:
                os.chown(Path(base)/name,owner.st_uid,owner.st_gid,follow_symlinks=False)


def endpoint(config):
    bind=config.get('bind','127.0.0.1');port=int(config.get('port',8780))
    address='127.0.0.1' if bind=='0.0.0.0' else bind
    if address=='::':address='::1'
    url='http://'+('['+address+']' if ':' in address else address)+':'+str(port)+'/api/status'
    hosts=config.get('hosts',[])
    if not hosts:raise ValueError('Web configuration has no allowed hosts.')
    return url,hosts[0],address,port


def status(config):
    url,host,_,_=endpoint(config)
    with urlopen(Request(url,headers={'Host':host}),timeout=3) as response:
        return json.load(response)


class Service:
    def __init__(self,args):
        self.command=(['sv'] if args.termux_service else ['systemctl']+(['--user'] if args.user_service else []))
        self.name=args.termux_service or args.service
        self.termux=bool(args.termux_service)
    def active(self):
        if not self.name:return False
        command=self.command+(['status',self.name] if self.termux else ['is-active','--quiet',self.name])
        result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        return result.returncode==0 and (not self.termux or result.stdout.startswith('run:'))
    def stop(self):run(*self.command, 'down' if self.termux else 'stop',self.name)
    def start(self):run(*self.command, 'up' if self.termux else 'start',self.name)


def size(root):
    return sum(p.lstat().st_size for p in root.rglob('*') if not p.is_symlink() and p.is_file())


def _upgrade(args):
    checkout=Path(args.checkout).expanduser().resolve()
    root=data_directory(checkout,args.data_root)
    if sys.version_info<(3,13):raise ValueError('Install Python 3.13 or newer before upgrading.')
    if run('git','status','--porcelain',cwd=checkout,capture=True).strip():
        raise ValueError('Commit or stash local changes and untracked files before upgrading. Private ignored files are preserved.')
    old=run('git','rev-parse','HEAD',cwd=checkout,capture=True).strip()
    if not args.no_fetch:run('git','fetch','origin',cwd=checkout)
    target=run('git','rev-parse','--verify',args.ref+'^{commit}',cwd=checkout,capture=True).strip()
    ancestry=subprocess.run(['git','merge-base','--is-ancestor',old,target],cwd=checkout)
    if ancestry.returncode:raise ValueError('Target is not a fast-forward upgrade. No installation files were changed.')
    installer=run('git','show',target+':install.sh',cwd=checkout,capture=True)
    if 'PLACEKEEPER_SKIP_SYSTEM_PACKAGES' not in installer:
        raise ValueError('Target predates the safe upgrade installer. Select the current branch or use the manual procedure.')
    if not root.is_dir():raise ValueError('Existing data directory not found. Specify --data-root; use install.sh for new installations.')
    if root==checkout or checkout.is_relative_to(root):raise ValueError('The data directory cannot contain the Git checkout.')
    config=json.loads((root/'web-config.json').read_text());endpoint(config)
    if (checkout/'.venv').is_symlink():raise ValueError('The virtual environment is a symlink. Use the manual upgrade procedure.')
    for path in root.rglob('*'):
        if path.is_symlink() and not path.resolve().is_relative_to(root):
            raise ValueError('Data contains an external symlink. Back up its target and use the manual upgrade procedure.')
    service=Service(args);was_active=service.active()
    if args.user_service and not args.service:raise ValueError('--user-service requires --service.')
    if service.name and not service.termux:
        loaded=run(*service.command,'show','-p','LoadState','--value',service.name,capture=True).strip()
        if loaded!='loaded':raise ValueError('Service not installed. Specify its existing unit name or use a stopped manual installation.')
        for legacy in ('placekeeper-onboarding.service','book-sync-onboarding.service'):
            if subprocess.run([*service.command,'is-active','--quiet',legacy]).returncode==0:
                raise ValueError('Stop and disable '+legacy+' before upgrading; the web service owns scheduling.')
    if was_active:
        deadline=time.monotonic()+args.wait
        while True:
            current=status(config)
            if not current.get('running') and not current.get('busy'):break
            if time.monotonic()>=deadline:raise ValueError('A job is still active. Upgrade left the installation unchanged; try again later.')
            time.sleep(1)
    else:
        _,_,address,port=endpoint(config)
        try:
            with socket.create_connection((address,port),timeout=.5):pass
        except OSError:pass
        else:raise ValueError('Placekeeper is still listening. Stop it or specify the service that manages it.')
    backup_base=Path(args.backup_dir).expanduser().resolve() if args.backup_dir else checkout.parent/'.placekeeper-upgrades'
    if backup_base.is_relative_to(root) or backup_base.is_relative_to(checkout):
        raise ValueError('Backups must be outside both the checkout and the data directory.')
    backup_base.mkdir(mode=0o700,parents=True,exist_ok=True)
    if backup_base.stat().st_mode & 0o077:raise ValueError('Backup directory must be private (chmod 700).')
    required=size(root)+size(checkout/'.venv')+64*1024*1024
    if shutil.disk_usage(backup_base).free<required:raise ValueError('Not enough free space for the data backup and a replacement virtual environment.')
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    backup=backup_base/(stamp+'-'+old[:8]);backup.mkdir(mode=0o700)
    manifest={'previous_commit':old,'target_commit':target,'checkout':str(checkout),'data_root':str(root),
              'service':service.name,'was_active':was_active,'status':'preparing'}
    manifest_path=backup/'upgrade.json'
    def record(state):
        manifest['status']=state;manifest_path.write_text(json.dumps(manifest,indent=2)+'\n');manifest_path.chmod(0o600)
    record('preparing')
    stopped=False;copied=False;moved_venv=False;updated=False;installing=False
    try:
        if was_active:service.stop();stopped=True
        copy_private(root,backup/'data');copied=True
        record('backed_up')
        if (checkout/'.venv').exists():
            shutil.move(str(checkout/'.venv'),str(backup/'venv'));moved_venv=True
        run('git','merge','--ff-only',target,cwd=checkout);updated=True
        environment={**os.environ,'PLACEKEEPER_DATA_DIR':str(root),'PLACEKEEPER_SKIP_SYSTEM_PACKAGES':'1'}
        installing=True
        run('bash','install.sh',cwd=checkout,env=environment)
        python=str(checkout/'.venv/bin/python')
        # The current app validates existing state without contacting providers.
        run(python,'-c','from pathlib import Path; from placekeeper.web.app import Jobs; '
            'import sys; Jobs(Path(sys.argv[1])).status()',str(root),cwd=checkout,env=environment)
        owns_data(root)
        if was_active:
            service.start()
            deadline=time.monotonic()+30
            while True:
                try:status(config);break
                except (URLError,OSError,ValueError):
                    if time.monotonic()>=deadline:raise RuntimeError('Upgraded web page did not become ready.')
                    time.sleep(.5)
        record('complete')
    except BaseException:
        try:record('rolling_back')
        except OSError:pass
        try:
            if stopped:service.stop()
            if updated:run('git','reset','--hard',old,cwd=checkout)
            if installing and (checkout/'.venv').exists():shutil.rmtree(checkout/'.venv')
            if moved_venv:shutil.move(str(backup/'venv'),str(checkout/'.venv'))
            if copied:
                shutil.rmtree(root);copy_private(backup/'data',root)
            if stopped:service.start()
        except BaseException as recovery:
            raise RuntimeError('Automatic rollback could not complete. Keep the service stopped and recover from '+str(backup)) from recovery
        try:record('rolled_back')
        except OSError:pass
        print('Upgrade failed; previous code, environment and data restored. Backup: '+str(backup),file=sys.stderr)
        raise
    print('Upgrade complete: '+target[:12]+'\nPrivate rollback backup: '+str(backup))
    if not was_active:print('Your installation was stopped; start it using your usual command.')
    return backup



@contextmanager
def upgrade_lock(checkout):
    relative=run('git','rev-parse','--git-path','placekeeper-upgrade.lock',cwd=checkout,capture=True).strip()
    path=Path(relative)
    if not path.is_absolute():path=checkout/path
    with path.open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('Another upgrade is already running.') from None
        yield


def upgrade(args):
    checkout=Path(args.checkout).expanduser().resolve()
    with upgrade_lock(checkout):return _upgrade(args)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout',default='.',help='Existing Git checkout (default: current directory)')
    parser.add_argument('--data-root',help='Existing data directory; otherwise use PLACEKEEPER_DATA_DIR, .private, or the current user’s home')
    parser.add_argument('--ref',default='origin/main',help='Target commit/ref; must be a fast-forward')
    parser.add_argument('--no-fetch',action='store_true',help='Use an already fetched local target')
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--service',help='Existing systemd unit name')
    group.add_argument('--termux-service',help='Existing termux-services/runit service name')
    parser.add_argument('--user-service',action='store_true',help='Use systemctl --user')
    parser.add_argument('--backup-dir',help='Private backup parent outside the checkout/data directory')
    parser.add_argument('--wait',type=int,default=300,help='Maximum seconds to wait for the current job')
    args=parser.parse_args()
    if args.wait<0:parser.error('--wait must not be negative')
    try:upgrade(args)
    except (ValueError,OSError,RuntimeError,subprocess.CalledProcessError) as error:
        parser.exit(1,'Upgrade stopped: '+str(error)+'\n')


if __name__=='__main__':main()
