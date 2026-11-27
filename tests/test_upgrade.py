"""Disposable-checkout upgrades: no network, package manager or real services."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from deploy.upgrade import data_directory, upgrade


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.checkout=self.base/'checkout';self.checkout.mkdir()
        self.root=self.base/'data';self.root.mkdir(mode=0o700)
        (self.root/'web-config.json').write_text(json.dumps({'bind':'127.0.0.1','port':1,'hosts':['localhost:1']}))
        for name in ('amazon-device.json','readest-device.json','crosspoint-auth.json','libby-device.json',
                     'web-schedule.json','book-status.json','kindle-email-deliveries.json'):
            path=self.root/name;path.write_text(json.dumps({'saved':name}));path.chmod(0o600)
        self.original={p.name:p.read_bytes() for p in self.root.iterdir()}
        (self.checkout/'.gitignore').write_text('.venv/\n.private\n')
        (self.checkout/'version').write_text('old')
        (self.checkout/'install.sh').write_text('# old installer\n')
        self.git('init','-q');self.git('add','.');self.git('commit','-qm','old')
        self.old=self.git('rev-parse','HEAD').strip()
        (self.checkout/'install.sh').write_text('# PLACEKEEPER_SKIP_SYSTEM_PACKAGES\n')
        (self.checkout/'version').write_text('new');self.git('add','.');self.git('commit','-qm','new')
        self.target=self.git('rev-parse','HEAD').strip();self.git('reset','--hard',self.old)
        (self.checkout/'.venv').mkdir();(self.checkout/'.venv/old-marker').write_text('old interpreter')
        self.args=argparse.Namespace(checkout=str(self.checkout),data_root=str(self.root),ref=self.target,no_fetch=True,
            service=None,user_service=False,termux_service=None,backup_dir=None,wait=0)
    def tearDown(self):self.temp.cleanup()
    def git(self,*args):
        return subprocess.check_output(['git','-c','user.name=Upgrade Test','-c','user.email=test@example.invalid',*args],cwd=self.checkout,text=True,stderr=subprocess.DEVNULL)
    def install(self,fail=False):
        from deploy.upgrade import run as actual
        def fake(*args,**kwargs):
            if args[:2]==('bash','install.sh'):
                self.assertEqual(kwargs['env']['PLACEKEEPER_SKIP_SYSTEM_PACKAGES'],'1')
                self.assertEqual(kwargs['env']['PLACEKEEPER_DATA_DIR'],str(self.root.resolve()))
                (self.checkout/'.venv/bin').mkdir(parents=True)
                (self.checkout/'.venv/bin/python').write_text('new interpreter')
                (self.root/'installer-created').write_text('temporary')
                if fail:raise subprocess.CalledProcessError(1,args)
                return ''
            if str(args[0]).endswith('/.venv/bin/python'):return ''
            return actual(*args,**kwargs)
        return patch('deploy.upgrade.run',side_effect=fake)
    def test_fast_forward_preserves_data_and_saves_private_backup(self):
        with self.install():backup=upgrade(self.args)
        self.assertEqual(self.git('rev-parse','HEAD').strip(),self.target)
        for name,value in self.original.items():self.assertEqual((self.root/name).read_bytes(),value)
        self.assertEqual((backup/'venv/old-marker').read_text(),'old interpreter')
        self.assertEqual(backup.stat().st_mode & 0o777,0o700)
        self.assertEqual((backup/'upgrade.json').stat().st_mode & 0o777,0o600)
        self.assertEqual(json.loads((backup/'upgrade.json').read_text())['status'],'complete')
        self.assertEqual((backup/'data/amazon-device.json').stat().st_mode & 0o777,0o600)
    def test_installer_failure_restores_code_environment_and_all_data(self):
        with self.install(fail=True),self.assertRaises(subprocess.CalledProcessError):upgrade(self.args)
        self.assertEqual(self.git('rev-parse','HEAD').strip(),self.old)
        self.assertEqual((self.checkout/'.venv/old-marker').read_text(),'old interpreter')
        self.assertFalse((self.root/'installer-created').exists())
        for name,value in self.original.items():self.assertEqual((self.root/name).read_bytes(),value)
    def test_dirty_checkout_rejected_before_changes(self):
        (self.checkout/'version').write_text('my edits')
        with self.install(),self.assertRaisesRegex(ValueError,'local changes'):upgrade(self.args)
        self.assertEqual((self.checkout/'version').read_text(),'my edits')
        self.assertTrue((self.checkout/'.venv/old-marker').exists())
    def test_backup_inside_data_rejected(self):
        self.args.backup_dir=str(self.root/'backups')
        with self.assertRaisesRegex(ValueError,'outside'):upgrade(self.args)
    def test_active_service_waits_then_restarts_with_same_configuration(self):
        self.args.service='placekeeper-web.service'
        with self.install() as commands:
            # Wrap the install stub for service metadata/control calls.
            fake=commands.side_effect
            def managed(*args,**kwargs):
                if args[0]=='systemctl':return 'loaded\n' if 'LoadState' in args else ''
                return fake(*args,**kwargs)
            commands.side_effect=managed
            with patch('deploy.upgrade.Service.active',return_value=True),patch('deploy.upgrade.status',return_value={'running':False}),patch('deploy.upgrade.subprocess.run',wraps=subprocess.run) as command:
                # No legacy units are active; do not call the host's systemd.
                original=command._mock_wraps
                command.side_effect=lambda args,**kw:subprocess.CompletedProcess(args,3) if args[0]=='systemctl' else original(args,**kw)
                upgrade(self.args)
            calls=[call.args for call in commands.call_args_list]
            self.assertIn(('systemctl','stop','placekeeper-web.service'),calls)
            self.assertIn(('systemctl','start','placekeeper-web.service'),calls)
    def test_legacy_private_directory_wins_over_new_default(self):
        (self.checkout/'.private').symlink_to(self.root,target_is_directory=True)
        with patch.dict(os.environ,{},clear=True):self.assertEqual(data_directory(self.checkout),self.root.resolve())

    def test_concurrent_upgrade_is_rejected(self):
        from deploy.upgrade import upgrade_lock
        with upgrade_lock(self.checkout),self.assertRaisesRegex(ValueError,'already running'):
            upgrade(self.args)

    def test_readiness_failure_restores_old_installation(self):
        # Failure in the new code's offline startup probe must also roll back.
        with self.install() as commands:
            fake=commands.side_effect
            def fail_probe(*args,**kwargs):
                if str(args[0]).endswith('/.venv/bin/python'):raise subprocess.CalledProcessError(1,args)
                return fake(*args,**kwargs)
            commands.side_effect=fail_probe
            with self.assertRaises(subprocess.CalledProcessError):upgrade(self.args)
        self.assertEqual(self.git('rev-parse','HEAD').strip(),self.old)
        self.assertTrue((self.checkout/'.venv/old-marker').exists())

    def test_busy_service_is_not_stopped(self):
        self.args.service='placekeeper-web.service'
        with self.install() as commands:
            fake=commands.side_effect
            commands.side_effect=lambda *a,**kw:'loaded' if a[0]=='systemctl' else fake(*a,**kw)
            original=subprocess.run
            with patch('deploy.upgrade.Service.active',return_value=True),patch('deploy.upgrade.status',return_value={'running':False,'busy':True}),patch('deploy.upgrade.subprocess.run',side_effect=lambda a,**kw:subprocess.CompletedProcess(a,3) if a[0]=='systemctl' else original(a,**kw)):
                with self.assertRaisesRegex(ValueError,'still active'):upgrade(self.args)
            self.assertNotIn(('systemctl','stop','placekeeper-web.service'),[call.args for call in commands.call_args_list])
        self.assertEqual(self.git('rev-parse','HEAD').strip(),self.old)

    def test_termux_service_uses_existing_supervisor(self):
        self.args.termux_service='placekeeper'
        with self.install() as commands:
            fake=commands.side_effect
            commands.side_effect=lambda *a,**kw:'' if a[0]=='sv' else fake(*a,**kw)
            with patch('deploy.upgrade.Service.active',return_value=True),patch('deploy.upgrade.status',return_value={'running':False}):upgrade(self.args)
            calls=[call.args for call in commands.call_args_list]
            self.assertIn(('sv','down','placekeeper'),calls)
            self.assertIn(('sv','up','placekeeper'),calls)

    def test_target_without_safe_installer_is_rejected(self):
        self.args.ref=self.old
        with self.assertRaisesRegex(ValueError,'safe upgrade installer'):upgrade(self.args)
        self.assertTrue((self.checkout/'.venv/old-marker').exists())
