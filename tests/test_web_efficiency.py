"""Browsing the status page must not import service clients or mapping code."""
import json
import subprocess
import sys
import tempfile
import unittest

from placekeeper.storage import ROOT


class WebImportTests(unittest.TestCase):
    def test_status_uses_only_lightweight_plugin_descriptors(self):
        with tempfile.TemporaryDirectory() as directory:
            code = '''
import json,sys
from placekeeper.web.app import Jobs
jobs=Jobs(sys.argv[1]);status=jobs.status()
assert len(status['services'])>=4
heavy=[name for name in sys.modules if name=='httpx' or name=='audible'
       or name=='placekeeper.mapping.text' or name.endswith('.registration')
       or (name.startswith('placekeeper.plugins.') and name.rsplit('.',1)[-1] in ('client','reader','catalog','legacy_catalog'))]
print(json.dumps(heavy))
'''
            output = subprocess.check_output([sys.executable, '-c', code, directory], cwd=ROOT, text=True)
            self.assertEqual(json.loads(output), [])

    def test_email_settings_do_not_load_smtp_or_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            code = """
import json,sys
from pathlib import Path
from placekeeper.plugins.kindle.email_delivery import settings,library_status
root=Path(sys.argv[1]);settings(root);library_status(root,[])
print(json.dumps([name for name in ('smtplib','ssl','placekeeper.mapping.text','httpx','audible') if name in sys.modules]))
"""
            output=subprocess.check_output([sys.executable,'-c',code,directory],cwd=ROOT,text=True)
            self.assertEqual(json.loads(output),[])
