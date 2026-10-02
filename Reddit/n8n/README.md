# n8n version

A workflow named **Reddit AI Leads Fetcher** does the same fetching and
filtering inside n8n. Build it from the structure below, or export an existing
one from your own n8n instance (workflow menu **⋯ → Download**) and save it here
as `reddit-ai-leads.json` to keep a copy in the repo.

## Structure

Four nodes, no credentials needed — every endpoint is public:

| Node | Type | What it does |
| --- | --- | --- |
| `Start` | Manual Trigger | swap for a Schedule Trigger to run it daily |
| `Build Reddit URLs` | Code | builds 25 public Reddit `.json` search URLs |
| `Fetch Reddit JSON` | HTTP Request | fetches them, 1 at a time, 1.2s apart, `neverError` on |
| `Filter Candidate Leads` | Code | keeps recent, AI-related, remote, hiring posts |

The Code nodes have no network access in n8n — that is why fetching is a
separate HTTP Request node rather than something the Code node does itself.

## Rules

`Filter Candidate Leads` applies the same rules as the Python version: 14-day
window, AI keywords with word-boundary matching, hiring intent required,
`[FOR HIRE]` posts dropped, negation-aware onsite exclusion, and explicit
remote evidence (or a remote-only subreddit). It emits up to 90 candidates.

For the full scoring, ranking and four-key deduplication, send its output
onward, or save it and run:

```bash
python3 -m reddit_leads --from-json candidates.json
```

## Note

n8n **will not execute any workflow while the account's trial or plan is
inactive** — runs fail immediately with "Your trial has ended". The workflow
itself is saved and valid; it runs as soon as the account is on an active plan.
Until then, use the Python CLI or the Google Apps Script version, neither of
which depends on n8n.
