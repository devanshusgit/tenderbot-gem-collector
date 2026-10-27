# TenderBot public GeM collector

Collects real public ongoing GeM bids with Python Playwright. Standard public GitHub-hosted Ubuntu Actions are scheduled at17 and47 minutes each hour; GitHub may delay runs. Jobs and data publication continue independently of the user's computer. No paid runners, artifacts, caches or external paid resources are used.

The data branch stores records, parsed original-PDF evidence and resumable checkpoints. The private website reads updated data directly. Original PDF binaries remain on official GeM; temporary downloads are discarded after parsing. No user credentials are needed by this collector. Fields not obtained from the source remain unknown.

A bounded official HTTPS check runs before browser installation. Connection failures preserve records, source dates and checkpoints and publish source_health diagnostics. An HTTP server error may still proceed to browser validation; access challenges stop collection. Do not bypass protections, rotate IPs or fabricate successful refreshes.

Recent-bid refreshes use a one-hour overlap and resumable cursor instead of a30-record cap. Each healthy run also advances the full ongoing-feed sweep and a bounded PDF queue. Failed PDFs have backoff and an attempt limit to prevent starvation. Existing parsed locations survive listing-only refreshes for the same document.

Traversal and exact current coverage are different: the source changes while pages are read. See collector.json for actual success/failure, source timestamps, checkpoints and pending documents. No complete coverage or award/gated-data feed is claimed. At the latest verification the imported dataset contained18,236 records, while GitHub requests to the official source were refused at the TCP connection stage.

Offline checks: python -m unittest test_collector test_source_health (requires the pinned PDF reader dependency used by the workflow).

Cloudflare source support is prepared: GEM_CLOUDFLARE_SOURCE_URL selects the private source API and TENDERBOT_SITE_SERVICE_TOKEN is the encrypted Site service credential. This credential grants access to the private Site. Activation requires the owner's explicit approval; no credential has been exported. GitHub remains the free scheduler, data store and PDF parser. Catch-up can run up to24 bounded batches and publishes progress after each one. The source API successfully fetched current GeM listings from Cloudflare; full coverage is not yet verified.
