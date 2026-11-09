"""Register a separate Kindle download test device only after user approval.

Uses the existing approved Amazon session, retaining the original device file.
No passwords, browser cookies, interception certificates, or Kindle app needed.
"""
import argparse
import html
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import re
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from audible import Authenticator
from audible.login import create_code_verifier, create_s256_code_challenge

from placekeeper.storage import default_data_root, private_json


DESTINATION = default_data_root()/'amazon-kindle-download-device.json'
PLAN = default_data_root()/'kindle-download-device-plan.json'


def plan():
    return {'device_name': 'Placekeeper', 'device_type': 'AGZZ1ST63LKBW',
            'app_name': 'Kindle for iOS', 'device_serial': secrets.token_hex(16).upper(),
            'destination': str(DESTINATION),
            'registration_endpoint': 'https://api.amazon.com/auth/register',
            'purpose': 'Test direct downloads of the user-owned Send to Kindle documents',
            'source_reference': 'https://github.com/mkb79/kindle/blob/main/src/kindle/register.py'}


def register(prepared, access_token=None, authorization=None, destination=None, data_root=None):
    destination = Path(destination) if destination is not None else DESTINATION
    data_root = Path(data_root) if data_root is not None else default_data_root()
    if (prepared['registration_endpoint'] != 'https://api.amazon.com/auth/register'
            or prepared['device_type'] != 'AGZZ1ST63LKBW'
            or prepared['device_name'] not in ('Placekeeper', 'Book Sync Pi Download Test')
            or prepared['app_name'] != 'Kindle for iOS'
            or not re.fullmatch(r'[A-F0-9]{32}', prepared['device_serial'])):
        raise ValueError('Plan differs from the reviewed Kindle download test device')
    if destination.exists():
        raise ValueError('Download-device credentials already exist; refusing another registration')
    if authorization is None and access_token is None:
        current = json.loads((data_root/'amazon-device.json').read_text())
        auth = Authenticator.from_dict(current)
        if float(current['expires']) <= time.time()+60:
            try:
                auth.refresh_access_token()
            except Exception:
                raise ValueError('Existing Amazon session could not be refreshed; sign-in is needed') from None
        access_token = auth.access_token
    registration = {'domain': 'Device', 'app_version': '6.38.0.100',
                    'device_type': prepared['device_type'], 'device_name': prepared['device_name'],
                    'os_version': '14.4', 'device_serial': prepared['device_serial'],
                    'device_model': 'iPhone', 'app_name': prepared['app_name'],
                    'software_version': '1184366692'}
    response = httpx.post(prepared['registration_endpoint'], timeout=30,
                         json={'requested_token_type': ['bearer', 'mac_dms'],
                               'registration_data': registration,
                               'auth_data': authorization if authorization is not None else {'access_token': access_token},
                               'requested_extensions': ['device_info', 'customer_info']})
    if response.status_code != 200:
        try:
            failure = response.json()
        except ValueError:
            failure = {'response_format': 'non-JSON', 'bytes': len(response.content)}
        private_json(data_root/'kindle-download-registration-failure.json',
                     {'http_status': response.status_code, 'response': failure})
        raise ValueError(f'Device registration failed: HTTP {response.status_code}')
    data = response.json().get('response', {}).get('success', {})
    mac = data.get('tokens', {}).get('mac_dms', {})
    bearer = data.get('tokens', {}).get('bearer', {})
    if not mac.get('adp_token') or not mac.get('device_private_key'):
        raise ValueError('Registration returned no signed-device credentials; inspect the account before retrying')
    credentials = {'adp_token': mac['adp_token'], 'device_private_key': mac['device_private_key'],
                   'access_token': bearer.get('access_token'), 'refresh_token': bearer.get('refresh_token'),
                   'expires': time.time()+int(bearer.get('expires_in', 0)), 'locale_code': 'us',
                   'device_info': data.get('extensions', {}).get('device_info', {})}
    private_json(destination, credentials)
    return {'registered': True, 'device_name': prepared['device_name'], 'credentials_saved_privately': True}


def login_details(prepared):
    client_id = (prepared['device_serial']+'#'+prepared['device_type']).encode().hex()
    verifier = create_code_verifier()
    challenge = create_s256_code_challenge(verifier)
    params = {'openid.return_to': 'https://www.amazon.com/ap/maplanding',
              'openid.assoc_handle': 'amzn_kindle_for_ios_us', 'pageId': 'amzn_kindle_for_ios_branded',
              'accountStatusPolicy': 'P1', 'openid.mode': 'checkid_setup',
              'openid.identity': 'http://specs.openid.net/auth/2.0/identifier_select',
              'openid.claimed_id': 'http://specs.openid.net/auth/2.0/identifier_select',
              'openid.ns.oa2': 'http://www.amazon.com/ap/ext/oauth/2',
              'openid.oa2.client_id': 'device:'+client_id,
              'openid.ns.pape': 'http://specs.openid.net/extensions/pape/1.0',
              'openid.oa2.response_type': 'code', 'openid.ns': 'http://specs.openid.net/auth/2.0',
              'openid.oa2.code_challenge_method': 'S256', 'openid.oa2.code_challenge': challenge,
              'openid.pape.max_auth_age': '300', 'openid.oa2.scope': 'device_auth_access'}
    return 'https://www.amazon.com/ap/signin?'+urlencode(params), client_id, verifier.decode()


