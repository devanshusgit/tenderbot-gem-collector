# TenderBot public GeM collector

The existing owner-private TenderBot Cloudflare Worker retrieves public GeM listings and official PDFs. Standard public Ubuntu GitHub Actions schedule Python collection, parse PDFs and publish resumable checkpoints to the data branch of https://github.com/devanshusgit/tenderbot-gem-collector. The website reads that branch automatically, independently of the user's computer. Scheduling and the short website cache are best effort.

The human approved encrypted repository secret TENDERBOT_SITE_SERVICE_TOKEN. GEM_CLOUDFLARE_SOURCE_URL selects the exact private Site source API. Preserve the secret and the owner-private audience; never log the token, move it elsewhere or request approval again.

The first full ongoing-feed page traversal was verified and notified for run40488548918/data commit94da1be12018b8158ab075bbf05ee7a74bdb29b6 on27October2026:4,223pages and42,228source-reported bids/RA. A changing feed can overlap or move between pages. Exact current coverage and completion of the PDF queue remain unverified. Read collector.json on the data branch and the latest Actions runs for current observations.

The normal17/47-minute schedule refreshes recent bids, resumes the full ongoing-feed sweep and processes a short PDF batch. A separate six-hour schedule processes the existing PDF queue for up to12bounded600-second batches, without changing listing cursors or receipt timestamps. The shared concurrency lock serializes collectors; listing schedules can wait behind a document job. Manual documents mode can continue for up to24bounded batches. Current and unknown-deadline bids are prioritized before expired notices; untouched PDFs precede individual retries within those groups.

collector.json distinguishes downloaded_documents, parsed_documents, eligible_documents, deferred_documents and exhausted_documents. The older documents field remains the number of PDF receipts for compatibility. Downloading a PDF does not prove that its text was parsed successfully. Missing source fields remain unknown, and original PDF binaries are not retained in GitHub.

runner.sh publishes checkpoints after every attempt. Transient source failures receive3then6minute backoff and stop after3consecutive failures. Access verification and authorization restrictions stop immediately. The5h15m work budget reserves publication time before the6h runner limit. Documents mode stops when no document jobs are due now, even if deferred/exhausted jobs remain. Listing traversal completion does not stop a documents-only batch.

Use only the included existing Site hosting and standard public Ubuntu Actions: no paid resources, larger runners, Actions artifacts/cache or local crawlers. The Supabase, Vercel and Railway files are historical alternatives, not active services.
