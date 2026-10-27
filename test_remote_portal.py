"""Offline remote-source checks; fake urllib handlers never open a socket."""
import asyncio
import copy
from email.message import Message
import io
import json
import unittest
from unittest import mock
import urllib.request
from urllib.response import addinfourl

from remote_portal import (
    RemotePortal, NoRedirect, configuration, document_route, MAX_METADATA_BYTES, MAX_PDF_BYTES,
)
from tenderbot_crawler.gem import PortalError, PortalChallengeError


BASE = 'https://tenderbot.example-workspace.chatgpt.site/api/cloud-source'
TOKEN = 'offline-service-secret'
RECORD = {'id':'gem-1234567','tender_id':'GEM/2026/B/1234567','title':'Real public bid',
    'source_document_url':'https://bidplus.gem.gov.in/showbidDocument/1234567',
    'source_fetched_at':'2026-10-27T09:00:00+00:00','demo':False}
LISTING = {'source':'GeM','tenders':[RECORD],'total':41659,'page':1,'has_more':True,
    'fetched_at':'2026-10-27T09:00:00+00:00'}


class Response(io.BytesIO):
    def __init__(self,url,body,status=200,headers=None):
        super().__init__(body)
        self.url,self.status,self.headers = url,status,headers or {}


def opener_for(body,status=200,headers=None):
    opener = mock.Mock()
    opener.open.side_effect = lambda request,timeout: Response(request.full_url,body,status,headers)
    return opener


