"""Offline checks for persisted cloud collection and bounded PDF retries."""
import copy
import datetime as dt
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import collector
from tenderbot_crawler.gem import PortalChallengeError, PortalError, PortalHTTPError
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

    def stale_ra(self,number=1234567):
        return {**self.bid(number,None),'tender_id':'GEM/2026/R/'+str(number),
            'source_document_url':'https://bidplus.gem.gov.in/showradocumentPdf/'+str(number),
            'source_document_fetched_at':'2026-10-26T09:00:00+00:00',
            'detail_status':'unrecognized_document'}

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
                mock.patch.object(collector,'time',mock.Mock(monotonic=lambda:elapsed[0])):
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
        with mock.patch.object(collector,'time',mock.Mock(monotonic=lambda:elapsed[0])):
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

    async def test_document_priority_serves_current_and_unknown_bids_before_expired_notices(self):
        expired = {**self.bid(1000001,'2026-10-27T09:59:00+00:00'),
            'closing_at':'2026-10-26T10:00:00+00:00'}
        open_retry = {**self.bid(1000002,'2026-10-27T09:59:00+00:00'),
            'document_retry':{'attempts':1,'next_attempt_at':'2026-10-27T09:00:00+00:00'}}
        unknown = {**self.bid(1000003,'2026-10-27T09:30:00+00:00'),'closing_at':None}
        open_old = self.bid(1000004,'2026-10-26T09:00:00+00:00')
        open_new = self.bid(1000005,'2026-10-27T09:45:00+00:00')
        cancelled = {**self.bid(1000006,'2026-10-27T09:58:00+00:00'),'status':'cancelled'}
        parsed = {**self.bid(1000007,'2026-10-27T09:59:00+00:00'),
            'source_document_fetched_at':collector.stamp(),'detail_status':'parsed'}
        deferred = {**self.bid(1000008,'2026-10-27T09:59:00+00:00'),
            'document_retry':{'attempts':1,'next_attempt_at':'2026-10-28T10:00:00+00:00'}}
        portal = mock.Mock(download=mock.AsyncMock(side_effect=ValueError('Offline unreadable PDF')))
        records = [expired,open_retry,unknown,open_old,open_new,cancelled,parsed,deferred]
        await collector.process_documents(self.folder,portal,records,seconds=10)
        self.assertEqual([call.args[0] for call in portal.download.await_args_list],
            [record['source_document_url'] for record in
                (open_new,unknown,open_old,expired,cancelled,open_retry)])
        self.assertNotIn('status',self.saved(parsed))
        self.assertEqual(self.saved(expired)['status'],'active')
        self.assertEqual(self.saved(cancelled)['status'],'cancelled')

    def test_document_priority_uses_source_receipt_when_publication_is_unknown(self):
        older = {**RECORD,'source_fetched_at':'2026-10-26T09:00:00+00:00'}
        newer = {**RECORD,'source_fetched_at':'2026-10-27T09:00:00+00:00'}
        self.assertLess(collector.document_priority(newer,Clock.current),
            collector.document_priority(older,Clock.current))
        unknown_close = {**older,'closing_at':'Not disclosed'}
        self.assertEqual(collector.document_priority(unknown_close,Clock.current)[2],0)

    def test_queue_counts_distinguish_downloaded_parsed_due_deferred_and_exhausted(self):
        parsed = {**self.bid(1000001,None),'source_document_fetched_at':collector.stamp(),'detail_status':'parsed'}
        unreadable = {**self.bid(1000002,None),'source_document_fetched_at':collector.stamp(),'detail_status':'no_readable_text'}
        unrecognized = {**self.bid(1000003,None),'source_document_fetched_at':collector.stamp(),'detail_status':'unrecognized_document'}
        due = self.bid(1000004,None)
        deferred = {**self.bid(1000005,None),
            'document_retry':{'attempts':1,'next_attempt_at':'2026-10-28T10:00:00+00:00'}}
        exhausted = {**self.bid(1000006,None),'document_retry':{'attempts':3,'status':'exhausted'}}
        collector.publish_index(self.folder,{'page':99},[parsed,unreadable,unrecognized,due,deferred,exhausted])
        status = collector.read(self.folder/'collector.json',{})
        self.assertEqual(status['documents'],3)
        self.assertEqual(status['downloaded_documents'],3)
        self.assertEqual(status['parsed_documents'],1)
        self.assertEqual(status['pending_documents'],3)
        self.assertEqual(status['eligible_documents'],1)
        self.assertEqual(status['deferred_documents'],1)
        self.assertEqual(status['exhausted_documents'],1)

    async def test_documents_only_preserves_listing_cursors_and_source_receipts(self):
        state = {'page':345,'source_total':42000,'complete':True,
            'newest_refresh_pages':70,'newest_refresh_complete':False,
            'newest_refresh_cutoff':'2026-10-25T09:00:00+00:00',
            'newest_refresh_started_at':'2026-10-26T09:00:00+00:00',
            'newest_refresh_watermark':'2026-10-25T10:00:00+00:00',
            'last_success_at':'2026-10-26T10:00:00+00:00'}
        atomic_json(self.folder/'state.json',state)
        collector.save_listing(self.folder,RECORD)
        portal = self.portal(mock.Mock(side_effect=AssertionError('No listing searches allowed')))
        portal.download = mock.AsyncMock(return_value=b'%PDF-offline fixture')
        temporary = tempfile.TemporaryDirectory
        details = {'source_document_fetched_at':collector.stamp(),'detail_status':'parsed','emd_fee':2500}
        environment = {'GEM_CLOUDFLARE_SOURCE_URL':'https://tenderbot.example.chatgpt.site/api/cloud-source',
            'TENDERBOT_SITE_SERVICE_TOKEN':'offline-site-secret'}
        with mock.patch.dict(os.environ,environment), \
                mock.patch.object(collector,'RemotePortal',return_value=portal), \
                mock.patch.object(collector,'GemPortal') as browser, \
                mock.patch.object(collector,'parse_pdf',return_value=details), \
                mock.patch.object(collector.tempfile,'TemporaryDirectory',side_effect=lambda:temporary(dir=self.folder)):
            self.assertEqual(await collector.collect_documents(self.folder,seconds=30),0)
        portal.search.assert_not_awaited()
        browser.assert_not_called()
        saved_state = collector.read(self.folder/'state.json',{})
        for key,value in state.items():
            self.assertEqual(saved_state[key],value)
        saved = self.saved()
        self.assertEqual(saved['source_fetched_at'],RECORD['source_fetched_at'])
        self.assertEqual(saved['closing_at'],RECORD['closing_at'])
        self.assertEqual(saved['emd_fee'],2500)
        status = collector.read(self.folder/'collector.json',{})
        self.assertEqual(status['fetched_at'],RECORD['source_fetched_at'])
        self.assertEqual(status['eligible_documents'],0)
        self.assertEqual(status['parsed_documents'],1)
        self.assertEqual(status['mode'],'documents')
        self.assertEqual(status['runtime'],'github-actions')
        self.assertEqual(status['source_runtime'],'cloudflare-workers')

    async def test_documents_only_source_failure_keeps_checkpoint_and_avoids_direct_fallback(self):
        state = {'page':345,'complete':False,'newest_refresh_pages':70,
            'newest_refresh_complete':True,'last_success_at':'2026-10-26T10:00:00+00:00'}
        atomic_json(self.folder/'state.json',state)
        collector.save_listing(self.folder,RECORD)
        environment = {'GEM_CLOUDFLARE_SOURCE_URL':'https://tenderbot.example.chatgpt.site/api/cloud-source',
            'TENDERBOT_SITE_SERVICE_TOKEN':'offline-site-secret'}
        with mock.patch.dict(os.environ,environment), \
                mock.patch.object(collector,'RemotePortal',side_effect=PortalError('Remote source unavailable')), \
                mock.patch.object(collector,'GemPortal') as browser, \
                mock.patch.object(collector.urllib.request,'urlopen') as direct:
            self.assertEqual(await collector.collect_documents(self.folder,seconds=30),1)
        browser.assert_not_called()
        direct.assert_not_called()
        saved_state = collector.read(self.folder/'state.json',{})
        for key,value in state.items():
            self.assertEqual(saved_state[key],value)
        self.assertEqual(saved_state['status'],'failed')
        self.assertEqual(saved_state['error'],'PortalError')
        self.assertEqual(self.saved()['source_fetched_at'],RECORD['source_fetched_at'])

    async def test_documents_only_challenge_stops_immediately_without_retry_or_cursor_change(self):
        state = {'page':345,'complete':False,'newest_refresh_pages':70,'newest_refresh_complete':True}
        atomic_json(self.folder/'state.json',state)
        collector.save_listing(self.folder,RECORD)
        collector.save_listing(self.folder,self.bid(2000000,'2026-10-26T09:00:00+00:00'))
        portal = self.portal(mock.Mock(side_effect=AssertionError('No listing searches allowed')))
        portal.download = mock.AsyncMock(side_effect=PortalChallengeError('Access restricted'))
        with mock.patch.object(collector,'GemPortal',return_value=portal):
            self.assertEqual(await collector.collect_documents(self.folder,seconds=30),1)
        portal.search.assert_not_awaited()
        portal.download.assert_awaited_once()
        saved_state = collector.read(self.folder/'state.json',{})
        for key,value in state.items():
            self.assertEqual(saved_state[key],value)
        self.assertEqual(saved_state['status'],'blocked')
        self.assertNotIn('document_retry',self.saved())

    async def test_documents_only_budget_is_bounded_before_opening_source(self):
        with mock.patch.object(collector,'GemPortal') as source:
            for budget in (0,901,float('inf'),float('nan'),True):
                with self.assertRaises(ValueError):
                    await collector.collect_documents(self.folder,seconds=budget)
        source.assert_not_called()

    async def test_consecutive_transient_document_failures_persist_then_fail_batch(self):
        state = {'page':345,'complete':False,'newest_refresh_pages':70,'newest_refresh_complete':True}
        atomic_json(self.folder/'state.json',state)
        records = [self.bid(1000000+number,None) for number in range(1,5)]
        for record in records:
            collector.save_listing(self.folder,record)
        portal = self.portal(mock.Mock(side_effect=AssertionError('No listing searches allowed')))
        portal.download = mock.AsyncMock(side_effect=PortalError('Cloud source request failed with HTTP 503.'))
        environment = {'GEM_CLOUDFLARE_SOURCE_URL':'https://tenderbot.example.chatgpt.site/api/cloud-source',
            'TENDERBOT_SITE_SERVICE_TOKEN':'offline-site-secret'}
        with mock.patch.dict(os.environ,environment), \
                mock.patch.object(collector,'RemotePortal',return_value=portal), \
                mock.patch.object(collector.urllib.request,'urlopen') as direct:
            self.assertEqual(await collector.collect_documents(self.folder,seconds=30),1)
        self.assertEqual(portal.download.await_count,3)
        portal.search.assert_not_awaited()
        direct.assert_not_called()
        for record in records[:3]:
            saved = self.saved(record)
            self.assertEqual(saved['document_source_error']['http_status'],503)
            self.assertNotIn('document_retry',saved)
            self.assertEqual(saved['source_fetched_at'],record['source_fetched_at'])
        self.assertNotIn('document_source_error',self.saved(records[3]))
        status = collector.read(self.folder/'collector.json',{})
        self.assertEqual(status['status'],'failed')
        self.assertEqual(status['source_health'],{'status':'unavailable','stage':'http','http_status':503})
        self.assertEqual(status['document_batch']['source_failures'],3)
        self.assertEqual(status['eligible_documents'],4)
        self.assertEqual(status['deferred_documents'],0)
        for key,value in state.items():
            self.assertEqual(collector.read(self.folder/'state.json',{})[key],value)

    async def test_single_all_transport_failure_batch_remains_retryable_then_recovers(self):
        atomic_json(self.folder/'state.json',{'page':345,'complete':False})
        collector.save_listing(self.folder,RECORD)
        portal = self.portal(mock.Mock(side_effect=AssertionError('No listing searches allowed')))
        portal.download = mock.AsyncMock(side_effect=PortalError('Cloud source request failed at transport stage.'))
        with mock.patch.object(collector,'GemPortal',return_value=portal):
            self.assertEqual(await collector.collect_documents(self.folder,seconds=30),1)
        failed_status = collector.read(self.folder/'collector.json',{})
        self.assertEqual(failed_status['eligible_documents'],1)
        self.assertEqual(failed_status['source_health'],{'status':'unavailable','stage':'connect'})
        self.assertNotIn('document_retry',self.saved())
        portal.download = mock.AsyncMock(return_value=b'%PDF-offline fixture')
        details = {'source_document_fetched_at':collector.stamp(),'detail_status':'parsed'}
        temporary = tempfile.TemporaryDirectory
        with mock.patch.object(collector,'GemPortal',return_value=portal), \
                mock.patch.object(collector,'parse_pdf',return_value=details), \
                mock.patch.object(collector.tempfile,'TemporaryDirectory',side_effect=lambda:temporary(dir=self.folder)):
            self.assertEqual(await collector.collect_documents(self.folder,seconds=30),0)
        recovered = collector.read(self.folder/'collector.json',{})
        self.assertEqual(recovered['source_health']['status'],'reachable')
        self.assertEqual(recovered['parsed_documents'],1)
        self.assertEqual(recovered['eligible_documents'],0)
        self.assertEqual(recovered['page'],345)
        self.assertNotIn('document_source_error',self.saved())

    async def test_missing_or_malformed_notices_remain_individual_retries(self):
        records = [self.bid(1000000+number,None) for number in range(1,5)]
        errors = [PortalHTTPError(404),PortalError('Cloud source request failed with HTTP 404.'),
            PortalError('Cloud source did not return an original PDF document.'),ValueError('Unreadable PDF')]
        portal = mock.Mock(download=mock.AsyncMock(side_effect=errors))
        summary = await collector.process_documents(self.folder,portal,records,seconds=30)
        self.assertEqual(summary['source_failures'],0)
        self.assertEqual(summary['failed'],4)
        for record in records:
            saved = self.saved(record)
            self.assertEqual(saved['document_retry']['attempts'],1)
            self.assertNotIn('document_source_error',saved)

    async def test_short_all_http_500_batch_fails_at_end_instead_of_reporting_success(self):
        portal = mock.Mock(download=mock.AsyncMock(
            side_effect=PortalError('Cloud source request failed with HTTP 500.')))
        with self.assertRaises(PortalError) as failure:
            await collector.process_documents(self.folder,portal,[RECORD],seconds=30)
        self.assertEqual(portal.download.await_count,1)
        self.assertEqual(failure.exception.document_batch['source_failures'],1)
        self.assertEqual(failure.exception.source_health['http_status'],500)
        self.assertEqual(self.saved()['document_source_error']['http_status'],500)
        self.assertTrue(collector.document_due(self.saved(),Clock.current))

    async def test_downloaded_pdf_resets_source_failure_streak_even_if_unreadable(self):
        records = [self.bid(1000000+number,None) for number in range(1,6)]
        source_error = PortalError('Cloud source request failed with HTTP 503.')
        portal = mock.Mock(download=mock.AsyncMock(side_effect=[source_error,source_error,
            b'%PDF-offline fixture',source_error,source_error]))
        temporary = tempfile.TemporaryDirectory
        with mock.patch.object(collector,'parse_pdf',side_effect=ValueError('Unreadable PDF')), \
                mock.patch.object(collector.tempfile,'TemporaryDirectory',side_effect=lambda:temporary(dir=self.folder)):
            summary = await collector.process_documents(self.folder,portal,records,seconds=30)
        self.assertEqual(summary['source_failures'],4)
        self.assertEqual(summary['downloaded'],1)
        self.assertEqual(portal.download.await_count,5)
        self.assertEqual(self.saved(records[2])['document_retry']['attempts'],1)

    def test_transient_direct_http_statuses_are_classified_without_escalating_404(self):
        for code in (429,500,502,503,504):
            self.assertEqual(collector.document_source_failure(PortalHTTPError(code))['http_status'],code)
        self.assertIsNone(collector.document_source_failure(PortalHTTPError(404)))
        self.assertIsNone(collector.document_source_failure(PortalChallengeError('Access restricted')))

    async def test_source_retry_batches_sample_fresh_documents_then_success_clears_history(self):
        state = {'page':345,'complete':False,'newest_refresh_pages':70,'newest_refresh_complete':True}
        atomic_json(self.folder/'state.json',state)
        records = [self.bid(1000000+number,None) for number in range(1,7)]
        for record in records:
            collector.save_listing(self.folder,record)
        portal = self.portal(mock.Mock(side_effect=AssertionError('No listing searches allowed')))
        portal.download = mock.AsyncMock(side_effect=PortalError('Cloud source request failed with HTTP 503.'))
        with mock.patch.object(collector,'GemPortal',return_value=portal):
            self.assertEqual(await collector.collect_documents(self.folder,seconds=30),1)
            self.assertEqual([call.args[0] for call in portal.download.await_args_list],
                [record['source_document_url'] for record in records[:3]])
            portal.download.reset_mock()
            Clock.current += dt.timedelta(minutes=5)
            self.assertEqual(await collector.collect_documents(self.folder,seconds=30),1)
        self.assertEqual([call.args[0] for call in portal.download.await_args_list],
            [record['source_document_url'] for record in records[3:]])
        for record in records:
            saved = self.saved(record)
            self.assertEqual(saved['document_source_error']['attempts'],1)
            self.assertNotIn('document_retry',saved)
        status = collector.read(self.folder/'collector.json',{})
        self.assertEqual((status['eligible_documents'],status['deferred_documents'],status['exhausted_documents']),(6,0,0))

        elapsed = [0]

        async def download_one(url):
            elapsed[0] += 2
            return b'%PDF-offline fixture'

        portal.download = mock.AsyncMock(side_effect=download_one)
        temporary = tempfile.TemporaryDirectory
        details = {'source_document_fetched_at':collector.stamp(),'detail_status':'parsed'}
        with mock.patch.object(collector,'GemPortal',return_value=portal), \
                mock.patch.object(collector,'parse_pdf',return_value=details), \
                mock.patch.object(collector,'time',mock.Mock(monotonic=lambda:elapsed[0])), \
                mock.patch.object(collector.tempfile,'TemporaryDirectory',side_effect=lambda:temporary(dir=self.folder)):
            self.assertEqual(await collector.collect_documents(self.folder,seconds=1),0)
        # The first group was last attempted earlier; retry it before the later
        # group once every eligible document has had an initial chance.
        portal.download.assert_awaited_once_with(records[0]['source_document_url'])
        self.assertNotIn('document_source_error',self.saved(records[0]))
        self.assertEqual(self.saved(records[0])['source_fetched_at'],records[0]['source_fetched_at'])
        status = collector.read(self.folder/'collector.json',{})
        self.assertEqual((status['documents'],status['parsed_documents'],status['eligible_documents']),(1,1,5))
        self.assertEqual(status['page'],state['page'])
        portal.search.assert_not_awaited()

    def test_source_retry_priority_handles_legacy_markers_and_attempt_history(self):
        legacy = {**RECORD,'published_at':'2026-10-27T09:59:00+00:00',
            'document_source_error':{'status':'unavailable','last_attempt_at':'2026-10-27T09:50:00+00:00'}}
        earlier = {**legacy,'document_source_error':{'attempts':1,'last_attempt_at':'2026-10-27T09:40:00+00:00'}}
        repeated = {**legacy,'document_source_error':{'attempts':2,'last_attempt_at':'2026-10-27T08:00:00+00:00'}}
        self.assertLess(collector.document_priority(RECORD,Clock.current),collector.document_priority(legacy,Clock.current))
        self.assertLess(collector.document_priority(earlier,Clock.current),collector.document_priority(legacy,Clock.current))
        self.assertLess(collector.document_priority(legacy,Clock.current),collector.document_priority(repeated,Clock.current))
        self.assertTrue(collector.document_due(repeated,Clock.current))

    async def test_legacy_source_error_counter_increments_without_using_parse_retry_cap(self):
        legacy = {**RECORD,'document_source_error':{'status':'unavailable',
            'last_attempt_at':'2026-10-27T09:00:00+00:00'}}
        portal = mock.Mock(download=mock.AsyncMock(
            side_effect=PortalError('Cloud source request failed with HTTP 503.')))
        with self.assertRaises(PortalError):
            await collector.process_documents(self.folder,portal,[legacy],seconds=30)
        saved = self.saved()
        self.assertEqual(saved['document_source_error']['attempts'],2)
        self.assertNotIn('document_retry',saved)
        self.assertTrue(collector.document_due(saved,Clock.current))

    def test_listing_refresh_retains_transient_source_history_for_same_document(self):
        history = {'attempts':2,'status':'unavailable','http_status':503,
            'last_attempt_at':'2026-10-27T09:50:00+00:00'}
        collector.save_listing(self.folder,{**RECORD,'document_source_error':history})
        collector.save_listing(self.folder,{**RECORD,'source_fetched_at':'2026-10-27T10:00:00+00:00'})
        self.assertEqual(self.saved()['document_source_error'],history)
        self.assertTrue(collector.document_due(self.saved(),Clock.current))

    async def test_failed_live_documents_do_not_block_untouched_expired_documents(self):
        live_retries = [{**self.bid(1000000+number,'2026-10-27T09:59:00+00:00'),
            'document_source_error':{'attempts':1,'http_status':503,
                'last_attempt_at':'2026-10-27T09:59:00+00:00'}} for number in range(1,4)]
        expired = {**self.bid(2000000,'2026-10-25T09:00:00+00:00'),
            'closing_at':'2026-10-26T09:00:00+00:00'}
        portal = mock.Mock(download=mock.AsyncMock(side_effect=ValueError('Offline unreadable PDF')))
        await collector.process_documents(self.folder,portal,[*live_retries,expired],seconds=30)
        self.assertEqual(portal.download.await_args_list[0].args[0],expired['source_document_url'])
        self.assertEqual(portal.download.await_count,4)
        self.assertEqual(self.saved(expired)['document_retry']['attempts'],1)

    def test_only_old_unrecognized_ra_receipts_become_reparse_jobs(self):
        stale = self.stale_ra()
        self.assertTrue(collector.document_due(stale,Clock.current))
        self.assertTrue(collector.document_due({**stale,'document_parser_version':1},Clock.current))
        for excluded in (
                {**stale,'document_parser_version':collector.DOCUMENT_PARSER_VERSION},
                {**stale,'document_parser_version':collector.DOCUMENT_PARSER_VERSION+1},
                {**stale,'detail_status':'parsed'},
                {**stale,'detail_status':'no_readable_text'},
                {**stale,'tender_id':'GEM/2026/B/1234567'},
                {**stale,'tender_id':'Unidentified reverse auction'}):
            with self.subTest(record=excluded):
                self.assertFalse(collector.document_needs_processing(excluded))
                self.assertFalse(collector.document_due(excluded,Clock.current))
        self.assertTrue(collector.document_due(RECORD,Clock.current))

    def test_reparse_queue_counts_keep_receipts_separate_from_pending_jobs(self):
        parsed = {**self.bid(1000001,None),'source_document_fetched_at':collector.stamp(),'detail_status':'parsed'}
        unreadable = {**self.bid(1000002,None),'source_document_fetched_at':collector.stamp(),'detail_status':'no_readable_text'}
        unsupported = {**self.bid(1000003,None),'source_document_fetched_at':collector.stamp(),'detail_status':'unrecognized_document'}
        due = self.stale_ra(1000004)
        deferred = {**self.stale_ra(1000005),'document_retry':{
            'attempts':1,'next_attempt_at':'2026-10-28T10:00:00+00:00'}}
        exhausted = {**self.stale_ra(1000006),'document_retry':{'attempts':3,'status':'exhausted'}}
        never_downloaded = self.bid(1000007,None)
        collector.publish_index(self.folder,{},[parsed,unreadable,unsupported,due,deferred,exhausted,never_downloaded])
        status = collector.read(self.folder/'collector.json',{})
        self.assertEqual((status['documents'],status['downloaded_documents'],status['parsed_documents']),(6,6,1))
        self.assertEqual((status['pending_documents'],status['unparsed_documents']),(1,5))
        self.assertEqual((status['reparse_documents'],status['pending_document_jobs']),(3,4))
        self.assertEqual((status['eligible_documents'],status['deferred_documents'],status['exhausted_documents']),(2,1,1))
        self.assertEqual(status['pending_document_jobs'],sum(status[key] for key in
            ('eligible_documents','deferred_documents','exhausted_documents')))

    async def test_successful_ra_reparse_preserves_listing_receipt_without_inflating_download_count(self):
        record = {**self.stale_ra(),'document_retry':{'attempts':1},
            'document_source_error':{'attempts':1,'http_status':503}}
        collector.save_listing(self.folder,record)
        atomic_json(self.folder/'state.json',{'page':345,'complete':False,'last_success_at':record['source_fetched_at']})
        portal = self.portal(mock.Mock(side_effect=AssertionError('No listing searches allowed')))
        portal.download = mock.AsyncMock(return_value=b'%PDF-offline fixture')
        details = {'source_document_fetched_at':collector.stamp(),'detail_status':'parsed',
            'document_parser_version':collector.DOCUMENT_PARSER_VERSION,'emd_fee':2500}
        temporary = tempfile.TemporaryDirectory
        with mock.patch.object(collector,'GemPortal',return_value=portal), \
                mock.patch.object(collector,'parse_pdf',return_value=details), \
                mock.patch.object(collector.tempfile,'TemporaryDirectory',side_effect=lambda:temporary(dir=self.folder)):
            self.assertEqual(await collector.collect_documents(self.folder,seconds=30),0)
        saved = self.saved(record)
        self.assertEqual(saved['source_fetched_at'],record['source_fetched_at'])
        self.assertEqual(saved['closing_at'],record['closing_at'])
        self.assertEqual(saved['document_parser_version'],collector.DOCUMENT_PARSER_VERSION)
        self.assertEqual(saved['emd_fee'],2500)
        self.assertNotIn('document_retry',saved)
        self.assertNotIn('document_source_error',saved)
        self.assertFalse(collector.document_due(saved,Clock.current))
        status = collector.read(self.folder/'collector.json',{})
        self.assertEqual((status['documents'],status['downloaded_documents'],status['parsed_documents']),(1,1,1))
        self.assertEqual((status['pending_documents'],status['reparse_documents'],status['pending_document_jobs']),(0,0,0))
        self.assertEqual(status['fetched_at'],record['source_fetched_at'])
        self.assertEqual(status['page'],345)
        portal.search.assert_not_awaited()

    async def test_current_parser_unrecognized_ra_is_not_downloaded_again(self):
        record = self.stale_ra()
        collector.save_listing(self.folder,record)
        portal = mock.Mock(download=mock.AsyncMock(return_value=b'%PDF-offline fixture'))
        details = {'source_document_fetched_at':collector.stamp(),'detail_status':'unrecognized_document',
            'document_parser_version':collector.DOCUMENT_PARSER_VERSION}
        temporary = tempfile.TemporaryDirectory
        with mock.patch.object(collector,'parse_pdf',return_value=details), \
                mock.patch.object(collector.tempfile,'TemporaryDirectory',side_effect=lambda:temporary(dir=self.folder)):
            await collector.process_documents(self.folder,portal,[record],seconds=30)
            await collector.process_documents(self.folder,portal,[self.saved(record)],seconds=30)
        portal.download.assert_awaited_once_with(record['source_document_url'])
        self.assertFalse(collector.document_due(self.saved(record),Clock.current))
        self.assertNotIn('emd_fee', {key:value for key,value in self.saved(record).items() if value is not None})
        collector.publish_index(self.folder,{})
        status = collector.read(self.folder/'collector.json',{})
        self.assertEqual((status['documents'],status['parsed_documents'],status['unparsed_documents']),(1,0,1))
        self.assertEqual((status['reparse_documents'],status['pending_document_jobs']),(0,0))

    async def test_ra_reparse_transport_failure_keeps_old_receipt_and_remains_retryable(self):
        record = self.stale_ra()
        collector.save_listing(self.folder,record)
        portal = mock.Mock(download=mock.AsyncMock(side_effect=PortalError('Cloud source request failed with HTTP 503.')))
        with self.assertRaises(PortalError):
            await collector.process_documents(self.folder,portal,[record],seconds=30)
        saved = self.saved(record)
        self.assertEqual(saved['source_document_fetched_at'],record['source_document_fetched_at'])
        self.assertEqual(saved['source_fetched_at'],record['source_fetched_at'])
        self.assertEqual(saved['detail_status'],'unrecognized_document')
        self.assertEqual(saved['document_source_error']['attempts'],1)
        self.assertNotIn('document_retry',saved)
        self.assertTrue(collector.document_due(saved,Clock.current))
        collector.publish_index(self.folder,{})
        status = collector.read(self.folder/'collector.json',{})
        self.assertEqual((status['documents'],status['pending_documents'],status['reparse_documents']),(1,0,1))
        self.assertEqual((status['pending_document_jobs'],status['eligible_documents'],status['deferred_documents']),(1,1,0))

    async def test_ra_reparse_missing_document_retains_backoff_and_attempt_cap(self):
        record = self.stale_ra()
        portal = mock.Mock(download=mock.AsyncMock(side_effect=PortalError('Cloud source request failed with HTTP 404.')))
        await collector.process_documents(self.folder,portal,[record],seconds=30)
        saved = self.saved(record)
        self.assertEqual(saved['source_document_fetched_at'],record['source_document_fetched_at'])
        self.assertEqual(saved['document_retry']['attempts'],1)
        self.assertFalse(collector.document_due(saved,Clock.current))
        for attempt in (2,3):
            Clock.current += dt.timedelta(days=1)
            self.assertTrue(collector.document_due(saved,Clock.current))
            await collector.process_documents(self.folder,portal,[saved],seconds=30)
            saved = self.saved(record)
            self.assertEqual(saved['document_retry']['attempts'],attempt)
        Clock.current += dt.timedelta(days=1)
        self.assertFalse(collector.document_due(saved,Clock.current))
        await collector.process_documents(self.folder,portal,[saved],seconds=30)
        self.assertEqual(portal.download.await_count,3)
        collector.publish_index(self.folder,{})
        status = collector.read(self.folder/'collector.json',{})
        self.assertEqual((status['reparse_documents'],status['pending_document_jobs'],status['exhausted_documents']),(1,1,1))
        self.assertEqual((status['eligible_documents'],status['deferred_documents']),(0,0))

    async def test_ra_migrations_precede_new_downloads_within_attempt_tier_but_failures_do_not_pin_queue(self):
        stale = {**self.stale_ra(1000001),'closing_at':'2026-10-26T09:00:00+00:00'}
        new = self.bid(1000002,'2026-10-27T09:59:00+00:00')
        failed_stale = {**self.stale_ra(1000003),'document_source_error':{
            'attempts':1,'http_status':503,'last_attempt_at':'2026-10-27T09:00:00+00:00'}}
        failed_new = {**self.bid(1000004,None),'document_retry':{
            'attempts':1,'next_attempt_at':'2026-10-27T09:00:00+00:00'}}
        unsupported = {**self.bid(1000005,None),'source_document_fetched_at':collector.stamp(),
            'detail_status':'unrecognized_document'}
        portal = mock.Mock(download=mock.AsyncMock(side_effect=ValueError('Offline malformed PDF')))
        await collector.process_documents(self.folder,portal,[new,failed_new,unsupported,failed_stale,stale],seconds=30)
        self.assertEqual([call.args[0] for call in portal.download.await_args_list],
            [record['source_document_url'] for record in (stale,new,failed_stale,failed_new)])
        self.assertEqual(self.saved(unsupported),{})

    async def test_ra_reparse_challenge_keeps_receipt_and_stops_without_job_retry(self):
        record = self.stale_ra()
        collector.save_listing(self.folder,record)
        atomic_json(self.folder/'state.json',{'page':345,'complete':False})
        portal = self.portal(mock.Mock(side_effect=AssertionError('No listing searches allowed')))
        portal.download = mock.AsyncMock(side_effect=PortalChallengeError('Access restricted'))
        with mock.patch.object(collector,'GemPortal',return_value=portal):
            self.assertEqual(await collector.collect_documents(self.folder,seconds=30),1)
        portal.download.assert_awaited_once()
        self.assertEqual(self.saved(record),record)
        self.assertEqual(collector.read(self.folder/'state.json',{})['status'],'blocked')

    def test_listing_refresh_does_not_reset_completed_ra_parser_migration(self):
        record = {**self.stale_ra(),'document_parser_version':collector.DOCUMENT_PARSER_VERSION}
        collector.save_listing(self.folder,record)
        listing = {key:value for key,value in record.items() if key not in
            ('source_document_fetched_at','detail_status','document_parser_version')}
        collector.save_listing(self.folder,{**listing,'source_fetched_at':collector.stamp()})
        saved = self.saved(record)
        self.assertEqual(saved['document_parser_version'],collector.DOCUMENT_PARSER_VERSION)
        self.assertEqual(saved['source_document_fetched_at'],record['source_document_fetched_at'])
        self.assertFalse(collector.document_due(saved,Clock.current))

    def test_listing_refresh_unions_official_pdf_links_preserving_precise_labels_and_evidence(self):
        record = {**self.stale_ra(),'detail_status':'parsed',
            'document_parser_version':collector.DOCUMENT_PARSER_VERSION,
            'ra_start_at':'2026-10-28T09:00:00+05:30',
            'evidence':[{'field':'ra_start_at','page':1,'quote':'RA start date: 28-10-2026'}]}
        original = {'name':'Original reverse auction document','url':record['source_document_url'],'page':None}
        parent = {'name':'Originating bid document GEM/2026/B/7654321',
            'url':'https://bidplus.gem.gov.in/showbidDocument/7654321','page':1,'reference':'GEM/2026/B/7654321'}
        extra = {'name':'Buyer specification','url':'https://mkp.gem.gov.in/specifications/9876543.pdf'}
        unsafe = [
            {'name':'Foreign','url':'https://gem.gov.in.example.com/foreign.pdf'},
            {'name':'Insecure','url':'http://bidplus.gem.gov.in/showbidDocument/7654321'},
            {'name':'Credentials','url':'https://user:secret@bidplus.gem.gov.in/showbidDocument/7654321'},
            {'name':'Invalid port','url':'https://bidplus.gem.gov.in:8443/notice.pdf'},
            {'name':'Missing URL'},{'name':'Invalid URL type','url':{'host':'gem.gov.in'}},'Not a document']
        collector.save_listing(self.folder,{**record,'documents':[original,parent,*unsafe]})
        listing = {key:value for key,value in record.items() if key not in
            ('source_document_fetched_at','detail_status','document_parser_version','ra_start_at','evidence')}
        collector.save_listing(self.folder,{**listing,'source_fetched_at':collector.stamp(),
            'documents':[{'name':'Source document','url':original['url']},
                {'name':'Document','url':parent['url']},extra,*unsafe]})
        saved = self.saved(record)
        self.assertEqual(saved['documents'],[original,parent,extra])
        for key in ('document_parser_version','evidence','ra_start_at','source_document_fetched_at'):
            self.assertEqual(saved[key],record[key])
        self.assertEqual(saved['source_fetched_at'],collector.stamp())

    def test_changed_source_or_reference_does_not_inherit_prior_pdf_document_links(self):
        record = {**self.stale_ra(),'detail_status':'parsed',
            'document_parser_version':collector.DOCUMENT_PARSER_VERSION,
            'documents':[{'name':'Originating bid','url':'https://bidplus.gem.gov.in/showbidDocument/7654321'}]}
        replacements = [
            {'source_document_url':'https://bidplus.gem.gov.in/showdirectradocumentPdf/1234567'},
            {'tender_id':'GEM/2026/R/7654321'}]
        for replacement in replacements:
            with self.subTest(replacement=replacement):
                atomic_json(self.folder/'records'/(record['id']+'.json'),record)
                listing = {key:value for key,value in record.items() if key not in
                    ('source_document_fetched_at','detail_status','document_parser_version','documents')}
                listing.update(replacement,source_fetched_at=collector.stamp())
                listing['documents'] = [{'name':'Current original document','url':listing['source_document_url']}]
                collector.save_listing(self.folder,listing)
                saved = self.saved(record)
                self.assertEqual(saved,listing)
                self.assertNotIn('document_parser_version',saved)


if __name__ == '__main__':
    unittest.main()
