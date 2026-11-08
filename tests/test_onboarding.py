import contextlib
from hashlib import md5
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

import httpx

from placekeeper.plugins.kindle.catalog import AmazonCatalog
from placekeeper.plugins.kindle.onboarding import Onboarder
from placekeeper.plugins.kindle.store_mapping import StoreDeliveryDenied
from placekeeper.plugins.kindle.profiles import load_profile
from placekeeper.storage import private_json
from placekeeper.mapping.text import epub_index

DOC='AUTOMATICBOOK0001'
TITLE='Placekeeper Progress Test'


class FakeCatalog:
    def __init__(self):
        self.documents={DOC:{'document':DOC,'title':TITLE,'authors':['Placekeeper']}}
    def pull(self, previous):
        return {'cursor':'complete','documents':dict(self.documents),'pages':2}


class FakeReadest:
    def __init__(self):
        self.items=[{'id':'a'*28,'title':TITLE,'author':'Placekeeper'}]
        self.download_count=0
    def books(self):return list(self.items)
    def download(self, book, directory):
        self.download_count+=1
        directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
        p=directory/(book['id']+'.epub')
        shutil.copyfile(Path(__file__).parent/'fixtures/Placekeeper Progress Test.epub',p)
        return p


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=FakeReadest();self.catalog=FakeCatalog()
        self.downloads=0
        def probe(doc,auth,directory):
            private_json(Path(directory)/(doc+'-manifest.json'),{})
            return {'http_status':200}
        def download(doc,manifest,directory):
            self.downloads+=1
            (Path(directory)/(doc+'.azw8')).write_bytes(b'CONTfixture')
        def extract(engine,doc,directory):
            directory=directory/'map';directory.mkdir(parents=True,exist_ok=True)
            text=epub_index(Path(__file__).parent/'fixtures/Placekeeper Progress Test.epub').text
            private_json(directory/'content.json',{'data':[{'type':1,'content':text,'position':0}]})
            private_json(directory/'locations.json',[{'location':i+1,'position':p,'length':100}
                         for i,p in enumerate(range(0,len(text),100))])
            return directory/'content.json',directory/'locations.json'
        self.stack=contextlib.ExitStack()
        self.stack.enter_context(patch('placekeeper.plugins.kindle.onboarding.probe',side_effect=probe))
        self.stack.enter_context(patch('placekeeper.plugins.kindle.onboarding.download_kfx',side_effect=download))
        self.stack.enter_context(patch.object(Onboarder,'extract',extract))
    def tearDown(self):
        self.stack.close();self.temp.cleanup()
    def engine(self):
        return Onboarder(self.root,'unused-auth','unused-parser',self.source,self.catalog)

    def test_loan_discovery_checks_x4_before_kindle_and_reports_store_mapping_failure(self):
        store_title = 'Second Novel'
        missing_title = 'Third Novel'
        self.source.items += [
            {'id':'b'*28,'title':store_title,'author':'An Author'},
            {'id':'c'*28,'title':missing_title,'author':'An Author'},
        ]
        self.catalog.documents[DOC]['kind'] = 'PDOC'
        for asin, title in [('B000000001', store_title), ('B000000002', missing_title)]:
            self.catalog.documents[asin] = {
                'document':asin,'kind':'EBOK','title':title,'authors':['Author, An'],
            }
        order = []
        libby = Mock()
        libby.audiobook_loans.side_effect = lambda: order.append('libby') or [
            {'title':title,'firstCreatorName':author}
            for title, author in [(store_title,'An Author'), (TITLE,'Placekeeper'), (missing_title,'An Author')]
        ]
        def crosspoint(request):
            order.append('x4')
            self.assertEqual(request.method, 'GET')
            document = request.url.path.rsplit('/', 1)[1]
            if document == md5((missing_title + '.epub').encode()).hexdigest():
                return httpx.Response(404)
            return httpx.Response(200,json={'document':document,'progress':'saved'})
        def kindle(previous):
            order.append('kindle')
            return {'documents':self.catalog.documents,'pages':1}
        private_json(self.root/'crosspoint-auth.json',{'username':'test','auth_key':'test'})
        client = httpx.Client(transport=httpx.MockTransport(crosspoint))
        with patch('placekeeper.plugins.kindle.loan_discovery.Libby',return_value=libby), \
                patch('placekeeper.plugins.kindle.loan_discovery.httpx.Client',return_value=client), \
                patch.object(self.catalog,'pull',side_effect=kindle), \
                patch('placekeeper.plugins.kindle.onboarding.extract_chapters',side_effect=ValueError('Unsupported voucher')):
            report = self.engine().run_once(self.root)
        self.assertEqual(order,['libby','x4','x4','x4','kindle'])
        self.assertEqual(report['matches'],[
            {'title':store_title,'kind':'EBOK','status':'retry'},
            {'title':TITLE,'kind':'PDOC','status':'ready'},
        ])
        self.assertEqual(self.downloads,1)
        registry=json.loads((self.root/'ready-profiles.json').read_text())
        self.assertEqual([entry['document'] for entry in registry['profiles']],[DOC])

    def test_no_active_loans_still_builds_maps_for_two_reader_sync(self):
        engine=self.engine()
        with patch('placekeeper.plugins.kindle.loan_discovery.Libby') as libby:
            libby.return_value.audiobook_loans.return_value=[]
            report=engine.run_once(self.root)
        self.assertEqual(report['matches'],[])
        registry=json.loads((self.root/'ready-profiles.json').read_text())
        self.assertEqual([entry['document'] for entry in registry['profiles']],[DOC])

    def test_fresh_detection_to_usable_profile_then_restart_is_cached(self):
        engine=self.engine();first=engine.run_once()
        self.assertEqual(first['events'][0]['status'],'ready')
        registry=json.loads((self.root/'ready-profiles.json').read_text())
        profile,mapper,_=load_profile(registry['profiles'][0]['path'])
        self.assertEqual(profile['document'],DOC)
        self.assertFalse(profile['automatic_progress_writes_enabled'])
        self.assertFalse(profile['validation']['round_trip']['physical_handoff_verified'])
        self.assertTrue(profile['validation']['text_match']['normalized_text_identical'])
        second=self.engine().run_once()
        self.assertEqual(second['events'][0]['status'],'cached')
        self.assertEqual(self.downloads,1)
        self.assertEqual(self.source.download_count,1)

    def test_registry_load_reuses_validation_but_still_checks_file_hashes(self):
        self.engine().run_once()
        entry = json.loads((self.root/'ready-profiles.json').read_text())['profiles'][0]
        with patch('placekeeper.plugins.kindle.onboarding.validate_map') as validate:
            profile, _, _ = load_profile(entry['path'], expected_sha256=entry['sha256'])
        validate.assert_not_called()
        Path(profile['files']['content']['path']).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'content fingerprint differs'):
            load_profile(entry['path'], expected_sha256=entry['sha256'])

    def test_epub_arriving_later_is_automatically_retried(self):
        items=self.source.items;self.source.items=[]
        first=self.engine().run_once()
        self.assertEqual(first['events'][0]['status'],'waiting_for_epub')
        self.assertEqual(self.downloads,0)
        self.source.items=items
        self.assertEqual(self.engine().run_once()['events'][0]['status'],'ready')

    def test_unrelated_catalog_change_keeps_ready_map(self):
        self.engine().run_once()
        self.source.items.append({'id':'b'*28,'title':'Another Novel','author':'An Author'})
        with patch.object(Onboarder, 'onboard') as onboard:
            self.assertEqual(self.engine().run_once()['events'][0]['status'], 'cached')
        onboard.assert_not_called()
        self.assertEqual(self.source.download_count, 1)

    def test_store_denial_backoff_survives_unrelated_catalog_change(self):
        self.catalog.documents[DOC]['kind'] = 'EBOK'
        with patch('placekeeper.plugins.kindle.onboarding.extract_chapters',
                   side_effect=StoreDeliveryDenied('Forbidden')) as extract, \
                patch('placekeeper.plugins.kindle.onboarding.time.time') as clock:
            clock.return_value = 1000
            self.assertEqual(self.engine().run_once()['events'][0]['status'], 'delivery_unavailable')
            self.source.items.append({'id':'b'*28,'title':'Another Novel','author':'An Author'})
            clock.return_value += 60
            self.assertEqual(self.engine().run_once()['events'][0]['status'], 'delivery_unavailable')
            self.assertEqual(extract.call_count, 1)
            clock.return_value += 86400
            self.engine().run_once()
            self.assertEqual(extract.call_count, 2)

    def test_same_text_multiple_epubs_is_quarantined(self):
        self.source.items.append({'id':'b'*28,'title':TITLE,'author':'Placekeeper'})
        self.assertEqual(self.engine().run_once()['events'][0]['status'],'needs_review')
        self.assertEqual(json.loads((self.root/'ready-profiles.json').read_text())['profiles'],[])

    def test_near_identical_edition_is_not_automatically_activated(self):
        original=self.source.download
        def changed(book,directory):
            p=original(book,directory)
            with ZipFile(p) as z:entries={n:z.read(n) for n in z.namelist()}
            name='c1.xhtml'
            body=entries[name].index(b'<body')
            entries[name]=entries[name][:body]+entries[name][body:].replace(b'Checkpoint',b'CheckpointX',1)
            with ZipFile(p,'w') as z:
                for n,b in entries.items():z.writestr(n,b)
            return p
        with patch.object(self.source,'download',side_effect=changed):
            report=self.engine().run_once()
        self.assertEqual(report['events'][0]['status'],'edition_mismatch')
        self.assertEqual(json.loads((self.root/'ready-profiles.json').read_text())['profiles'],[])

    def test_changed_epub_cannot_reuse_ready_map(self):
        self.engine().run_once()
        epub=self.root/'epubs'/('a'*28+'.epub')
        with ZipFile(epub,'a') as z:
            z.writestr('test-extra.txt','changed file fingerprint')
        # Regeneration must occur instead of trusting the cached file hash.
        result=self.engine().run_once()
        self.assertEqual(result['events'][0]['status'],'ready')
        self.assertNotEqual(result['events'][0]['status'],'cached')

    def test_new_ambiguity_withdraws_old_profile_from_registry(self):
        self.engine().run_once()
        self.source.items.append({'id':'b'*28,'title':TITLE,'author':'Placekeeper'})
        self.assertEqual(self.engine().run_once()['events'][0]['status'],'needs_review')
        self.assertEqual(json.loads((self.root/'ready-profiles.json').read_text())['profiles'],[])

    def test_partial_discovery_failure_keeps_checkpoint_and_registry(self):
        self.engine().run_once()
        before=(self.root/'state.json').read_bytes()
        registry=(self.root/'ready-profiles.json').read_bytes()
        with patch.object(self.catalog,'pull',side_effect=ValueError('incomplete page')):
            with self.assertRaises(ValueError):self.engine().run_once()
        self.assertEqual((self.root/'state.json').read_bytes(),before)
        self.assertEqual((self.root/'ready-profiles.json').read_bytes(),registry)

    def test_removed_document_leaves_ready_registry(self):
        self.engine().run_once();self.catalog.documents={}
        self.engine().run_once()
        self.assertEqual(json.loads((self.root/'ready-profiles.json').read_text())['profiles'],[])


