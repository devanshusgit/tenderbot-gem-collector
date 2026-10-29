# TenderBot public GeM collector

Collects real public ongoing GeM bids with Python Playwright. Standard public GitHub-hosted Ubuntu Actions are scheduled at17 and47 minutes each hour; GitHub may delay runs. Jobs and data publication continue independently of the user's computer. No paid runners, artifacts, caches or external paid resources are used.

The data branch stores records, parsed original-PDF evidence and resumable checkpoints. The private website reads updated data directly. Original PDF binaries remain on official GeM; temporary downloads are discarded after parsing. No user credentials are needed by this collector. Fields not obtained from the source remain unknown.

A bounded official HTTPS check runs before browser installation. Connection failures preserve records, source dates and checkpoints and publish source_health diagnostics. An HTTP server error may still proceed to browser validation; access challenges stop collection. Do not bypass protections, rotate IPs or fabricate successful refreshes.

Recent-bid refreshes use a one-hour overlap and resumable cursor instead of a30-record cap. Each healthy run also advances the full ongoing-feed sweep and a bounded PDF queue. Failed PDFs have backoff and an attempt limit to prevent starvation. Existing parsed locations survive listing-only refreshes for the same document.

Traversal and exact current coverage are different: the source changes while pages are read. See collector.json for actual success/failure, source timestamps, checkpoints and pending documents. No complete coverage or award/gated-data feed is claimed. Initial direct requests from GitHub were refused. Source retrieval now runs on Cloudflare through the private Site API; the GitHub job only schedules, parses and publishes data.

Offline checks: python -m unittest test_collector test_source_health (requires the pinned PDF reader dependency used by the workflow).

Cloudflare source support is prepared: GEM_CLOUDFLARE_SOURCE_URL selects the private source API and TENDERBOT_SITE_SERVICE_TOKEN is the encrypted Site service credential. This credential grants access to the private Site. The owner explicitly approved storing it as an encrypted Actions secret; its value is never public. GitHub remains the free scheduler, data store and PDF parser. Catch-up can run up to24 bounded batches and publishes progress after each one. The source API successfully fetched current GeM listings from Cloudflare; full coverage is not yet verified.

Cloud activation was verified by successful run40487867972 and matching website readback:18,255real records and32parsed PDFs. Full catch-up run40488548918 is running; complete source coverage is not yet verified.

The runner publishes a failed checkpoint before retrying transient collection failures after3and6minutes, with at most3consecutive attempts. Access-verification or authorization restrictions stop immediately. A job budget leaves time to publish before the six-hour runner limit; later schedules resume incomplete work. Offline runner tests exercise recovery, bounded persistent failure and immediate restriction stops.
