"""Explicit, duplicate-protected EPUB delivery through authenticated SMTP."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re

from placekeeper.plugins.library.storage import book_path, record
from placekeeper.storage import file_sha256, private_json, state_lock

CONFIG='kindle-email.json'
LEDGER='kindle-email-deliveries.json'
MAX_ATTACHMENT=20*1024*1024


def load(root, name):
    path=Path(root)/name
    return json.loads(path.read_text()) if path.exists() else {}


def settings(root):
    config=load(root,CONFIG)
    return {**{k:v for k,v in config.items() if k!='password'},
            'configured':bool(config), 'password_saved':bool(config.get('password'))}


def configure(root, data):
    with state_lock(Path(root)/'kindle-email'):
        if data.get('clear') is True:
            (Path(root)/CONFIG).unlink(missing_ok=True)
            return settings(root)
        if data.get('consent') is not True:raise ValueError('Confirm saving your email credentials on this installation.')
        config={key:str(data.get(key,'')).strip() for key in ('host','username','sender','recipient','security')}
        if not re.fullmatch(r'[A-Za-z0-9.-]{1,253}',config['host']):raise ValueError('Enter an SMTP hostname.')
        for key in ('sender','recipient'):
            if not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+',config[key]):raise ValueError('Enter valid sender and Kindle email addresses.')
        config['recipient']=config['recipient'].lower()
        if config['recipient'].split('@')[1] not in ('kindle.com','free.kindle.com'):raise ValueError('Use your Amazon Send to Kindle address ending in @kindle.com.')
        if config['security'] not in ('ssl','starttls'):raise ValueError('Choose TLS or STARTTLS.')
        try:config['port']=int(data.get('port',0))
        except (ValueError,TypeError):raise ValueError('Enter a valid SMTP port.') from None
        if not 1<=config['port']<=65535:raise ValueError('Enter a valid SMTP port.')
        config['password']=str(data.get('password') or load(root,CONFIG).get('password',''))
        if not config['username'] or not config['password']:raise ValueError('Enter SMTP credentials (an app password if required).')
        private_json(Path(root)/CONFIG,config)
    return settings(root)


def delivery_key(book, recipient):
    return sha256((book['sha256']+':'+recipient).encode()).hexdigest()


def library_status(root, books):
    config=load(root,CONFIG);ledger=load(root,LEDGER)
    return {'configured':bool(config), 'deliveries':{
        b['id']:ledger.get(delivery_key(b,config.get('recipient','')),{}).get('status','not_sent') for b in books}}


def send_book(root, key):
    # Imported only by the short-lived email worker, never for web settings/status.
    from email.message import EmailMessage
    from email.utils import make_msgid
    import smtplib
    import ssl
    root=Path(root)
    with state_lock(root/'kindle-email'):
        config=load(root,CONFIG)
        if not config:raise ValueError('Configure Send to Kindle email in Setup first.')
        if not isinstance(key,str):raise ValueError('Unknown EPUB.')
        path=book_path(root,key)
        try:book=record(root,key)
        except FileNotFoundError:raise ValueError('Unknown EPUB.') from None
        if path.stat().st_size>MAX_ATTACHMENT:raise ValueError('Email delivery supports EPUBs up to 20 MiB. Use Amazon’s upload page for larger books.')
        if file_sha256(path)!=book['sha256']:raise ValueError('EPUB fingerprint changed. Upload it again before sending.')
        ledger=load(root,LEDGER);identity=delivery_key(book,config['recipient'])
        old=ledger.get(identity,{})
        if old.get('status') in ('sent','sending','uncertain'):
            return {'status':old['status'],'duplicate':True,
                    'message':'Already accepted by the email server.' if old['status']=='sent' else 'Delivery is uncertain. Check Kindle and your sent mail before retrying; automatic resends are blocked.'}
        message=EmailMessage()
        message['From']=config['sender'];message['To']=config['recipient']
        message['Subject']=book['title'];message['Message-ID']=make_msgid(domain='placekeeper.local')
        message.set_content('EPUB sent explicitly from your Placekeeper library.')
        message.add_attachment(path.read_bytes(),maintype='application',subtype='epub+zip',filename=book['filename'])
        smtp=None;attempted=False
        result={'status':'failed','message':'Could not connect or authenticate. Check email settings, then retry.'}
        try:
            context=ssl.create_default_context()
            if config['security']=='ssl':smtp=smtplib.SMTP_SSL(config['host'],config['port'],timeout=30,context=context)
            else:
                smtp=smtplib.SMTP(config['host'],config['port'],timeout=30)
                smtp.ehlo();smtp.starttls(context=context);smtp.ehlo()
            smtp.login(config['username'],config['password'])
            # Persist before DATA: a crash or lost SMTP acknowledgement must not
            # cause a second click/restart to send another copy automatically.
            ledger[identity]={'status':'sending','at':datetime.now(timezone.utc).isoformat(),'message_id':message['Message-ID']}
            private_json(root/LEDGER,ledger);attempted=True
            smtp.send_message(message,from_addr=config['sender'],to_addrs=[config['recipient']])
            result={'status':'sent','message':'Accepted by the email server. Amazon processing and Kindle delivery are not yet confirmed.'}
        except (smtplib.SMTPRecipientsRefused,smtplib.SMTPSenderRefused,smtplib.SMTPDataError):
            result={'status':'failed','message':'Email server rejected delivery. Check settings and provider attachment limits, then retry.'}
        except (smtplib.SMTPException,OSError):
            if attempted:result={'status':'uncertain','message':'Connection ended during delivery. Check Kindle and your sent mail; resends are blocked to prevent duplicates.'}
        finally:
            if smtp is not None:
                try:smtp.close()
                except (OSError,smtplib.SMTPException):pass
        ledger[identity]={**ledger.get(identity,{}),'status':result['status'],'at':datetime.now(timezone.utc).isoformat()}
        private_json(root/LEDGER,ledger)
        return result