def serve_login(prepared):
    origin = 'http://127.0.0.1:8768'
    login_url, client_id, verifier_text = login_details(prepared)
    csrf = secrets.token_urlsafe(32)
    completed = False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, status, body):
            data = body.encode()
            self.send_response(status)
            for k,v in {'Content-Type':'text/html; charset=utf-8', 'Cache-Control':'no-store',
                        'Referrer-Policy':'same-origin', 'Content-Length':str(len(data))}.items():
                self.send_header(k,v)
            self.end_headers();self.wfile.write(data)

        def do_GET(self):
            if self.path != '/' or self.headers.get('Host') != '127.0.0.1:8768':
                self.respond(404,'Unknown page');return
            if completed:
                self.respond(200,'<h1>Kindle download test device registered</h1>');return
            self.respond(200,f'''<!doctype html><meta charset="utf-8"><title>Kindle download device sign-in</title>
                <h1>Placekeeper</h1><p>Amazon requires a fresh sign-in to register this approved test device.</p>
                <p><a href="{html.escape(login_url,quote=True)}" target="_blank" rel="noreferrer">Sign in with Amazon</a></p>
                <form method="post" action="/complete"><input type="hidden" name="csrf" value="{csrf}">
                <p><label>Amazon completion URL <input name="callback" type="password" required autocomplete="off" size="70"></label></p>
                <button>Register approved Kindle test device</button></form>''')

        def do_POST(self):
            nonlocal completed
            if (completed or self.path != '/complete' or self.headers.get('Host') != '127.0.0.1:8768'
                    or self.headers.get('Origin') != origin):
                self.respond(403,'Invalid session or origin');return
            try:
                size = int(self.headers.get('Content-Length','0'))
                if not 0 < size <= 20000:
                    raise ValueError()
                fields = parse_qs(self.rfile.read(size).decode())
            except (ValueError,UnicodeError):
                self.respond(400,'Invalid form');return
            if not secrets.compare_digest(fields.get('csrf',[''])[0],csrf):
                self.respond(403,'Invalid session');return
            callback = urlsplit(fields.get('callback',[''])[0])
            if callback.scheme != 'https' or callback.hostname != 'www.amazon.com' or callback.path != '/ap/maplanding':
                self.respond(400,'Expected the Amazon completion page URL');return
            token = parse_qs(callback.query).get('openid.oa2.access_token',[''])[0]
            code = parse_qs(callback.query).get('openid.oa2.authorization_code',[''])[0]
            if not token and not code:
                self.respond(400,'Completion URL has no authorization code or sign-in token');return
            try:
                authorization = {'client_id':client_id, 'authorization_code':code,
                                 'code_verifier':verifier_text, 'code_algorithm':'SHA-256',
                                 'client_domain':'DeviceLegacy'} if code else None
                result = register(prepared, access_token=token or None, authorization=authorization)
            except (ValueError,httpx.HTTPError):
                self.respond(400,'Registration failed. Private diagnostics were saved; do not resubmit yet.');return
            completed = True
            self.respond(200,'<h1>Kindle download test device registered</h1><p>Credentials saved privately. This page stays available for five minutes.</p>')
            print(json.dumps(result),flush=True)

    server = HTTPServer(('127.0.0.1',8768),Handler);server.timeout=1
    print(json.dumps({'status':'awaiting_fresh_amazon_signin','url':origin}),flush=True)
    deadline = None
    try:
        while deadline is None or time.monotonic()<deadline:
            server.handle_request()
            if completed and deadline is None:
                deadline=time.monotonic()+300
    finally:
        server.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--register', action='store_true', help='Perform the registration; requires explicit user approval')
    parser.add_argument('--login', action='store_true', help='Fresh Amazon sign-in for the approved registration')
    args = parser.parse_args()
    if not PLAN.exists():
        private_json(PLAN, plan())
    prepared = json.loads(PLAN.read_text())
    if args.login:
        serve_login(prepared)
    elif args.register:
        print(json.dumps(register(prepared)))
    else:
        print(json.dumps({k:v for k,v in prepared.items() if k != 'device_serial'}, indent=2))
