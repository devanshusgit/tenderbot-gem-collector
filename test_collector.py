"""Offline checks for persisted cloud collection and bounded PDF retries."""
import copy
import datetime as dt
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import collector
from tenderbot_crawler.gem import PortalChallengeError, PortalError
from tenderbot_crawler.storage import atomic_json, timestamp


ROOT = Path(__file__).resolve().parent
RECORD = {
    'id':'gem-1234567','tender_id':'GEM/2026/B/1234567','title':'Public bid',
    'description':'Current listing','quantity':50,'status':'active',
    'source_document_url':'https://bidplus.gem.gov.in/showbidDocument/1234567',
    'source_fetched_at':'2026-10-27T09:00:00+00:00',
    'closing_at':'2026-11-01T18:00:00+05:30','state':[],'city':[],
    'emd_fee':None,'tender_value':None,
}


class Clock(dt.datetime):
    current = dt.datetime(2026,10,27,10,tzinfo=dt.timezone.utc)

    @classmethod
    def now(cls,tz=None):
        return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)


class GithubCollectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        (ROOT/'.local').mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT/'.local')
        self.folder = Path(self.temporary.name)
        (self.folder/'records').mkdir()
        Clock.current = dt.datetime(2026,10,27,10,tzinfo=dt.timezone.utc)
        self.clock_patch = mock.patch.object(collector.dt,'datetime',Clock)
        self.clock_patch.start()
        self.environment_patch = mock.patch.dict(os.environ,
            {'GEM_CLOUDFLARE_SOURCE_URL':'','TENDERBOT_SITE_SERVICE_TOKEN':''})
        self.environment_patch.start()

    def tearDown(self):
        self.clock_patch.stop()
        self.environment_patch.stop()
        self.temporary.cleanup()

    def saved(self,record=RECORD):
        return collector.read(self.folder/'records'/(record['id']+'.json'),{})

    def bid(self,number,published_at):
        return {**RECORD,'id':'gem-'+str(number),'tender_id':'GEM/2026/B/'+str(number),
            'source_document_url':'https://bidplus.gem.gov.in/showbidDocument/'+str(number),
            'published_at':published_at,'source_fetched_at':collector.stamp()}

    def portal(self,search):
        portal = mock.MagicMock()
        portal.__aenter__ = mock.AsyncMock(return_value=portal)
        portal.__aexit__ = mock.AsyncMock(return_value=False)
        portal.search = mock.AsyncMock(side_effect=search)
        return portal

    async def test_configured_remote_failure_never_uses_browser_or_direct_gem_probe(self):
        environment = {'GEM_CLOUDFLARE_SOURCE_URL':'https://tenderbot.example.chatgpt.site/api/cloud-source',
            'TENDERBOT_SITE_SERVICE_TOKEN':'offline-site-secret'}
        with mock.patch.dict(os.environ,environment), \
                mock.patch.object(collector,'RemotePortal',side_effect=PortalError('Remote source unavailable')), \
                mock.patch.object(collector,'GemPortal') as browser, \
                mock.patch.object(collector.urllib.request,'urlopen') as direct_source:
            self.assertEqual(await collector.collect(self.folder,seconds=1,pdf_seconds=0),1)
        browser.assert_not_called()
        direct_source.assert_not_called()
        self.assertEqual(collector.read(self.folder/'state.json',{})['source_runtime'],'cloudflare-workers')

    async def test_remote_collect_keeps_github_as_orchestrator_and_cloudflare_as_source(self):
        portal = self.portal(lambda **kwargs:{'tenders':[],'total':0,'has_more':False})
        environment = {'GEM_CLOUDFLARE_SOURCE_URL':'https://tenderbot.example.chatgpt.site/api/cloud-source',
            'TENDERBOT_SITE_SERVICE_TOKEN':'offline-site-secret'}
        with mock.patch.dict(os.environ,environment), \
                mock.patch.object(collector,'RemotePortal',return_value=portal) as remote, \
                mock.patch.object(collector,'GemPortal') as browser:
            self.assertEqual(await collector.collect(self.folder,seconds=1,pdf_seconds=0),0)
        browser.assert_not_called()
        remote.assert_called_once_with(delay=1)
        status = collector.read(self.folder/'collector.json',{})
        self.assertEqual(status['runtime'],'github-actions')
        self.assertEqual(status['source_runtime'],'cloudflare-workers')

    def test_first_refresh_uses_ingestion_cutoff_and_partial_run_keeps_it(self):
        receipt = '2026-10-25T19:30:00+00:00'
        self.assertEqual(collector.newest_cutoff({}, {'fetched_at':receipt}),
            timestamp(receipt)-dt.timedelta(hours=1))
        partial = {'newest_refresh_complete':False,'newest_refresh_cutoff':None,
            'last_success_at':collector.stamp()}
        self.assertIsNone(collector.newest_cutoff(partial,{'fetched_at':collector.stamp()}))

    async def test_newest_refresh_traverses_more_than_three_pages_and_clears_stale_failure(self):
        Clock.current = dt.datetime(2026,10,27,11,tzinfo=dt.timezone.utc)
        state = {'page':7,'complete':False,'newest_refresh_watermark':'2026-10-27T10:00:00+00:00',
            'error_message':'Old connection refusal',
            'source_health':{'status':'unavailable','stage':'connect','errno':111}}
        atomic_json(self.folder/'state.json',state)
        published = ['10:40','10:20','10:00','09:20','08:50']

        async def search(page,sort='Bid-End-Date-Oldest'):
            if sort == 'Bid-Start-Date-Latest':
                return {'tenders':[self.bid(1000000+page,'2026-10-27T'+published[page-1]+':00+00:00')],
                    'total':100,'has_more':True}
            return {'tenders':[self.bid(2000000,'2026-10-26T08:00:00+00:00')],
                'total':100,'has_more':False}

        portal = self.portal(search)
        with mock.patch.object(collector,'GemPortal',return_value=portal):
            result = await collector.collect(self.folder,seconds=100,pdf_seconds=0)
        self.assertEqual(result,0)
        saved = collector.read(self.folder/'state.json',{})
        self.assertEqual(saved['newest_refresh_pages'],5)
        self.assertTrue(saved['newest_refresh_complete'])
        self.assertEqual(saved['newest_refresh_cutoff'],'2026-10-27T09:00:00+00:00')
        self.assertEqual(saved['newest_refresh_watermark'],saved['newest_refresh_started_at'])
        self.assertEqual(saved['page'],8)
        self.assertNotIn('error_message',saved)
        self.assertEqual(saved['source_health'],{'status':'reachable','stage':'http','http_status':200})
        self.assertFalse(collector.read(self.folder/'collector.json',{})['exact_coverage_verified'])

    async def test_incomplete_newest_pass_resumes_with_overlap_without_advancing_watermark(self):
        atomic_json(self.folder/'state.json',{'page':7,'complete':False})
        elapsed = [0]

        async def search(page,sort='Bid-End-Date-Oldest'):
            elapsed[0] += 2 if sort == 'Bid-Start-Date-Latest' else 10
            return {'tenders':[self.bid(1000000+page,'2026-10-27T09:00:00+00:00')],
                'total':1000,'has_more':True}

        portal = self.portal(search)
        with mock.patch.object(collector,'GemPortal',return_value=portal), \
                mock.patch.object(collector.time,'monotonic',side_effect=lambda:elapsed[0]):
            self.assertEqual(await collector.collect(self.folder,seconds=10,pdf_seconds=0),0)
            first = collector.read(self.folder/'state.json',{})
            self.assertFalse(first['newest_refresh_complete'])
            self.assertEqual(first['newest_refresh_pages'],2)
            self.assertNotIn('newest_refresh_watermark',first)
            self.assertIsNone(first['newest_refresh_cutoff'])
            Clock.current += dt.timedelta(hours=1)
            self.assertEqual(await collector.collect(self.folder,seconds=10,pdf_seconds=0),0)
        saved = collector.read(self.folder/'state.json',{})
        self.assertFalse(saved['newest_refresh_complete'])
        self.assertEqual(saved['newest_refresh_pages'],3)
        self.assertEqual(saved['newest_refresh_started_at'],first['newest_refresh_started_at'])
        self.assertIsNone(saved['newest_refresh_cutoff'])
        self.assertNotIn('newest_refresh_watermark',saved)
        self.assertEqual(saved['page'],9)
        self.assertFalse(saved['complete'])
        newest_pages = [call.kwargs['page'] for call in portal.search.await_args_list
            if call.kwargs.get('sort') == 'Bid-Start-Date-Latest']
        self.assertEqual(newest_pages,[1,2,2,3])

    async def test_empty_middle_full_feed_page_fails_without_advancing_checkpoint(self):
        atomic_json(self.folder/'state.json',{'page':7,'complete':False,
            'newest_refresh_watermark':'2026-10-27T10:00:00+00:00'})

        async def search(page,sort='Bid-End-Date-Oldest'):
            if sort == 'Bid-Start-Date-Latest':
                return {'tenders':[self.bid(1000000,'2026-10-27T08:00:00+00:00')],
                    'total':100,'has_more':True}
            return {'tenders':[],'total':100,'has_more':True}

        portal = self.portal(search)
        with mock.patch.object(collector,'GemPortal',return_value=portal), \
                mock.patch.object(collector.urllib.request,'urlopen',side_effect=OSError('Offline test')):
            self.assertEqual(await collector.collect(self.folder,seconds=100,pdf_seconds=0),1)
        saved = collector.read(self.folder/'state.json',{})
        self.assertEqual(saved['page'],7)
        self.assertFalse(saved['complete'])
        self.assertEqual(saved['error'],'PortalError')
        self.assertIn('empty page',saved['error_message'])

    async def test_resumed_completion_uses_original_pass_start_as_watermark(self):
        Clock.current = dt.datetime(2026,10,27,11,tzinfo=dt.timezone.utc)
        original_start = '2026-10-27T09:00:00+00:00'
        state = {'newest_refresh_complete':False,'newest_refresh_pages':4,
            'newest_refresh_started_at':original_start,
            'newest_refresh_cutoff':'2026-10-27T08:00:00+00:00'}

        async def search(page,sort):
            published = '2026-10-27T09:00:00+00:00' if page == 4 else '2026-10-27T07:00:00+00:00'
            return {'tenders':[self.bid(1000000+page,published)],'total':100,'has_more':True}

        portal = self.portal(search)
        await collector.refresh_newest(self.folder,portal,state,seconds=100,
            cutoff=collector.newest_cutoff(state,{}))
        self.assertTrue(state['newest_refresh_complete'])
        self.assertEqual(state['newest_refresh_watermark'],original_start)
        self.assertNotEqual(state['newest_refresh_watermark'],state['newest_refresh_finished_at'])
        self.assertEqual([call.kwargs['page'] for call in portal.search.await_args_list],[4,5])

    async def test_empty_middle_newest_page_preserves_incomplete_pass_and_cursor(self):
        state = {'page':7,'complete':False}

        async def search(page,sort):
            return {'tenders':[self.bid(1000000,'2026-10-27T09:00:00+00:00')] if page == 1 else [],
                'total':100,'has_more':True}

        portal = self.portal(search)
        with self.assertRaises(PortalError):
            await collector.refresh_newest(self.folder,portal,state,seconds=100,cutoff=None)
        self.assertFalse(state['newest_refresh_complete'])
        self.assertEqual(state['newest_refresh_pages'],1)
        self.assertNotIn('newest_refresh_watermark',state)
        self.assertEqual(state['newest_refresh_stop_reason'],'error')
        self.assertEqual(state['page'],7)

    def test_listing_refresh_preserves_recorded_pdf_location_and_retry(self):
        previous = {**RECORD,'state':['Maharashtra'],'city':['Mumbai'],'emd_fee':2500,
            'source_document_fetched_at':'2026-10-27T08:00:00+00:00',
            'document_sha256':'abc','detail_status':'parsed',
            'document_retry':{'attempts':1,'next_attempt_at':'2026-10-28T10:00:00+00:00'}}
        atomic_json(self.folder/'records'/(RECORD['id']+'.json'),previous)
        newer = {**RECORD,'source_fetched_at':'2026-10-27T10:00:00+00:00',
            'closing_at':'2026-11-03T18:00:00+05:30','source_data':{'unpublished':'raw'}}
        collector.save_listing(self.folder,newer)
        saved = self.saved()
        self.assertEqual((saved['state'],saved['city']),(['Maharashtra'],['Mumbai']))
        self.assertEqual(saved['emd_fee'],2500)
        self.assertEqual(saved['closing_at'],newer['closing_at'])
        self.assertEqual(saved['source_document_fetched_at'],previous['source_document_fetched_at'])
        self.assertEqual(saved['document_retry'],previous['document_retry'])
        self.assertNotIn('source_data',saved)
        collector.save_listing(self.folder,{**newer,'city':['Pune']})
        self.assertEqual(self.saved()['city'],['Pune'])

    def test_changed_document_does_not_inherit_old_evidence_or_exhausted_retry(self):
        previous = {**RECORD,'state':['Maharashtra'],'city':['Mumbai'],
            'source_document_fetched_at':'2026-10-27T08:00:00+00:00',
            'document_sha256':'abc','eligibility':[{'text':'Old PDF requirement'}],
            'document_retry':{'attempts':3,'status':'exhausted'}}
        atomic_json(self.folder/'records'/(RECORD['id']+'.json'),previous)
        collector.save_listing(self.folder,{**RECORD,
            'source_document_url':RECORD['source_document_url']+'?revision=2'})
        saved = self.saved()
        self.assertEqual(saved['state'],[])
        for key in ('source_document_fetched_at','document_sha256','eligibility','document_retry'):
            self.assertNotIn(key,saved)
        self.assertTrue(collector.document_due(saved,Clock.current))

    def test_older_listing_cannot_overwrite_newer_location_or_deadline(self):
        collector.save_listing(self.folder,RECORD)
        collector.save_listing(self.folder,{**RECORD,
            'source_fetched_at':'2026-10-26T10:00:00+00:00',
            'city':['Old city'],'closing_at':'2026-10-28T18:00:00+05:30'})
        self.assertEqual(self.saved(),RECORD)

    async def test_failed_document_is_deferred_so_next_run_processes_other_bid(self):
        bad = copy.deepcopy(RECORD)
        good = {**RECORD,'id':'gem-2345678','tender_id':'GEM/2026/B/2345678',
            'source_document_url':'https://bidplus.gem.gov.in/showbidDocument/2345678'}
        elapsed = [0]

        async def failed_download(url):
            elapsed[0] += 2
            raise ValueError('Unreadable official document')

        portal = mock.Mock(download=mock.AsyncMock(side_effect=failed_download))
        with mock.patch.object(collector.time,'monotonic',side_effect=lambda:elapsed[0]):
            await collector.process_documents(self.folder,portal,[bad,good],seconds=1)
        self.assertEqual(portal.download.await_count,1)
        failed = self.saved(bad)
        self.assertEqual(failed['document_retry']['attempts'],1)
        self.assertFalse(collector.document_due(failed,Clock.current))
        details = {'tender_id':good['tender_id'],'source_document_fetched_at':collector.stamp(),
            'detail_status':'parsed','document_sha256':'good-sha'}
        portal.download = mock.AsyncMock(return_value=b'%PDF-offline fixture')
        temporary = tempfile.TemporaryDirectory
        with mock.patch.object(collector,'parse_pdf',return_value=details), \
                mock.patch.object(collector.tempfile,'TemporaryDirectory',side_effect=lambda:temporary(dir=self.folder)):
            await collector.process_documents(self.folder,portal,[failed,good],seconds=10)
        portal.download.assert_awaited_once_with(good['source_document_url'])
        self.assertEqual(self.saved(good)['document_sha256'],'good-sha')
        self.assertEqual(self.saved(bad)['document_retry']['attempts'],1)

    async def test_failures_stop_after_three_attempts_and_listing_refresh_keeps_cap(self):
        portal = mock.Mock(download=mock.AsyncMock(side_effect=ValueError('Invalid PDF')))
        current = copy.deepcopy(RECORD)
        for attempt in range(1,4):
            await collector.process_documents(self.folder,portal,[current],seconds=10)
            current = self.saved()
            retry = current['document_retry']
            self.assertEqual(retry['attempts'],attempt)
            if attempt < 3:
                self.assertEqual(retry['status'],'retry')
                self.assertGreater(timestamp(retry['next_attempt_at']),Clock.current)
                Clock.current = timestamp(retry['next_attempt_at'])
        self.assertEqual(current['document_retry']['status'],'exhausted')
        self.assertIsNone(current['document_retry']['next_attempt_at'])
        Clock.current += dt.timedelta(days=365)
        collector.save_listing(self.folder,{**RECORD,'source_fetched_at':collector.stamp()})
        await collector.process_documents(self.folder,portal,[self.saved()],seconds=10)
        self.assertEqual(portal.download.await_count,3)

    async def test_success_clears_retry_without_replacing_current_listing_facts(self):
        retry = {**RECORD,'document_retry':{'attempts':1}}
        details = {'tender_id':RECORD['tender_id'],'quantity':999,
            'closing_at':'2026-10-28T18:00:00+05:30',
            'source_document_fetched_at':collector.stamp(),'detail_status':'parsed'}
        portal = mock.Mock(download=mock.AsyncMock(return_value=b'%PDF-offline fixture'))
        temporary = tempfile.TemporaryDirectory
        with mock.patch.object(collector,'parse_pdf',return_value=details), \
                mock.patch.object(collector.tempfile,'TemporaryDirectory',side_effect=lambda:temporary(dir=self.folder)):
            await collector.process_documents(self.folder,portal,[retry],seconds=10)
        saved = self.saved()
        self.assertNotIn('document_retry',saved)
        self.assertEqual(saved['quantity'],RECORD['quantity'])
        self.assertEqual(saved['closing_at'],RECORD['closing_at'])
        self.assertFalse(collector.document_due(saved,Clock.current))

    async def test_access_challenge_stops_batch_without_consuming_document_retries(self):
        portal = mock.Mock(download=mock.AsyncMock(side_effect=PortalChallengeError('Access restricted')))
        with self.assertRaises(PortalChallengeError):
            await collector.process_documents(self.folder,portal,[RECORD],seconds=10)
        self.assertEqual(self.saved(),{})


if __name__ == '__main__':
    unittest.main()
