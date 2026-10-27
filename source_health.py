"""Bounded, certificate-verified check before installing the cloud browser.

Only the official anonymous listing is requested. No proxy, DNS or TLS setting
is changed. Failed checks preserve the last dataset and collection checkpoint.
"""
import argparse
import datetime as dt
import json
from pathlib import Path
import socket
import ssl
import urllib.error
import urllib.request

from tenderbot_crawler.gem import (
    LISTING_URL, MAX_METADATA_BYTES, PortalChallengeError, PortalError,
    _challenge_reason, _csrf, safe_gem_url,
)
from tenderbot_crawler.storage import atomic_json


class OfficialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        safe_gem_url(newurl)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def check_source(opener=None):
    opener = opener or urllib.request.build_opener(OfficialRedirect())
    try:
        with opener.open(LISTING_URL, timeout=25) as response:
            safe_gem_url(response.url)
            body = response.read(MAX_METADATA_BYTES + 1)
            if len(body) > MAX_METADATA_BYTES:
                raise PortalError('Public listing exceeds the metadata limit')
            reason = _challenge_reason(body, response.headers.get('Content-Type', ''), response.url)
            if reason:
                raise PortalChallengeError(reason)
            if not _csrf(body.decode('utf-8', errors='replace')):
                raise PortalError('Anonymous public GeM form is missing')
            return {'status': 'reachable', 'stage': 'http', 'http_status': response.status}
    except urllib.error.HTTPError as error:
        return {'status': 'blocked' if error.code in (401, 403, 407) else 'unavailable',
                'stage': 'http', 'http_status': error.code, 'error': 'GeM returned HTTP ' + str(error.code)}
    except PortalChallengeError as error:
        return {'status': 'blocked', 'stage': 'http', 'error': str(error)}
    except PortalError as error:
        return {'status': 'unavailable', 'stage': 'http', 'error': str(error)}
    except (urllib.error.URLError, OSError) as error:
        reason = getattr(error, 'reason', error)
        if isinstance(reason, socket.gaierror):
            stage = 'dns'
        elif isinstance(reason, ssl.SSLError):
            stage = 'tls'
        else:
            stage = 'connect'
        return {'status': 'unavailable', 'stage': stage, 'error': type(reason).__name__,
                'errno': getattr(reason, 'errno', None)}


def record_failure(folder, result):
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    state_path, status_path = folder / 'state.json', folder / 'collector.json'
    # Require the existing data branch; never replace a missing checkout with
    # an empty dataset or reset a resumable full-feed checkpoint.
    state = json.loads(state_path.read_text(encoding='utf-8'))
    status = json.loads(status_path.read_text(encoding='utf-8'))
    update = {'status': 'blocked' if result['status'] == 'blocked' else 'failed',
              'last_run_at': stamp, 'source_checked_at': stamp,
              'error': 'GeMSourceUnavailable',
              'error_message': 'Public GeM source unavailable at ' + result['stage'] + ' stage',
              'source_health': result}
    atomic_json(state_path, {**state, **update})
    atomic_json(status_path, {**status, **update, 'errors': 1})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path)
    args = parser.parse_args()
    try:
        addresses = sorted({item[4][0] for item in socket.getaddrinfo('bidplus.gem.gov.in', 443, type=socket.SOCK_STREAM)})
    except OSError:
        addresses = []
    # Log public destination addresses and proxy protocol names, never values.
    print(json.dumps({'source_addresses': addresses,
                      'proxy_protocols': sorted(urllib.request.getproxies())}), flush=True)
    result = check_source()
    print(json.dumps({'source_health': result}), flush=True)
    # A plain HTTP check may receive a server error even when a browser can
    # load the form. Only transport failures or explicit access restrictions
    # stop browser setup; the collector still validates the actual session.
    if not may_attempt_browser(result):
        if args.data_dir:
            record_failure(args.data_dir, result)
        return 1
    return 0


def may_attempt_browser(result):
    return result['stage'] == 'http' and result['status'] != 'blocked'


if __name__ == '__main__':
    raise SystemExit(main())
