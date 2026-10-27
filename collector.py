"""Public-source collector for free standard GitHub Actions runners.

No artifacts, caches, paid runners, cloud credentials or PDF binaries are stored.
The data branch keeps source records, parsed evidence and a resumable cursor.
"""
import argparse
import asyncio
import datetime as dt
import json
from pathlib import Path
import tempfile
import time
import urllib.request

from tenderbot_crawler.gem import GemPortal, PortalChallengeError, PortalError
from tenderbot_crawler.documents import parse_pdf
from tenderbot_crawler.storage import atomic_json, timestamp, validate_record

FIELDS = ['id','tender_id','title','organisation','ministry','department','quantity',
          'closing_at','published_at','source_fetched_at','source_document_url',
          'product_category','status','state','city']
DOCUMENT_RETRY_DELAYS = (3600, 21600)
MAX_DOCUMENT_ATTEMPTS = len(DOCUMENT_RETRY_DELAYS) + 1
NEWEST_BUDGET_FRACTION = 0.35
NEWEST_OVERLAP = dt.timedelta(hours=1)


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def publish_index(folder, state, records=None):
    if records is None:
        records = [read(path,{}) for path in sorted((folder/'records').glob('gem-*.json'))]
    rows = [[record.get(key) for key in FIELDS] for record in records]
    parsed = sum(bool(r.get('source_document_fetched_at')) for r in records)
    receipt = max((r['source_fetched_at'] for r in records),default=None)
    status = {**state,'enabled':True,'runtime':'github-actions','automatic':True,
        'records':len(records),'documents':parsed,'pending_documents':len(records)-parsed,
        'fetched_at':receipt,'last_run_at':state.get('last_run_at'),
        'full_sweep_complete':bool(state.get('complete')),
        'exact_coverage_verified':False,
        'coverage_note':'A traversal of the moving ongoing feed does not verify exact unique coverage.',
        'pages_collected':state.get('page',0),'errors':1 if state.get('error') else 0,
        'document_storage':'Original PDFs remain on official GeM; parsed evidence is retained.'}
    atomic_json(folder/'index.json',{'fields':FIELDS,'rows':rows,'fetched_at':receipt})
    atomic_json(folder/'collector.json',status)
    atomic_json(folder/'state.json',state)
    return records


def save_listing(folder, record):
    validate_record(record)
    path = folder/'records'/(record['id']+'.json')
    previous = read(path,{})
    # Raw index payload is retained in the local archive, not the public dataset.
    current = {key:value for key,value in record.items() if key!='source_data'}
    if previous.get('source_fetched_at') and timestamp(previous['source_fetched_at']) > timestamp(current['source_fetched_at']):
        return
    same_document = (previous.get('tender_id') == current['tender_id'] and
                     previous.get('source_document_url') == current['source_document_url'])
    # Evidence and exhausted retries belong to a particular bid/document URL.
    # A changed source must be processed anew, without inheriting old details.
    merged = {**previous,**current} if same_document else current
    # A newly fetched listing does not erase already parsed PDF requirements.
    for key in ('emd_fee','tender_value','organisation','ministry','department'):
        if same_document and current.get(key) is None and previous.get(key) is not None:
            merged[key] = previous[key]
    if same_document and previous.get('source_document_fetched_at'):
        for key in ('state','city'):
            if not current.get(key) and previous.get(key):
                merged[key] = previous[key]
    atomic_json(path,merged)


def document_due(record, now):
    if record.get('source_document_fetched_at'):
        return False
    retry = record.get('document_retry',{})
    if retry.get('attempts',0) >= MAX_DOCUMENT_ATTEMPTS:
        return False
    return not retry.get('next_attempt_at') or timestamp(retry['next_attempt_at']) <= now


