# scripts/maintenance/

One-off repair and migration tools for **existing** databases, plus a live
Apify smoke test. Not needed for a fresh install or the demo; each prints
what it would do first (dry run) and is covered by the test suite.

| Script | When to run it |
| --- | --- |
| `repair_url_identity.py` | After an upgrade that bumps `URL_KEY_VERSION` (see `DATA.md`): merges rows of one page that were stored under different URL keys. |
| `repair_human_verdicts.py` | After `repair_url_identity.py`: re-applies human review verdicts to every stored copy of a page. |
| `split_subjects.py` | Once, on a database from before projects/subjects existed, to split searches for several names out of subject 1. |
| `smoke_apify.py` | Manually, with your own `APIFY_API_KEY`: one real actor run (costs Apify credits; never run by CI). |

Back up the database (Settings → Backup) before running any of them with
`--apply` / without `--dry-run`.
