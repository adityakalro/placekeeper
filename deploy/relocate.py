"""Relocate saved Placekeeper paths after an authorized host migration.

Run on the destination before its watcher starts. Does not contact services.
"""
import argparse
import json
from pathlib import Path

from placekeeper.storage import file_sha256, private_json


def relocate(data_root, previous_root):
    data_root=Path(data_root).resolve()
    previous_root=str(previous_root).rstrip('/')
    def path(value):
        if value.startswith(previous_root+'/'):
            return str(data_root/value[len(previous_root)+1:])
        if value.startswith('.private/'):
            return str(data_root/value[len('.private/'):])
        return value
    profiles=list((data_root/'onboarding/books').glob('*/profile.json'))
    for manual in data_root.glob('*profile.json'):
        candidate=json.loads(manual.read_text())
        files=set(candidate.get('files',{}))
        if {'epub','content','locations'}<=files or {'epub','chapters'}<=files:
            profiles.append(manual)
    changes=[]
    for p in profiles:
        profile=json.loads(p.read_text())
        for record in profile['files'].values():
            record['path']=path(record['path'])
            if file_sha256(record['path'])!=record['sha256']:
                raise ValueError('Transferred book/map fingerprint mismatch')
        changes.append((p,profile))
    reader_path=data_root/'reader-books.json'
    if reader_path.exists():
        readers=json.loads(reader_path.read_text())
        for book in readers['books']:
            book['epub']=path(book['epub'])
            if file_sha256(book['epub'])!=book['sha256']:
                raise ValueError('Transferred reader EPUB fingerprint mismatch')
        changes.append((reader_path,readers))
    # Validate every input before publishing any rewritten paths.
    for target,data in changes:
        private_json(target,data)
    state_path=data_root/'onboarding/state.json'
    if state_path.exists():
        state=json.loads(state_path.read_text())
        registry_path = data_root/'onboarding/ready-profiles.json'
        registry = json.loads(registry_path.read_text()) if registry_path.exists() else {
            'schema_version': 1, 'automatic_progress_writes_enabled': False}
        ready = {}
        for doc,job in state['jobs'].items():
            if 'profile' in job:job['profile']=path(job['profile'])
            for record in job.get('files',{}).values():record['path']=path(record['path'])
            if job.get('status')=='ready':
                job['profile_sha256']=file_sha256(job['profile'])
                ready[doc] = {'document':doc,'path':job['profile'],'sha256':job['profile_sha256']}
        membership = [entry['document'] for entry in registry['profiles']] if 'profiles' in registry else list(ready)
        registry['profiles'] = [ready[doc] for doc in membership if doc in ready]
        private_json(state_path,state)
        private_json(registry_path,registry)
    return {'profiles_relocated':len(profiles),'data_root':str(data_root)}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-root',required=True,type=Path)
    p.add_argument('--previous-root',required=True)
    args=p.parse_args()
    print(json.dumps(relocate(args.data_root,args.previous_root)))
