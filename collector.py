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

from tenderbot_crawler.gem import GemPortal, PortalChallengeError
from tenderbot_crawler.documents import parse_pdf
from tenderbot_crawler.storage import atomic_json, validate_record

FIELDS = ['id','tender_id','title','organisation','ministry','department','quantity',
          'closing_at','published_at','source_fetched_at','source_document_url',
          'product_category','status','state','city']


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def publish_index(folder, state):
    records = [read(path,{}) for path in sorted((folder/'records').glob('gem-*.json'))]
    rows = [[record.get(key) for key in FIELDS] for record in records]
    parsed = sum(bool(r.get('source_document_fetched_at')) for r in records)
    receipt = max((r['source_fetched_at'] for r in records),default=None)
    status = {**state,'enabled':True,'runtime':'github-actions','automatic':True,
        'records':len(records),'documents':parsed,'pending_documents':len(records)-parsed,
        'fetched_at':receipt,'last_run_at':state.get('last_run_at'),
        'full_sweep_complete':bool(state.get('complete')),
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
    merged = {**previous,**current}
    # A newly fetched listing does not erase already parsed PDF requirements.
    for key in ('emd_fee','tender_value','organisation','ministry','department'):
        if current.get(key) is None and previous.get(key) is not None:
            merged[key] = previous[key]
    atomic_json(path,merged)


async def collect(folder, seconds=600, pdf_seconds=100):
    folder.mkdir(parents=True,exist_ok=True)
    (folder/'records').mkdir(exist_ok=True)
    state = read(folder/'state.json',{})
    state.update(status='running',last_run_at=stamp(),error=None)
    started = time.monotonic()
    try:
        async with GemPortal(browser_channel='chromium',delay=1) as portal:
            for page in range(1,4):
                result = await portal.search(page=page,sort='Bid-Start-Date-Latest')
                for record in result['tenders']:
                    save_listing(folder,record)
            page = 1 if state.get('complete') else state.get('page',0)+1
            while time.monotonic()-started < seconds:
                result = await portal.search(page=page)
                for record in result['tenders']:
                    save_listing(folder,record)
                complete = not result['has_more'] or not result['tenders']
                state.update(page=page,source_total=result['total'],complete=complete)
                atomic_json(folder/'state.json',state)
                print(json.dumps({'page':page,'source_total':result['total']}),flush=True)
                if complete:
                    break
                page += 1
            records = publish_index(folder,state)
            pdf_start = time.monotonic()
            for record in records:
                if time.monotonic()-pdf_start >= pdf_seconds:
                    break
                if record.get('source_document_fetched_at'):
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
                    atomic_json(folder/'records'/(record['id']+'.json'),merged)
                except PortalChallengeError:
                    raise
                except Exception as error:
                    # Unreadable or non-PDF notices stay explicit, never invented.
                    print(json.dumps({'id':record['id'],'pdf_error':type(error).__name__}),flush=True)
            state.update(status='succeeded',last_success_at=stamp())
    except PortalChallengeError:
        state.update(status='blocked',error='GeM requires human access verification')
    except Exception as error:
        state.update(status='failed',error=type(error).__name__)
    finally:
        publish_index(folder,state)
    return 0 if state['status']=='succeeded' else 1


if __name__=='__main__':
    args = argparse.ArgumentParser()
    args.add_argument('--data-dir',type=Path,required=True)
    args.add_argument('--seconds',type=int,default=600)
    config=args.parse_args()
    raise SystemExit(asyncio.run(collect(config.data_dir,config.seconds)))