async def process_documents(folder, portal, records, seconds):
    started = time.monotonic()
    # Try untouched documents before retrying failed ones. Backoff and the cap
    # survive job restarts, so one unreadable notice cannot consume every run.
    queue = sorted(records,key=lambda record:(record.get('document_retry',{}).get('attempts',0),record['id']))
    for record in queue:
        if time.monotonic()-started >= seconds:
            break
        if not document_due(record,dt.datetime.now(dt.timezone.utc)):
            continue
        try:
            content = await portal.download(record['source_document_url'])
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary)/'original.pdf'
                path.write_bytes(content)
                details = await asyncio.to_thread(parse_pdf,path,record,stamp())
            protected = {key:record.get(key) for key in
                ('id','tender_id','title','description','quantity','status','published_at','closing_at','source_fetched_at')}
            merged = {**record,**{k:v for k,v in details.items() if v is not None},**protected}
            merged.pop('document_retry',None)
            atomic_json(folder/'records'/(record['id']+'.json'),merged)
        except PortalChallengeError:
            raise
        except Exception as error:
            failed_at = dt.datetime.now(dt.timezone.utc)
            attempts = record.get('document_retry',{}).get('attempts',0)+1
            exhausted = attempts >= MAX_DOCUMENT_ATTEMPTS
            retry = {'attempts':attempts,'last_attempt_at':failed_at.isoformat(),
                'error':type(error).__name__,'status':'exhausted' if exhausted else 'retry',
                'next_attempt_at':None if exhausted else
                    (failed_at+dt.timedelta(seconds=DOCUMENT_RETRY_DELAYS[attempts-1])).isoformat()}
            atomic_json(folder/'records'/(record['id']+'.json'),{**record,'document_retry':retry})
            # Unreadable or non-PDF notices stay explicit, never invented.
            print(json.dumps({'id':record['id'],'pdf_error':type(error).__name__,
                'document_attempts':attempts,'document_retry_status':retry['status']}),flush=True)


def newest_cutoff(state, previous_status):
    watermark = state.get('newest_refresh_watermark')
    if not watermark and state.get('newest_refresh_complete') is False and 'newest_refresh_cutoff' in state:
        # A partial run must not move its cutoff forward using its own receipt.
        return timestamp(state['newest_refresh_cutoff']) if state['newest_refresh_cutoff'] else None
    watermark = (watermark or state.get('source_fetched_at') or state.get('fetched_at') or
                 previous_status.get('fetched_at') or state.get('last_success_at'))
    return timestamp(watermark)-NEWEST_OVERLAP if watermark else None


def checked_page(result, state):
    # search() already verified the anonymous form, HTTP response and schema.
    state.update(source_checked_at=stamp(),
        source_health={'status':'reachable','stage':'http','http_status':200})
    if not result['tenders'] and result['has_more']:
        raise PortalError('GeM returned an empty page before the reported end of the feed; checkpoint retained.')
    return result['tenders']


async def refresh_newest(folder, portal, state, seconds, cutoff):
    resuming = state.get('newest_refresh_complete') is False and state.get('newest_refresh_started_at')
    started_at = state['newest_refresh_started_at'] if resuming else stamp()
    # Replay the last page on resume to overlap a moving page boundary. Keep
    # the original pass watermark; bids arriving during this pass are covered
    # by the following pass instead of falsely advancing to its finish time.
    page = max(1,state.get('newest_refresh_pages',0)) if resuming else 1
    run_pages = 0
    deadline = time.monotonic()+seconds
    state.update(newest_refresh_complete=False,
        newest_refresh_pages=state.get('newest_refresh_pages',0) if resuming else 0,
        newest_refresh_run_pages=0,newest_refresh_run_started_at=stamp(),
        newest_refresh_cutoff=cutoff.isoformat() if cutoff else None,
        newest_refresh_started_at=started_at,newest_refresh_stop_reason='budget')
    atomic_json(folder/'state.json',state)
    try:
        # On resume one replay plus one new page guarantees cursor progress.
        # The normal job budget is ample; only a slow request may cross it.
        while time.monotonic() < deadline or (resuming and run_pages < 2):
            result = await portal.search(page=page,sort='Bid-Start-Date-Latest')
            records = checked_page(result,state)
            for record in records:
                save_listing(folder,record)
            # Unknown publication dates cannot establish that the cutoff was
            # reached. The source's newest sort orders the remaining pages.
            publication_dates = [record.get('published_at') for record in records]
            crossed_cutoff = bool(cutoff and records and all(publication_dates) and
                all(timestamp(value) <= cutoff for value in publication_dates))
            complete = not result['has_more'] or crossed_cutoff
            run_pages += 1
            state.update(newest_refresh_pages=page,newest_refresh_source_total=result['total'],
                newest_refresh_run_pages=run_pages,newest_refresh_complete=complete)
            if complete:
                state.update(newest_refresh_watermark=started_at,
                    newest_refresh_last_complete_at=stamp(),
                    newest_refresh_stop_reason='cutoff' if crossed_cutoff else 'end_of_feed')
            atomic_json(folder/'state.json',state)
            if complete:
                break
            page += 1
    except Exception:
        state['newest_refresh_stop_reason'] = 'error'
        raise
    finally:
        state['newest_refresh_finished_at'] = stamp()
        atomic_json(folder/'state.json',state)


