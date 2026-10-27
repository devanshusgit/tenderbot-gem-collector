"""Paced stdlib client for TenderBot's owner-private Cloudflare source API.

The Site service credential is sent only to the configured HTTPS Site origin.
Redirects are rejected; official document URLs are mapped to fixed API routes.
This client never opens a direct GeM connection or logs response/secret values.
"""
import asyncio
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlsplit

from tenderbot_crawler.gem import PortalChallengeError, PortalError
from tenderbot_crawler.storage import timestamp, validate_record


MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_PDF_BYTES = 25 * 1024 * 1024
DOCUMENT_PATH = re.compile(r'/(showbidDocument|showradocumentPdf|showdirectradocumentPdf|list-ra-schedules)/(\d{1,14})')
SORTS = ('Bid-End-Date-Oldest','Bid-Start-Date-Latest')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        # Returning None makes urllib raise HTTPError before forwarding headers.
        return None


def configured():
    return bool(os.environ.get('GEM_CLOUDFLARE_SOURCE_URL') or os.environ.get('TENDERBOT_SITE_SERVICE_TOKEN'))


def configuration(base_url, token):
    try:
        if not isinstance(base_url,str) or '\\' in base_url or any(ord(c) <= 32 or ord(c) == 127 for c in base_url):
            raise ValueError
        parsed = urlsplit(base_url)
        host = parsed.hostname or ''
        if (parsed.scheme != 'https' or parsed.username is not None or parsed.password is not None or
                parsed.port not in (None,443) or not host.endswith('.chatgpt.site') or
                not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',part) for part in host.split('.')) or
                parsed.path != '/api/cloud-source' or parsed.query or parsed.fragment):
            raise ValueError
        if not isinstance(token,str) or not token or len(token) > 8192 or any(ord(c) <= 32 or ord(c) == 127 for c in token):
            raise ValueError
    except (ValueError,TypeError):
        raise PortalError('Cloud source requires its exact HTTPS Site API URL and a valid service credential.') from None
    return base_url


def document_route(url):
    try:
        if not isinstance(url,str) or '\\' in url or any(ord(c) <= 32 or ord(c) == 127 for c in url):
            raise ValueError
        parsed = urlsplit(url)
        match = DOCUMENT_PATH.fullmatch(parsed.path)
        if (parsed.scheme != 'https' or parsed.hostname != 'bidplus.gem.gov.in' or
                parsed.username is not None or parsed.password is not None or parsed.port not in (None,443) or
                parsed.query or parsed.fragment or not match):
            raise ValueError
    except (ValueError,TypeError):
        raise PortalError('Only an original official GeM bid document URL is permitted.') from None
    return '/documents/gem-'+match[2], {'kind':match[1]}


def _challenge(body):
    try:
        result = json.loads(body)
    except (ValueError,UnicodeError):
        return False
    return isinstance(result,dict) and (result.get('error_type') == 'access_restricted' or
        result.get('challenge') is True or result.get('blocked') is True)


