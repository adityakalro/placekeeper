import io
import ipaddress
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from xml.etree import ElementTree as ET
from unittest.mock import Mock, patch

from placekeeper.plugins.library.storage import save, records, book_path, MAX_UPLOAD
from placekeeper.plugins.library.source import LibrarySource
from placekeeper.plugins.library.catalog import feed
from placekeeper.web.app import Jobs, handler_for

FIXTURE=Path(__file__).parent/'fixtures/Placekeeper Progress Test.epub'

class LibraryTests(unittest.TestCase):
    def test_upload_catalog_and_source_without_readest(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);data=FIXTURE.read_bytes()
            book=save(root,io.BytesIO(data),len(data))
            self.assertEqual(save(root,io.BytesIO(data),len(data)),book)
            self.assertEqual(len(records(root)),1)
            self.assertEqual(book_path(root,book['id']).read_bytes(),data)
            source=LibrarySource(root)
            try:
                self.assertEqual(source.books(),[book])
                self.assertEqual(source.download(book,root/'copies'),book_path(root,book['id']))
                self.assertFalse((root/'copies').exists())
            finally:source.close()
            xml=ET.fromstring(feed(root))
            link=xml.find('{*}entry/{*}link')
            self.assertIn(book['id'],link.attrib['href'])
            self.assertEqual(link.attrib['type'],'application/epub+zip')
            self.assertEqual(xml.find('{*}entry/{*}title').text+'.epub',book['filename'])
            self.assertIsNone(xml.find('{*}entry/{*}author'))

    def test_invalid_and_interrupted_uploads_publish_nothing(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            for data,size in [(b'bad',3),(b'bad',10),(b'',MAX_UPLOAD+1)]:
                with self.assertRaises(ValueError):save(root,io.BytesIO(data),size)
                self.assertEqual(records(root),[])
            with self.assertRaises(ValueError):book_path(root,'../../secret')

    def test_http_upload_download_and_csrf(self):
        with TemporaryDirectory() as directory:
            jobs=Jobs(directory,plugins={})
            server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(jobs,{'localhost'},[ipaddress.ip_network('127.0.0.0/8')]))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base='http://127.0.0.1:'+str(server.server_port)
            def request(path,data=None,**extra):
                headers={'Host':'localhost',**extra}
                return urlopen(Request(base+path,data=data,headers=headers))
            try:
                page=request('/').read().decode()
                import re
                token=re.search("'X-Placekeeper':'([^']+)'",page)[1]
                data=FIXTURE.read_bytes()
                with self.assertRaises(HTTPError) as error:request('/api/library/upload',data,**{'Content-Type':'application/epub+zip'})
                self.assertEqual(error.exception.code,403)
                error.exception.close()
                result=json.load(request('/api/library/upload',data,**{'Origin':'http://localhost','X-Placekeeper':token,'Content-Type':'application/epub+zip'}))
                self.assertEqual(request('/epubs/'+result['id']+'/'+result['filename'].replace(' ','%20')).read(),data)
                self.assertIn(result['id'],request('/opds').read().decode())
                self.assertIn(result['id'],request('/opds/').read().decode())
                self.assertIn(result['id'],request('/opds?offset=0').read().decode())
                from test_kindle_email import CONFIG
                auth={'Origin':'http://localhost','X-Placekeeper':token,'Content-Type':'application/json'}
                saved=json.load(request('/api/kindle-email',json.dumps(CONFIG).encode(),**auth))
                self.assertTrue(saved['configured']);self.assertNotIn('password',saved)
                self.assertNotIn('password',json.load(request('/api/kindle-email')))
                smtp=Mock()
                from placekeeper.plugins.kindle.email_delivery import send_book
                with patch('smtplib.SMTP_SSL',return_value=smtp), patch('placekeeper.web.app.run_worker',side_effect=lambda root,job,notify,**kw:send_book(root,kw['book_id'])) as worker:
                    payload=json.dumps({'id':result['id']}).encode()
                    sent=json.load(request('/api/library/send-kindle',payload,**auth))
                    self.assertEqual(sent['status'],'sent')
                    duplicate=json.load(request('/api/library/send-kindle',payload,**auth))
                    self.assertTrue(duplicate['duplicate']);self.assertEqual(smtp.send_message.call_count,1)
                    self.assertEqual(worker.call_args.args[1],'email')
                    self.assertEqual(worker.call_args.kwargs['timeout'],180)
                library=json.load(request('/api/library'))
                self.assertEqual(library['kindle_email']['deliveries'][result['id']],'sent')
                with self.assertRaises(HTTPError) as error:request('/epubs/invalid/test.epub')
                error.exception.close()
            finally:
                server.shutdown();server.server_close();thread.join()
