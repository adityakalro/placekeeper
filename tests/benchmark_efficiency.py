"""Offline Pi benchmarks: fake SMTP only, disposable EPUBs, no account access.

Run: python -m tests.benchmark_efficiency
"""
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from zipfile import ZipFile

ROOT=Path(__file__).resolve().parents[1]


def rss():
    status=Path('/proc/self/status')
    if status.exists():
        return int(next(line.split()[1] for line in status.read_text().splitlines() if line.startswith('VmRSS:')))/1024
    return int(subprocess.check_output(['ps','-o','rss=','-p',str(os.getpid())],text=True).strip())/1024


def peak():
    value=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value/(1024*1024 if sys.platform=='darwin' else 1024)


def fake_smtp():
    import smtplib
    class FakeSMTP:
        def __init__(self,*args,**kwargs):pass
        def login(self,*args):pass
        def send_message(self,message,**kwargs):
            # Includes stdlib MIME serialization, but no network transmission.
            message.as_bytes()
        def close(self):pass
    smtplib.SMTP_SSL=FakeSMTP


def email_case(mode, root, key):
    from placekeeper.web.app import Jobs
    from placekeeper.plugins.kindle.email_delivery import settings,send_book
    from placekeeper.sync.worker import run_worker
    Jobs(root,plugins={});settings(root)
    before=rss();started=time.perf_counter()
    if mode=='direct':
        fake_smtp();result=send_book(root,key)
    else:
        launch=subprocess.Popen
        def child(command,**kwargs):
            return launch([command[0],'-m','tests.benchmark_efficiency','worker',*command[3:]],**kwargs)
        subprocess.Popen=child
        try:result=run_worker(root,'email',lambda _:None,book_id=key)
        finally:subprocess.Popen=launch
    assert result['status']=='sent'
    print(json.dumps({'mode':mode,'parent_before_mib':round(before,2),'parent_after_mib':round(rss(),2),
                      'parent_peak_mib':round(peak(),2),'seconds':round(time.perf_counter()-started,3)}))


def mapping_case():
    from hashlib import sha256
    from placekeeper.plugins.kindle.onboarding import evaluate_indexes,text_evidence
    from placekeeper.mapping.text import TextIndex
    # Distinct prose avoids repeated-passage ambiguity in the original scan.
    text=''.join(sha256(str(i).encode()).hexdigest() for i in range(32768))
    first=TextIndex(text,[]);second=TextIndex((' '+text)[1:],[])
    started=time.perf_counter();old=(text_evidence(first,second),text_evidence(second,first))
    old_time=time.perf_counter()-started
    started=time.perf_counter();new=evaluate_indexes(first,second);new_time=time.perf_counter()-started
    assert all(item['adequate'] for item in old) and new['accepted']
    print(json.dumps({'mapping_characters':len(text),'context_scans_seconds':round(old_time,6),
                      'full_equality_seconds':round(new_time,6),'speedup':round(old_time/max(new_time,1e-9),1)}))


def main():
    if len(sys.argv)>1 and sys.argv[1]=='worker':
        fake_smtp()
        from placekeeper.sync.worker import main as worker
        sys.argv=['worker',*sys.argv[2:]];worker();return
    if len(sys.argv)>1 and sys.argv[1] in ('direct','isolated'):
        email_case(sys.argv[1],Path(sys.argv[2]),sys.argv[3]);return
    from placekeeper.plugins.library.storage import save
    from placekeeper.plugins.kindle.email_delivery import configure
    with tempfile.TemporaryDirectory() as directory:
        base=Path(directory);epub=base/'sample.epub'
        shutil.copyfile(ROOT/'tests/fixtures/Placekeeper Progress Test.epub',epub)
        with ZipFile(epub,'a') as archive:archive.writestr('benchmark.bin',os.urandom(8*1024*1024))
        for mode in ('direct','isolated'):
            root=base/mode;root.mkdir()
            with epub.open('rb') as stream:book=save(root,stream,epub.stat().st_size)
            configure(root,{'host':'smtp.example.invalid','port':465,'security':'ssl','username':'test@example.invalid',
                'password':'disposable','sender':'test@example.invalid','recipient':'test@kindle.com','consent':True})
            subprocess.run([sys.executable,'-m','tests.benchmark_efficiency',mode,str(root),book['id']],cwd=ROOT,check=True)
    mapping_case()


if __name__=='__main__':main()