class RemotePortalTests(unittest.IsolatedAsyncioTestCase):
    def test_invalid_config_cannot_send_secret_to_another_origin_or_query(self):
        invalid = ['http://example.chatgpt.site/api/cloud-source',
            'https://chatgpt.site/api/cloud-source',
            'https://example.chatgpt.site.evil.test/api/cloud-source',
            'https://user:password@example.chatgpt.site/api/cloud-source',
            BASE+'?token='+TOKEN,BASE+'#fragment',BASE+'/',BASE+'/listings',
            'https://example.chatgpt.site:8443/api/cloud-source',
            'https://example.chatgpt.site\\@evil.test/api/cloud-source',
            '\n'+BASE]
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(PortalError) as error:
                configuration(url,TOKEN)
            self.assertNotIn(TOKEN,str(error.exception))
        for token in ('',None,'bad\r\nAuthorization:value'):
            with self.assertRaises(PortalError):
                configuration(BASE,token)

    async def test_normalized_real_listing_and_credential_boundary(self):
        opener = opener_for(json.dumps(LISTING).encode())
        async with RemotePortal(BASE,TOKEN,opener=opener) as portal:
            result = await portal.search(search='paper & toner')
        self.assertEqual(result['total'],41659)
        request = opener.open.call_args.args[0]
        self.assertTrue(request.full_url.startswith(BASE+'/listings?'))
        self.assertIn('search=paper+%26+toner',request.full_url)
        self.assertNotIn(TOKEN,request.full_url)
        headers = {key.lower():value for key,value in request.header_items()}
        self.assertEqual(headers['oai-sites-authorization'],'Bearer '+TOKEN)
        self.assertNotIn('authorization',headers)

    async def test_real_urllib_redirect_handling_never_forwards_credential(self):
        requests = []

        class FakeHTTPS(urllib.request.HTTPSHandler):
            def https_open(self,request):
                requests.append(request)
                headers = Message()
                headers['Location'] = 'https://evil.test/steal'
                response = addinfourl(io.BytesIO(b''),headers,request.full_url,302)
                response.msg = 'Found'
                return response

        opener = urllib.request.build_opener(NoRedirect(),FakeHTTPS(),urllib.request.ProxyHandler({}))
        async with RemotePortal(BASE,TOKEN,opener=opener) as portal:
            with self.assertRaisesRegex(PortalError,'redirect rejected'):
                await portal.health()
        self.assertEqual(len(requests),1)
        self.assertEqual(requests[0].full_url,BASE+'/health')

    async def test_invalid_source_identity_is_rejected_before_persistence(self):
        invalid = [
            {**RECORD,'demo':True},
            {**RECORD,'source_document_url':'https://evil.test/1234567'},
            {**RECORD,'source_document_url':'https://bidplus.gem.gov.in/showbidDocument/9999999'},
            {**RECORD,'tender_id':'Invented'},
        ]
        for record in invalid:
            opener = opener_for(json.dumps({**LISTING,'tenders':[record]}).encode())
            async with RemotePortal(BASE,TOKEN,opener=opener) as portal:
                with self.assertRaises(PortalError):
                    await portal.search()

    async def test_only_fixed_official_document_paths_map_to_source_api(self):
        kinds = ('showbidDocument','showradocumentPdf','showdirectradocumentPdf','list-ra-schedules')
        for kind in kinds:
            self.assertEqual(document_route('https://bidplus.gem.gov.in/'+kind+'/1234567'),
                ('/documents/gem-1234567',{'kind':kind}))
        invalid = ['https://bidplus.gem.gov.in.evil.test/showbidDocument/1234567',
            'https://user:pass@bidplus.gem.gov.in/showbidDocument/1234567',
            'https://bidplus.gem.gov.in:8443/showbidDocument/1234567',
            'https://bidplus.gem.gov.in/showbidDocument/1234567?redirect=https://evil.test',
            'https://bidplus.gem.gov.in/showbidDocument/1234567#fragment',
            'https://bidplus.gem.gov.in/showbidDocument/../1234567',
            'https://bidplus.gem.gov.in/showbidDocument/%31',
            'https://gem.gov.in/showbidDocument/1234567',
            'http://bidplus.gem.gov.in/showbidDocument/1234567']
        opener = opener_for(b'%PDF-1.7\nreal bytes')
        async with RemotePortal(BASE,TOKEN,opener=opener) as portal:
            for url in invalid:
                with self.subTest(url=url),self.assertRaises(PortalError):
                    await portal.download(url)
            opener.open.assert_not_called()
            self.assertEqual(await portal.download(RECORD['source_document_url']),b'%PDF-1.7\nreal bytes')
        self.assertEqual(opener.open.call_args.args[0].full_url,BASE+'/documents/gem-1234567?kind=showbidDocument')

    async def test_authorization_and_explicit_remote_challenges_stop_without_retry(self):
        for status,body in ((401,b''),(403,b''),(503,b'{"error_type":"access_restricted"}')):
            opener = opener_for(body,status)
            async with RemotePortal(BASE,TOKEN,opener=opener) as portal:
                with self.assertRaises(PortalChallengeError) as error:
                    await portal.health()
            self.assertEqual(opener.open.call_count,1)
            self.assertNotIn(TOKEN,str(error.exception))

    async def test_transport_failure_is_bounded_paced_and_does_not_leak_error_values(self):
        opener = mock.Mock()
        opener.open.side_effect = OSError('Untrusted error '+TOKEN)
        with mock.patch('remote_portal.asyncio.sleep',new_callable=mock.AsyncMock) as sleep:
            async with RemotePortal(BASE,TOKEN,opener=opener) as portal:
                with self.assertRaises(PortalError) as error:
                    await portal.health()
        self.assertEqual(opener.open.call_count,3)
        self.assertEqual(sleep.await_count,2)
        self.assertTrue(all(call.args[0] >= 1 for call in sleep.await_args_list))
        self.assertNotIn(TOKEN,str(error.exception))

    async def test_metadata_and_pdf_size_limits_and_non_pdf_bytes(self):
        cases = [('/health',MAX_METADATA_BYTES+1,False),('/documents',MAX_PDF_BYTES+1,True)]
        for _,size,pdf in cases:
            opener = opener_for(b'',headers={'Content-Length':str(size)})
            async with RemotePortal(BASE,TOKEN,opener=opener) as portal:
                with self.assertRaisesRegex(PortalError,'byte limit'):
                    if pdf:
                        await portal.download(RECORD['source_document_url'])
                    else:
                        await portal.health()
        opener = opener_for(b'<html>Not a PDF</html>')
        async with RemotePortal(BASE,TOKEN,opener=opener) as portal:
            with self.assertRaisesRegex(PortalError,'PDF'):
                await portal.download(RECORD['source_document_url'])

    async def test_health_verifies_gem_source_and_reachable_status(self):
        opener = opener_for(b'{"source":"GeM","status":"reachable","source_total":41659}')
        async with RemotePortal(BASE,TOKEN,opener=opener) as portal:
            self.assertEqual((await portal.health())['source_total'],41659)
        for body in (b'{"source":"Other","status":"reachable"}',b'{"source":"GeM","status":"failed"}'):
            async with RemotePortal(BASE,TOKEN,opener=opener_for(body)) as portal:
                with self.assertRaises(PortalError):
                    await portal.health()


if __name__ == '__main__':
    unittest.main()
