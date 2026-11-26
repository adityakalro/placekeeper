import io
import json
from pathlib import Path
import smtplib
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from placekeeper.plugins.kindle import email_delivery as mail
from placekeeper.plugins.library.storage import save

FIXTURE=Path(__file__).parent/'fixtures/Placekeeper Progress Test.epub'
CONFIG={'host':'smtp.example.com','port':465,'security':'ssl','username':'reader@example.com',
        'password':'private-app-password','sender':'reader@example.com','recipient':'reader@kindle.com','consent':True}

class KindleEmailTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.root=Path(self.tmp.name)
        data=FIXTURE.read_bytes();self.book=save(self.root,io.BytesIO(data),len(data))
        self.smtp=Mock()
        self.patch=patch.object(smtplib,'SMTP_SSL',return_value=self.smtp);self.factory=self.patch.start()
    def tearDown(self):self.patch.stop();self.tmp.cleanup()
    def configure(self):mail.configure(self.root,CONFIG)
    def test_upload_never_sends_and_requires_configuration(self):
        self.factory.assert_not_called()
        with self.assertRaises(ValueError):mail.send_book(self.root,self.book['id'])
        self.factory.assert_not_called()
    def test_credentials_private_redacted_and_retained_on_edit(self):
        result=mail.configure(self.root,CONFIG)
        self.assertNotIn('password',result)
        self.assertEqual((self.root/mail.CONFIG).stat().st_mode & 0o777,0o600)
        mail.configure(self.root,{**CONFIG,'password':''})
        self.assertEqual(mail.load(self.root,mail.CONFIG)['password'],CONFIG['password'])
        with self.assertRaises(ValueError):mail.configure(self.root,{**CONFIG,'recipient':'stranger@example.com'})
        with self.assertRaises(ValueError):mail.configure(self.root,{**CONFIG,'security':'plain'})
    def test_send_attachment_and_deduplicate_after_reload_and_reupload(self):
        self.configure();result=mail.send_book(self.root,self.book['id'])
        self.assertEqual(result['status'],'sent')
        self.smtp.login.assert_called_once_with(CONFIG['username'],CONFIG['password'])
        message=self.smtp.send_message.call_args.args[0]
        attachment=list(message.iter_attachments())[0]
        self.assertEqual(attachment.get_payload(decode=True),FIXTURE.read_bytes())
        self.assertEqual(attachment.get_filename(),self.book['filename'])
        data=FIXTURE.read_bytes();save(self.root,io.BytesIO(data),len(data))
        self.assertTrue(mail.send_book(self.root,self.book['id'])['duplicate'])
        self.assertEqual(self.smtp.send_message.call_count,1)
        mail.configure(self.root,{'clear':True});self.configure()
        self.assertTrue(mail.send_book(self.root,self.book['id'])['duplicate'])
    def test_ambiguous_send_blocks_retry(self):
        self.configure();self.smtp.send_message.side_effect=smtplib.SMTPServerDisconnected('secret server diagnostic')
        result=mail.send_book(self.root,self.book['id'])
        self.assertEqual(result['status'],'uncertain')
        self.assertNotIn('secret',str(result))
        self.assertTrue(mail.send_book(self.root,self.book['id'])['duplicate'])
        self.assertEqual(self.smtp.send_message.call_count,1)
    def test_auth_failure_and_explicit_rejection_allow_manual_retry(self):
        self.configure();self.smtp.login.side_effect=smtplib.SMTPAuthenticationError(535,b'secret')
        self.assertEqual(mail.send_book(self.root,self.book['id'])['status'],'failed')
        self.smtp.send_message.assert_not_called();self.smtp.login.side_effect=None
        self.smtp.send_message.side_effect=smtplib.SMTPDataError(552,b'too big')
        self.assertEqual(mail.send_book(self.root,self.book['id'])['status'],'failed')
        self.smtp.send_message.side_effect=None
        self.assertEqual(mail.send_book(self.root,self.book['id'])['status'],'sent')
    def test_crashed_send_stays_blocked(self):
        self.configure()
        mail.private_json(self.root/mail.LEDGER,{mail.delivery_key(self.book,CONFIG['recipient']):{'status':'sending'}})
        self.assertTrue(mail.send_book(self.root,self.book['id'])['duplicate'])
        self.factory.assert_not_called()
    def test_starttls_and_target_specific_protection(self):
        self.configure()
        mail.configure(self.root,{**CONFIG,'security':'starttls','port':587})
        with patch.object(smtplib,'SMTP',return_value=self.smtp):
            self.assertEqual(mail.send_book(self.root,self.book['id'])['status'],'sent')
            self.smtp.starttls.assert_called_once()
        mail.configure(self.root,{**CONFIG,'recipient':'other@kindle.com'})
        self.assertEqual(mail.send_book(self.root,self.book['id'])['status'],'sent')
    def test_corrupt_epub_is_not_sent(self):
        self.configure();mail.book_path(self.root,self.book['id']).write_bytes(b'changed')
        with self.assertRaises(ValueError):mail.send_book(self.root,self.book['id'])
        self.factory.assert_not_called()


class EmailWorkerTests(unittest.TestCase):
    def test_real_worker_process_sends_once_and_releases_itself(self):
        import os
        import subprocess
        import sys
        from placekeeper.sync.worker import run_worker
        source = r"""
import json,os,pathlib,smtplib,sys
root=pathlib.Path(sys.argv[sys.argv.index('--data-root')+1])
class FakeSMTP:
    def __init__(self,*args,**kwargs):pass
    def login(self,*args):pass
    def send_message(self,message,**kwargs):
        # Exercise MIME serialization without making any network connection.
        message.as_bytes()
        with (root/'smtp-calls').open('a') as output:output.write(str(os.getpid())+'\n')
    def close(self):pass
smtplib.SMTP_SSL=FakeSMTP
from placekeeper.sync.worker import main
sys.argv=['worker',*sys.argv[1:]]
main()
"""
        launch=subprocess.Popen
        def child(command,**kwargs):
            return launch([command[0],'-c',source,*command[3:]],**kwargs)
        with TemporaryDirectory() as directory:
            root=Path(directory);data=FIXTURE.read_bytes()
            book=save(root,io.BytesIO(data),len(data));mail.configure(root,CONFIG)
            with patch('placekeeper.sync.worker.subprocess.Popen',side_effect=child):
                self.assertEqual(run_worker(root,'email',lambda _:None,book_id=book['id'])['status'],'sent')
                self.assertTrue(run_worker(root,'email',lambda _:None,book_id=book['id'])['duplicate'])
            calls=(root/'smtp-calls').read_text().splitlines();self.assertEqual(len(calls),1)
            pid=int(calls[0]);self.assertNotEqual(pid,os.getpid())
            with self.assertRaises(ProcessLookupError):os.kill(pid,0)

    def test_worker_rejects_invalid_identity_before_launch(self):
        from placekeeper.sync.worker import run_worker
        with patch('placekeeper.sync.worker.subprocess.Popen') as launch:
            with self.assertRaises(ValueError):run_worker(Path('/tmp'),'email',lambda _:None,book_id='../secret')
            launch.assert_not_called()
