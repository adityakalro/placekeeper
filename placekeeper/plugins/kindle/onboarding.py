"""Discover matching titles and verify text maps for Kindle personal documents.

No reading-progress writes. Uncertain matches are quarantined for review.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from zipfile import BadZipFile

import httpx

from placekeeper.plugins.kindle.catalog import AmazonCatalog
from placekeeper.plugins.kindle.delivery import download_kfx, probe
from placekeeper.plugins.kindle.loan_discovery import kindle_matches, matches as metadata_matches, x4_loans
from placekeeper.plugins.kindle.store_mapping import StoreMapper, extract_chapters
from placekeeper.plugins.readest.client import ReadestSource
from placekeeper.storage import ROOT, private_json, state_lock
from placekeeper.mapping.text import Mapper, epub_index, kfx_index

VERSION = 1


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def title_words(text):
    return set(re.findall(r'[^\W_]+',unicodedata.normalize('NFKC',text).casefold()))


def candidates(document, books):
    words = title_words(document['title'])
    ranked=[]
    for book in books:
        other=title_words(book['title'])
        if not words or not other:
            continue
        score=len(words&other)/min(len(words),len(other))
        if score >= 0.5:
            ranked.append((score,book))
    # All plausible candidates are checked; never silently discard an alternate
    # edition because its metadata ranked one place below a fixed shortlist.
    return [b for _,b in sorted(ranked,key=lambda x:(-x[0],x[1]['id']))]


def text_evidence(source, target, sample_count=120):
    if min(len(source.text),len(target.text)) < 200:
        return {'samples':0,'matched':0,'coverage':0.0,'adequate':False}
    # Distributed context tests, including start/end. Require unique matches so
    # repeated boilerplate cannot establish the identity of a different edition.
    indices=sorted(set(round(i*(len(source.text)-1)/(sample_count-1)) for i in range(sample_count)))
    matched=0
    for i in indices:
        try:
            source.unique_match(target,i)
            matched+=1
        except ValueError:
            pass
    coverage=matched/len(indices)
    ratio=min(len(source.text),len(target.text))/max(len(source.text),len(target.text))
    return {'samples':len(indices),'matched':matched,'coverage':coverage,
            'length_ratio':ratio,'adequate':coverage>=0.98 and ratio>=0.98}


def evaluate_indexes(e, k):
    forward=text_evidence(e,k);reverse=text_evidence(k,e)
    identical=e.text==k.text
    return {'epub_to_kfx':forward,'kfx_to_epub':reverse,
            'accepted':identical and forward['adequate'] and reverse['adequate'],
            'normalized_text_identical':identical}


def validate_map(mapper, count=40):
    passed=0
    for i in range(count):
        index=round((len(mapper.epub.coordinates)-1)*i/(count-1))
        xpath=mapper.epub.coordinates[index]
        try:
            forward=mapper.from_xpointer(xpath)
            reverse=mapper.from_sidecar(forward['suggested_forward_position'])
            # Buckets can straddle paragraphs, so validate the coordinate range,
            # not equality to an arbitrary text-node offset.
            again=mapper.from_xpointer(reverse['xpointer'])
            if again['kindle_location']==forward['kindle_location']:
                passed+=1
        except (ValueError,IndexError):
            pass
    return {'samples':count,'passed':passed,'accepted':passed/count>=0.95,
            'precision':'Kindle location buckets, not exact characters',
            'physical_handoff_verified':False}


class Onboarder:
    def __init__(self, root, amazon_auth, kfxlib, source, catalog):
        self.root=Path(root).resolve();self.auth=Path(amazon_auth).resolve()
        self.kfxlib=Path(kfxlib).resolve();self.source=source;self.catalog=catalog
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.state_path=self.root/'state.json'
        self.state=json.loads(self.state_path.read_text()) if self.state_path.exists() else {
            'version':VERSION,'amazon':{},'jobs':{}}
        if self.state.get('version') != VERSION:
            raise ValueError('Unsupported onboarding state version')

    def extract(self, doc, directory):
        content=directory/'map/content.json';locations=directory/'map/locations.json'
        book=directory/(doc+'.azw8')
        source=directory/'map/source.json'
        if content.exists() and locations.exists() and source.exists():
            if json.loads(source.read_text())['sha256']==file_hash(book):
                return content,locations
        # kfxlib is isolated in a subprocess; its conversion logs may contain
        # book text, so never copy them into public status/error output.
        try:
            result=subprocess.run([sys.executable,'-m','placekeeper.plugins.kindle.extract_kfx',str(book),
                str(directory/'map'),'--kfxlib-parent',str(self.kfxlib)],
                capture_output=True,timeout=120,cwd=ROOT)
        except subprocess.TimeoutExpired:
            raise ValueError('KFX extraction timed out') from None
        if result.returncode:
            raise ValueError('KFX extraction failed; unsupported content format')
        return content,locations

    def onboard(self, document, books, catalog_signature):
        doc=document['document'];directory=self.root/'books'/doc
        directory.mkdir(mode=0o700,parents=True,exist_ok=True)
        possible=candidates(document,books)
        if not possible:
            return {'status':'waiting_for_epub','catalog_signature':catalog_signature}
        book=directory/(doc+'.azw8')
        if not book.exists():
            response=probe(doc,self.auth,directory)
            if response['http_status'] != 200:
                raise ValueError('Amazon delivery manifest failed')
            manifest=json.loads((directory/(doc+'-manifest.json')).read_text())
            download_kfx(doc,manifest,directory)
        content,locations=self.extract(doc,directory)
        kindle_text = kfx_index(content)
        accepted=[];checks=[]
        for candidate in possible:
            epub=self.root/'epubs'/(candidate['id']+'.epub')
            if not epub.exists():
                epub=self.source.download(candidate,self.root/'epubs')
            epub_text = epub_index(epub)
            evidence=evaluate_indexes(epub_text, kindle_text)
            checks.append({'readest_id':candidate['id'],'evidence':evidence})
            if evidence['accepted']:
                accepted.append((candidate,epub,evidence,epub_text))
        private_json(directory/'match-evidence.json',checks)
        if len(accepted)!=1:
            return {'status':'needs_review' if accepted else 'edition_mismatch',
                    'candidate_count':len(possible),'accepted_count':len(accepted),
                    'catalog_signature':catalog_signature}
        candidate,epub,evidence,epub_text=accepted[0]
        filename=candidate['title']+'.epub'
        if any(c in filename for c in '<>:"/\\|?*') or any(ord(c)<32 for c in filename) or filename in ('.epub','..epub'):
            return {'status':'needs_review','reason':'unsupported_x4_filename','catalog_signature':catalog_signature}
        if sum(b['title']==candidate['title'] for b in books)>1:
            return {'status':'needs_review','reason':'x4_filename_collision','catalog_signature':catalog_signature}
        mapper=Mapper(epub,content,locations,epub_text=epub_text,kfx_text=kindle_text)
        validation=validate_map(mapper)
        if not validation['accepted']:
            return {'status':'needs_review','reason':'round_trip_check_failed','catalog_signature':catalog_signature}
        files={name:{'path':str(path.resolve()),'sha256':file_hash(path)} for name,path in (
            ('epub',epub),('content',content),('locations',locations))}
        profile={'schema_version':2,'document':doc,
            'crosspoint_document':hashlib.md5(filename.encode()).hexdigest(),
            'readest_id':candidate['id'],'x4_filename':filename,
            'crosspoint_identity_method':'filename MD5; same mode confirmed on the X4',
            'files':files,'validation':{'text_match':evidence,'round_trip':validation},
            'bridge':{'model':'legacy_sidecar_location_bucket_150','precision':'location bucket',
                      'basis':'Confirmed PDOC client behavior; no per-book physical test claimed'},
            'status':'ready_for_dry_run','automatic_progress_writes_enabled':False}
        profile_path=directory/'profile.json';private_json(profile_path,profile)
        return {'status':'ready','profile':str(profile_path),'readest_id':candidate['id'],
                'profile_sha256':file_hash(profile_path),
                'files':files,'catalog_signature':catalog_signature}

    def onboard_store(self, document, books, signature):
        possible = [book for book in books if metadata_matches(
            book['title'], [book.get('author')], document['title'], document['authors'])]
        if not possible:
            return {'status': 'waiting_for_epub', 'catalog_signature': signature}
        if len(possible) != 1:
            return {'status': 'needs_review', 'catalog_signature': signature}
        candidate = possible[0]
        filename = candidate['title'] + '.epub'
        if (any(c in filename for c in '<>:"/\\|?*') or any(ord(c) < 32 for c in filename)
                or sum(book['title'] == candidate['title'] for book in books) != 1):
            return {'status': 'needs_review', 'catalog_signature': signature}
        directory = self.root/'books'/document['document']
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        epub = self.root/'epubs'/(candidate['id']+'.epub')
        if not epub.exists():
            epub = self.source.download(candidate, self.root/'epubs')
        table = extract_chapters(document['document'], self.auth, self.kfxlib)
        StoreMapper(epub, table)
        table_path = directory/'chapters.json'
        private_json(table_path, table)
        files = {key: {'path': str(path.resolve()), 'sha256': file_hash(path)}
                 for key, path in [('epub', epub), ('chapters', table_path)]}
        profile = directory/'profile.json'
        private_json(profile, {
            'schema_version': 3, 'kind': 'EBOK', 'document': document['document'],
            'crosspoint_document': hashlib.md5(filename.encode()).hexdigest(),
            'readest_id': candidate['id'], 'x4_filename': filename, 'files': files,
        })
        return {'status': 'ready', 'profile': str(profile), 'readest_id': candidate['id'],
                'profile_sha256': file_hash(profile), 'files': files, 'catalog_signature': signature}

    def run_once(self, loan_root=None):
        with state_lock(self.state_path):
            if self.state_path.exists():
                self.state=json.loads(self.state_path.read_text())
            # Do not commit a partial Amazon cursor or an incomplete EPUB list.
            x4_books,books=x4_loans(loan_root,self.source) if loan_root is not None else ([],None)
            amazon=self.catalog.pull(self.state.get('amazon',{}))
            if books is None:
                books=self.source.books()
            documents=amazon['documents']
            selected=kindle_matches(x4_books,documents)
            signature=hashlib.sha256(json.dumps(sorted(books,key=lambda b:b['id']),sort_keys=True).encode()).hexdigest()
            self.state['amazon']=amazon
            events=[]
            ready=[]
            # Prioritize three-service matches and retain Kindle maps for two-service sync.
            ordered = dict.fromkeys([*selected, *documents])
            ordered = sorted(ordered, key=lambda doc: documents[doc].get('kind') == 'EBOK')
            mapped_editions = set()
            for doc in ordered:
                document=documents[doc]
                store_book=document.get('kind')=='EBOK'
                if store_book and not any(metadata_matches(book['title'], [book.get('author')],
                                                           document['title'], document['authors'])
                                          for book in books):
                    continue
                candidate=selected.get(doc)
                candidates_for_doc=books if candidate is None else [candidate]
                old=self.state['jobs'].get(doc,{})
                same_candidate=candidate is None or old.get('readest_id')==candidate['id']
                if old.get('status')=='ready' and old.get('catalog_signature')==signature and same_candidate:
                    damaged=[name for name,v in old.get('files',{}).items()
                             if not Path(v['path']).exists() or file_hash(v['path'])!=v['sha256']]
                    if not damaged and Path(old['profile']).exists() \
                            and file_hash(old['profile'])==old.get('profile_sha256'):
                        events.append({'document':doc,'status':'cached'})
                        if old.get('readest_id') not in mapped_editions:
                            ready.append(doc)
                            mapped_editions.add(old.get('readest_id'))
                        continue
                    # Only clear our own generated cache, never an arbitrary
                    # path supplied in edited state. Recover from corruption by
                    # re-fetching the EPUB or extracting the map again.
                    if 'epub' in damaged:
                        candidate_id=old.get('readest_id','')
                        if re.fullmatch(r'[a-f0-9]{28,32}',candidate_id):
                            (self.root/'epubs'/(candidate_id+'.epub')).unlink(missing_ok=True)
                    if {'content','locations'} & set(damaged):
                        for name in ('content.json','locations.json','source.json'):
                            (self.root/'books'/doc/'map'/name).unlink(missing_ok=True)
                if old.get('next_retry',0)>time.time() and old.get('catalog_signature')==signature:
                    events.append({'document':doc,'status':'retry_deferred'})
                    continue
                try:
                    job=(self.onboard_store(document,books,signature) if store_book
                         else self.onboard(document,candidates_for_doc,signature))
                except (ValueError,OSError,ET.ParseError,BadZipFile,KeyError,StopIteration,httpx.HTTPError) as error:
                    # Status contains the error category, not remote URLs or
                    # payloads that might carry secrets or book prose.
                    job={'status':'retry','error_type':type(error).__name__,
                         'next_retry':time.time()+300,'catalog_signature':signature}
                self.state['jobs'][doc]=job
                events.append({'document':doc,'status':job['status']})
                if job['status']=='ready' and job.get('readest_id') not in mapped_editions:
                    ready.append(doc)
                    mapped_editions.add(job.get('readest_id'))
                private_json(self.state_path,self.state)
            for doc in set(self.state['jobs'])-set(documents):
                self.state['jobs'][doc]['status']='removed'
            private_json(self.state_path,self.state)
            profiles=[{'document':doc,'path':self.state['jobs'][doc]['profile'],
                       'sha256':self.state['jobs'][doc]['profile_sha256']} for doc in ready]
            private_json(self.root/'ready-profiles.json',{'schema_version':1,'profiles':profiles,
                                            'automatic_progress_writes_enabled':False})
            report={'mode':'onboarding_only','progress_writes':0,'events':events,
                    'amazon_pages':amazon['pages'],
                    'readest_books':len(books)}
            if loan_root is not None:
                statuses={event['document']:event['status'] for event in events}
                report['matches']=[
                    {'title':book['title'],'kind':documents[doc].get('kind','PDOC'),
                     'status':'ready' if doc in ready else statuses.get(doc,'mapping_required')}
                    for doc,book in selected.items()
                ]
            private_json(self.root/'last-run.json',report)
            return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=ROOT/'.private/onboarding')
    p.add_argument('--amazon-auth',type=Path,default=ROOT/'.private/amazon-kindle-download-device.json')
    p.add_argument('--readest-auth',type=Path,default=ROOT/'.private/readest-device.json')
    p.add_argument('--kfxlib-parent',type=Path,default=ROOT/'.private/vendor/python')
    p.add_argument('--loan-root',type=Path,help='Discover from active Libby loans and saved X4 positions')
    p.add_argument('--watch',action='store_true')
    p.add_argument('--interval',type=int,default=300)
    args=p.parse_args()
    if args.interval<60:p.error('interval must be at least 60 seconds')
    os.umask(0o077)
    source=ReadestSource(args.readest_auth);catalog=AmazonCatalog(args.amazon_auth)
    try:
        engine=Onboarder(args.root,args.amazon_auth,args.kfxlib_parent,source,catalog)
        while True:
            try:
                report=engine.run_once(args.loan_root)
                print(json.dumps({'status':'onboarding_complete','mode':report['mode'],
                    'counts':dict(Counter(e['status'] for e in report['events'])),
                    'amazon_pages':report['amazon_pages'],'readest_books':report['readest_books'],
                    'progress_writes':0}),flush=True)
            except (ValueError,httpx.HTTPError,ET.ParseError):
                print(json.dumps({'status':'discovery_failed','checkpoint_preserved':True}),flush=True)
                if not args.watch:return 1
            if not args.watch:return 0
            time.sleep(args.interval)
    finally:
        source.close();catalog.close()


if __name__=='__main__':
    try:raise SystemExit(main())
    except KeyboardInterrupt:raise SystemExit(130)