async def collect(folder, seconds=600, pdf_seconds=100):
    folder.mkdir(parents=True,exist_ok=True)
    (folder/'records').mkdir(exist_ok=True)
    state = read(folder/'state.json',{})
    cutoff = newest_cutoff(state,read(folder/'collector.json',{}))
    state.update(status='running',last_run_at=stamp(),error=None)
    started = time.monotonic()
    try:
        async with GemPortal(browser_channel='chromium',delay=1) as portal:
            await refresh_newest(folder,portal,state,seconds*NEWEST_BUDGET_FRACTION,cutoff)
            page = 1 if state.get('complete') else state.get('page',0)+1
            full_pages = 0
            # Reserve a full-feed attempt even if a slow newest request crosses
            # its budget. Ordinary refresh work cannot starve the sweep cursor.
            while full_pages == 0 or time.monotonic()-started < seconds:
                result = await portal.search(page=page)
                for record in checked_page(result,state):
                    save_listing(folder,record)
                complete = not result['has_more']
                state.update(page=page,source_total=result['total'],complete=complete)
                full_pages += 1
                atomic_json(folder/'state.json',state)
                print(json.dumps({'page':page,'source_total':result['total']}),flush=True)
                if complete:
                    break
                page += 1
            records = publish_index(folder,state)
            await process_documents(folder,portal,records,pdf_seconds)
            state.update(status='succeeded',last_success_at=stamp())
            state.pop('error_message',None)
    except PortalChallengeError:
        state.update(status='blocked',error='GeM requires human access verification')
    except Exception as error:
        state.update(status='failed',error=type(error).__name__,error_message=str(error)[:250])
        print(json.dumps({'error':type(error).__name__,'message':str(error)[:250],
            'cause':str(error.__cause__)[:350] if error.__cause__ else None}),flush=True)
        # A single read-only transport check distinguishes browser failure from
        # source reachability. It never solves or bypasses access challenges.
        try:
            def probe():
                request=urllib.request.Request('https://bidplus.gem.gov.in/all-bids',method='HEAD')
                with urllib.request.urlopen(request,timeout=15) as response:
                    return response.status
            status=await asyncio.to_thread(probe)
            print(json.dumps({'public_https_probe_status':status}),flush=True)
        except Exception as probe_error:
            print(json.dumps({'public_https_probe_error':str(probe_error)[:250]}),flush=True)
    finally:
        publish_index(folder,state)
    return 0 if state['status']=='succeeded' else 1


if __name__=='__main__':
    args = argparse.ArgumentParser()
    args.add_argument('--data-dir',type=Path,required=True)
    args.add_argument('--seconds',type=int,default=600)
    args.add_argument('--pdf-seconds',type=int,default=100)
    config=args.parse_args()
    if not 1<=config.seconds<=900 or not 0<=config.pdf_seconds<=180:
        args.error('Collection limits must fit within one free scheduled job')
    raise SystemExit(asyncio.run(collect(config.data_dir,config.seconds,config.pdf_seconds)))
