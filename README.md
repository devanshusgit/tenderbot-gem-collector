# TenderBot public GeM collector

Collects real public ongoing GeM bids using an anonymous Playwright session. Standard GitHub-hosted Linux Actions run every30minutes (best-effort scheduling). Data and checkpoints live on the data branch; no Actions artifacts, caches, larger runners or external paid resources are used.

Only collector code and public-source records are published. The TenderBot website remains private. No keys or user credentials are required by this workflow. Original PDFs remain linked to GeM; extracted original-PDF evidence is stored as it is processed. Missing fields stay unknown. Full-feed traversal and PDF extraction are separate resumable tasks. Source access challenges stop collection.

The records are collected observations, not official GeM advice or a guarantee of full current coverage. Record timestamps and full-sweep status are provided. The dataset is not an awards feed. Scheduled workflows can be delayed or disabled by GitHub; monitor their run status.
