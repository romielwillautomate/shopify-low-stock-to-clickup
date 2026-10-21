# Changelog

## v1.1 (2026-10-21)

Review fixes, tested end to end on n8n 2.19.5 (58 checks, 21 scenarios).

### Fixed

- **Duplicates from a second run or a person.** The open tasks are read at the start of a run, but a big catalog takes minutes to scan, so a Run Now during the scheduled run (or a person making the task by hand) could lead to two tasks for one SKU. Every create now has a look-up right before it: open tasks in the list whose SKU field is exactly that SKU (ClickUp's `custom_fields` filter with the exact match operator `==`). If one is found, nothing is created and the digest says so. If the look-up fails, the create is skipped for that run.
- **ClickUp 429 on the task read stopped the run.** The read only had the node's 3 quick tries, 2 seconds apart, which cannot outlast a per minute limit. It now waits until ClickUp's `X-RateLimit-Reset` time (at most 2 minutes) and reads again, up to 3 times, then stops with a clear message.
- **The task read could stop early.** It ended when `last_page` was anything but `false`, so a response without that field would have hidden every task after the first 100 and caused duplicate tasks. It now ends only when `last_page` is `true` or a page is empty.
- **Run Now said "started" for runs that never started.** It answered 202 before the settings were checked. It now answers after the check: 202, or 500 with the list of problems.
- **Alert wording.** A ClickUp 429 showed n8n's own hint about batching ("Try spacing your requests out..."), a ClickUp 401 on the read showed the raw body, and the Shopify ACCESS_DENIED text ended in a double period. They now read "the rate limit was still reached after 3 waits", "Token invalid (OAUTH_025)" and a single sentence.
- **Default scan filter** is now `product_status:ACTIVE`, written like Shopify's own example for this filter.

### Tests added or made stricter

- The THROTTLED wait is checked against the time the budget really needs (3 seconds), so a fixed 2 second wait would now fail the test.
- The whole run data is searched for secrets, not only the settings node, including the error items of failed calls.
- New scenarios: a task that appears in ClickUp while the run is going, ClickUp 429 on the read (a few times, and without end), ClickUp 429 on every change, Slack not set, and the 500 answer for a settings mistake.
- The mock ClickUp now supports and validates the `custom_fields` filter, sends `X-RateLimit-Reset` on read 429s, and the mock Shopify parses the `product_status` filter.

## v1 (2026-10-20)

First build, tested end to end on n8n 2.19.5 against mock Shopify, ClickUp and Slack APIs (46 checks, 16 scenarios).

### What it does

- A daily stock check on `STOCK_CHECK_CRON`, plus a Run Now webhook protected by the `X-Run-Now-Secret` header. With no secret set, Run Now refuses every call.
- Every setting is checked before any API is called, and a settings problem stops the run with a message that names each one.
- Reads all open tasks in the ClickUp reorder list (ClickUp API v2, every page) and matches them to SKUs through a SKU custom field or the task name.
- Pages through every product variant with the Shopify Admin GraphQL API (`productVariants`, cursor pagination, version `2026-10` by default). Untracked variants and variants without a SKU are skipped and counted. The same SKU on several variants is added up.
- Reorder point per variant from a metafield (`custom.reorder_point` by default), else `DEFAULT_REORDER_POINT`.
- Supports both kinds of Shopify apps: the client credentials grant for apps made in the Dev Dashboard (a fresh token on every run), or a stored Admin API token for older custom apps.
- One ClickUp task per low SKU, with stock numbers, supplier and email from `SUPPLIERS_JSON`, a Shopify link, a priority by severity, a due date from the supplier's lead time, assignees, a tag and the SKU field.
- A comment when stock drops below the number the team last saw, with the priority raised if needed (never lowered). The task is closed, then commented, when stock is back above the reorder point.
- One Slack digest per run: new, updated, closed, still low, needs a look, and failed changes.

### Built to fail safely

- Shopify `THROTTLED` answers wait until the query budget has refilled and ask for the same page again. When the budget left is too small for the next page, the scan pauses first. HTTP 429 backs off. All of this stops after 5 waits.
- ClickUp calls are made one at a time, so a retry never repeats a change that already worked. A 429 gets 3 quick tries, then a wait of `CLICKUP_RATE_LIMIT_WAIT_SECONDS` and the same step again.
- A failed ClickUp change does not stop the others. It is listed in the digest, retried on the next run, and the run ends as failed so the error workflow alerts.
- Secrets stay in `$env`. They are never copied into the run data or the digest.
- An error alert workflow for Slack (`workflow/error-alert-slack.json`).

### Found while testing and reviewing

- n8n 2.x only runs an error workflow that is active. An inactive one never fired, so the setup steps now say to activate it.
- With pagination turned on, n8n's HTTP error puts the status in `error.statusCode` and `error.response.status` instead of `error.status`. Every error message reads all three, so a ClickUp 401 is reported as "HTTP 401".
- A page limit on the ClickUp read would stop quietly and hide open tasks, which could lead to duplicates. The read has no page limit; an empty page always ends it.
- By Shopify's cost rules each variant in this query costs about 4 points, so 250 per page would land right at the 1,000 point limit for one request. `SHOPIFY_PAGE_SIZE` is capped at 200.