class PaginationTests(unittest.TestCase):
    def test_amazon_cursor_pages_and_removals(self):
        requests=[]
        def respond(request):
            requests.append(request)
            self.assertEqual(request.method,'GET')
            if len(requests)==1:
                self.assertNotIn('last_sync_time',request.url.params)
                return httpx.Response(200,text=f'<response><has_more>true</has_more><sync_time>one</sync_time><add_update_list><meta_data><ASIN>{DOC}</ASIN><title>{TITLE}</title><cde_contenttype>PDOC</cde_contenttype></meta_data></add_update_list></response>')
            self.assertEqual(request.url.params['last_sync_time'],'one')
            return httpx.Response(200,text='<response><has_more>false</has_more><sync_time>two</sync_time><add_update_list><meta_data><ASIN>B000000001</ASIN><title>Store Book</title><cde_contenttype>EBOK</cde_contenttype></meta_data></add_update_list><removal_list><meta_data><ASIN>OLD</ASIN></meta_data></removal_list></response>')
        with tempfile.TemporaryDirectory() as d:
            auth=Path(d)/'auth';auth.write_text('{}')
            with patch('placekeeper.plugins.kindle.catalog.Authenticator.from_dict',return_value=None):
                catalog=AmazonCatalog(auth,httpx.Client(transport=httpx.MockTransport(respond)))
            try:r=catalog.pull({'documents':{'OLD':{}},'cursor':'legacy'})
            finally:catalog.close()
        self.assertEqual(r['pages'],2);self.assertEqual(r['cursor'],'two')
        self.assertEqual(set(r['documents']),{DOC,'B000000001'})
        self.assertEqual(r['documents']['B000000001']['kind'],'EBOK')

    def test_nonadvancing_pagination_cannot_commit(self):
        with tempfile.TemporaryDirectory() as d:
            auth=Path(d)/'auth';auth.write_text('{}')
            with patch('placekeeper.plugins.kindle.catalog.Authenticator.from_dict',return_value=None):
                catalog=AmazonCatalog(auth,httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,text='<response><has_more>true</has_more><sync_time>same</sync_time></response>'))))
            previous={'cursor':'same','documents':{}}
            try:
                with self.assertRaisesRegex(ValueError,'did not advance'):catalog.pull(previous)
            finally:catalog.close()
            self.assertEqual(previous,{'cursor':'same','documents':{}})


if __name__=='__main__':unittest.main()