class RemotePortal:
    def __init__(self, base_url=None, token=None, delay=1, timeout=60, opener=None):
        base_url = os.environ.get('GEM_CLOUDFLARE_SOURCE_URL','') if base_url is None else base_url
        token = os.environ.get('TENDERBOT_SITE_SERVICE_TOKEN','') if token is None else token
        self._base_url = configuration(base_url,token)
        self._token = token
        if isinstance(delay,bool) or not isinstance(delay,(float,int)) or not math.isfinite(delay) or delay < 1:
            raise ValueError('Cloud source pacing must be at least one second')
        if isinstance(timeout,bool) or not isinstance(timeout,(float,int)) or not math.isfinite(timeout) or not 1 <= timeout <= 90:
            raise ValueError('Cloud source timeout must be between one and90seconds')
        self._delay,self._timeout = float(delay),float(timeout)
        self._opener = opener or urllib.request.build_opener(NoRedirect())
        self._lock = asyncio.Lock()
        self._last_request = None
        self._open = False

    async def __aenter__(self):
        if self._open:
            raise PortalError('This cloud source session is already open.')
        self._open = True
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        await self.close()
        return False

    async def close(self):
        self._open = False

    def _fetch(self, path, query, max_bytes):
        url = self._base_url+path+('?' + urlencode(query) if query else '')
        request = urllib.request.Request(url,method='GET',headers={
            'OAI-Sites-Authorization':'Bearer '+self._token,
            'Accept':'application/json' if max_bytes == MAX_METADATA_BYTES else 'application/pdf',
        })
        try:
            response = self._opener.open(request,timeout=self._timeout)
        except urllib.error.HTTPError as error:
            response = error
        except (urllib.error.URLError,OSError):
            return None,{},b''
        with response:
            status = response.status
            # A redirect must never result in a second credential-bearing call.
            if 300 <= status < 400:
                raise PortalError('Cloud source redirect rejected; service credential was not forwarded.')
            if getattr(response,'url',url) != url:
                raise PortalError('Cloud source response left its requested API URL.')
            headers = {key.lower():value for key,value in response.headers.items()}
            if status in (401,403):
                return status,headers,b''
            limit = max_bytes if status == 200 else MAX_METADATA_BYTES
            length = headers.get('content-length','')
            if length.isdigit() and int(length) > limit:
                raise PortalError('Cloud source response exceeds its byte limit.')
            body = response.read(limit+1)
            if len(body) > limit:
                raise PortalError('Cloud source response exceeds its byte limit.')
            return status,headers,body

    async def _request(self, path, query=None, max_bytes=MAX_METADATA_BYTES):
        if not self._open:
            raise PortalError('Use the cloud source inside an async context.')
        async with self._lock:
            for attempt in range(3):
                wait = 0 if self._last_request is None else self._delay-(time.monotonic()-self._last_request)
                if attempt:
                    wait = max(wait,float(2**(attempt-1)))
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_request = time.monotonic()
                try:
                    status,headers,body = await asyncio.to_thread(self._fetch,path,query or {},max_bytes)
                except (urllib.error.URLError,OSError):
                    # A partial response/read failure has the same bounded
                    # retry policy as a failed connection. Do not log it raw.
                    status,headers,body = None,{},b''
                if status in (401,403) or _challenge(body):
                    raise PortalChallengeError('Cloud source access or official GeM access requires attention.')
                if status == 200:
                    return headers,body
                if status not in (None,429,500,502,503,504) or attempt == 2:
                    raise PortalError('Cloud source request failed '+
                        ('at transport stage.' if status is None else 'with HTTP '+str(status)+'.'))
        raise PortalError('Cloud source request did not complete.')

    @staticmethod
    def _json(body):
        try:
            result = json.loads(body)
            if not isinstance(result,dict) or result.get('source') != 'GeM':
                raise ValueError
            return result
        except (ValueError,UnicodeError):
            raise PortalError('Cloud source returned unexpected public GeM metadata.') from None

    async def health(self):
        _,body = await self._request('/health')
        result = self._json(body)
        if result.get('status') != 'reachable':
            raise PortalError('Cloud source did not verify public GeM access.')
        return result

    async def search(self, search='', page=1, sort='Bid-End-Date-Oldest'):
        if not isinstance(search,str) or len(search.strip()) > 200:
            raise ValueError('Search must be text with at most200characters')
        if isinstance(page,bool) or not isinstance(page,int) or not 1 <= page <= 100000 or sort not in SORTS:
            raise ValueError('Invalid public GeM page or sort')
        _,body = await self._request('/listings',{'search':search.strip(),'page':page,'sort':sort})
        result = self._json(body)
        try:
            if (isinstance(result.get('total'),bool) or not isinstance(result.get('total'),int) or result['total'] < 0 or
                    isinstance(result.get('page'),bool) or result.get('page') != page or not isinstance(result.get('has_more'),bool) or
                    not isinstance(result.get('tenders'),list) or len(result['tenders']) > 100):
                raise ValueError
            timestamp(result['fetched_at'])
            for record in result['tenders']:
                validate_record(record)
                route,_ = document_route(record['source_document_url'])
                if route != '/documents/'+record['id']:
                    raise ValueError
        except (ValueError,TypeError,KeyError,AttributeError):
            raise PortalError('Cloud source returned invalid GeM listing identities or fields.') from None
        result['tenders'] = [{key:value for key,value in record.items() if key != 'source_data'}
            for record in result['tenders']]
        return result

    async def download(self, url, **kwargs):
        path,query = document_route(url)
        _,body = await self._request(path,query,max_bytes=MAX_PDF_BYTES)
        if not body.startswith(b'%PDF-'):
            raise PortalError('Cloud source did not return an original PDF document.')
        return body
