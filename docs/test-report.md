# Test report

**Date:** 2026-10-21
**n8n:** 2.19.5, self-hosted in Docker
**Test script:** [tests/e2e_mock_test.py](../tests/e2e_mock_test.py)
**Result:** 58 of 58 checks pass across 21 scenarios (run time about 4 minutes)

## How it was tested

No real Shopify store, ClickUp workspace or Slack workspace was used. The test script:

1. starts mock Shopify (OAuth token endpoint and Admin GraphQL), ClickUp API v2 and Slack APIs on the test machine,
2. imports temporary copies of the stock check and the error alert workflow, with every `$env` value replaced by a test value, the API hosts pointed at the mocks and the waits shortened to 2 seconds,
3. drives the workflow through the Run Now webhook (and once through the real schedule, with a copy set to run every minute),
4. waits for each n8n run to finish, then checks every request the workflow made and the node each run ended on,
5. deletes every temporary copy, even when a check fails.

Four copies are imported one after the other: one with broken settings, one without Slack, one for the schedule (older custom app token, no Run Now secret), and the main one (Dev Dashboard client credentials) together with an active copy of the error alert workflow.

### What the mocks check

The mocks answer with the real response shapes and refuse what the real APIs would refuse, so a request that passes here has the right shape:

| Mock | Checks | Answers |
|---|---|---|
| Shopify token | `application/x-www-form-urlencoded`, `grant_type=client_credentials`, client ID and secret | `{"access_token", "scope", "expires_in": 86399}` or `invalid_client` |
| Shopify GraphQL | Path `/admin/api/2026-10/graphql.json`, `X-Shopify-Access-Token` is a token the mock issued, JSON content type, a `query` operation with balanced braces, every variable declared, required variables present, `first` between 1 and 250, and the fields the workflow needs (`sku`, `inventoryQuantity`, `inventoryItem { tracked }`, `product { id title vendor }`, `metafield(namespace:, key:) { value }`, `pageInfo`). The `product_status` filter is applied | `data.productVariants.nodes` and `pageInfo`, `extensions.cost.throttleStatus`, `THROTTLED`, `ACCESS_DENIED`, 401, 429, 503 |
| ClickUp | `Authorization` equals the personal token (no `Bearer`), JSON content type on writes, the list ID, task name, priority 1 to 4, integer due date, boolean flags, assignees as numbers, the SKU custom field ID, `comment_text` and `notify_all` on comments, that a status exists in the list, and that a `custom_fields` filter is a JSON array of `field_id`, `operator` and `value` with a text field operator (`==` exact, `=` contains) | Full task objects, `last_page`, `{"err", "ECODE"}` errors, 429 with `X-RateLimit-*` headers (a read 429 says the limit resets in 4 seconds) |
| Slack | JSON body with a non-empty `text` | `ok`, `no_text`, 500 |

The mock ClickUp list has 100 unrelated tasks before the reorder tasks, so the reorder tasks are only on the second page. It also holds a closed task and a task in a Done status for SKUs that are low again, a task made by hand without the SKU field, two copies of one task, and a task for a SKU that is not in Shopify. One scenario adds an open task while the run is scanning Shopify, the way a second run or a person would.

The mock Shopify catalog has 13 active variants (read 3 per page, so 5 pages) and one draft product the default filter leaves out. It includes an untracked variant, a variant without a SKU, a broken reorder point metafield (`abc`) and one SKU on two variants. A throttled answer says 9 more points are needed at 3 points per second, so the right wait (3 seconds) is different from the 2 second fallback.

Shopify and ClickUp have no free endpoint that checks a request without running it, so the request shapes were checked against the official references: [productVariants](https://shopify.dev/docs/api/admin-graphql/latest/queries/productVariants), [ProductVariant](https://shopify.dev/docs/api/admin-graphql/latest/objects/ProductVariant), [client credentials grant](https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens/client-credentials-grant), [GraphQL rate limits](https://shopify.dev/docs/apps/build/apis/graphql-admin/rate-limits), [response codes](https://shopify.dev/docs/api/usage/response-codes), [Get Tasks](https://developer.clickup.com/reference/gettasks), [filtering tasks by Custom Field](https://developer.clickup.com/docs/taskfilters), [Create Task](https://developer.clickup.com/reference/createtask), [Create Task Comment](https://developer.clickup.com/reference/createtaskcomment), [Update Task](https://developer.clickup.com/reference/updatetask) and [rate limits](https://developer.clickup.com/docs/rate-limits).

## Results

```
Settings mistake (no list ID, broken SUPPLIERS_JSON)
  PASS  Run Now answers 500 with the list of problems, not 202  (500 {"status":"not started","problems":["SUPPLIERS_JSON is not valid JSON ...)
  PASS  stopped before any API call, both problems named in the error  (ended at Stop: Bad Settings: The stock check did not start because of a settings problem: SUPPLIERS_JSON is not valid J...)

Slack not set (SLACK_WEBHOOK_URL empty)
  PASS  tasks still created and updated, nothing posted, run ends at Done  (7 writes, 0 Slack calls, ended at Done)

Run Now with RUN_NOW_SECRET not set
  PASS  every call refused with 401 (fails closed)  (401 Respond: Unauthorized, 401 Respond: Unauthorized)

Daily schedule (static access token)
  PASS  the schedule started a full run that posted the digest  (mode trigger, ended at Done, passed the reply node)
  PASS  used SHOPIFY_ACCESS_TOKEN, no token request  (5 GraphQL requests)

Run Now, no secret header
  PASS  refused with 401, nothing called  (401, ended at Respond: Unauthorized)

Run Now, wrong secret
  PASS  refused with 401, nothing called  (401, ended at Respond: Unauthorized)

First run
  PASS  Run Now answers 202 right away  (202 {"status":"started","message":"The stock)
  PASS  one client credentials token request, form encoded  (1 request(s))
  PASS  GraphQL paged with the cursor over 5 pages, page 2 asked twice  (6 requests)
  PASS  GraphQL request: versioned URL, new token, variables  ({"first": 3, "after": null, "query": "product_status:ACTIVE", "ns": "custom", "key": "reorder_point"})
  PASS  THROTTLED page: waited the time the budget needs (3 s), then asked for the same page  (3.0s pause)
  PASS  low query budget: paused about 2 s before the next page  (2.0s pause)
  PASS  ClickUp open tasks read on 2 pages with the right filters and token  (2 requests)
  PASS  right before each create, ClickUp was asked for an open task with exactly that SKU  (3 look-ups, 3 right before a create)
  PASS  three new tasks, only for low SKUs without an open task  (MUG-BLK-12, MUG-WHT-12, PLANT-POT-S)
  PASS  out of stock task: name, urgent, due in 21 days, assignee, tag, SKU field  (Reorder MUG-BLK-12: Stoneware Mug (Black / 12 oz))
  PASS  description has stock numbers, supplier, lead time and the Shopify link  (Stock is at or below the reorder point in Shopify.)
  PASS  SKU prefix supplier, default reorder point after a broken metafield, high priority  (PLANT-POT-S: 7 days, priority 2)
  PASS  stock fell below the task description: one comment  (Stock dropped from 9 to 7 on hand (reorder point 8).)
  PASS  restocked SKU: task closed first, then a comment  (2 calls)
  PASS  task made by hand: found by its name, priority raised to high, no comment  (CANDLE-SND)
  PASS  nothing else changed (7 writes in total)  (7 writes)
  PASS  one Slack digest with every section  (Checked 13 variants on 5 pages: 7 low, 1 back in stock. Skipped 1 not tracked, 1 without a SKU.)
  PASS  run ended successfully at Done  (success, ended at Done)
  PASS  no secret anywhere in the run data or the digest  (tokens stay in $env; the issued Shopify token only in its own node)

Second run, no stock change
  PASS  no new tasks, comments or updates (idempotent)  (0 writes)
  PASS  digest lists the 7 low SKUs as still low  (Checked 13 variants on 5 pages: 7 low, 0 back in stock. Skipped 1 not tracked, 1 without a SKU.)

Stock drops, ClickUp and Shopify rate limits
  PASS  Shopify 429: retried, waited, then the same first page again  (8 GraphQL requests)
  PASS  ClickUp 429: 3 quick tries, a wait, then the same comment once  (4 attempts, 2.0s wait before the last)
  PASS  out of stock now: priority raised to urgent  (MUG-WHT-12)
  PASS  drop measured from the number saved in the last run  (CANDLE-SND 2 to 1)
  PASS  restocked task closed  (RUG-JUTE)
  PASS  digest shows the updates  (Checked 13 variants on 5 pages: 6 low, 1 back in stock. Skipped 1 not tracked, 1 without a SKU.)

ClickUp fails one create
  PASS  the failing create was tried 3 times, the next change still went through  (3 attempts)
  PASS  default supplier and default lead time  (SOAP-LAV: 14 days)
  PASS  digest still posted, with the failure  (failure listed)
  PASS  run ended as failed after the digest, error workflow alerted  (ended at Stop: ClickUp Changes Failed; alert: :rotating_light: *TEST shopify-low-stock-to-clickup (tempora...)
  PASS  the error items of the failed calls hold no secret  (checked all run data)

ClickUp is back
  PASS  the failed task is created once, nothing is doubled  (1 create(s))
  PASS  exactly one open task per low SKU after all runs  (MUG-BLK-12 1, MUG-WHT-12 1, CANDLE-CED 1, PLANT-POT-S 1, CANDLE-SND 1, SOAP-LAV 1, TOWEL-WHT 1, TOWEL-GRY 0, RUG-JUTE 0)

A task appears in ClickUp while the run is going
  PASS  the look-up right before the create found it: no second task  (0 create(s) for RUG-JUTE)
  PASS  the other low SKU still got its task, right after its own look-up  (1 write(s), ended at Done)
  PASS  the digest says why no task was created  (listed under Needs a look)

ClickUp rate limits the task read
  PASS  waited for the reset time ClickUp gave (4 s, not the 2 s fallback), then read every page  (5 reads, 5.0s wait)
  PASS  then the run went on as normal and changed nothing (idempotent)  (0 writes, ended at Done)

Shopify denies access (missing scope)
  PASS  stopped with a clear error, no ClickUp change, no digest, alert sent  (ended at Stop: Shopify Failed: Shopify returned an error for the stock scan: ACCESS_DENIED: Access de...)

Shopify keeps throttling
  PASS  stopped with a clear error, no ClickUp change, no digest, alert sent  (ended at Stop: Shopify Failed: Shopify kept throttling the stock scan (THROTTLED) after 5 waits. Noth...)

Shopify is down (503)
  PASS  stopped with a clear error, no ClickUp change, no digest, alert sent  (ended at Stop: Shopify Failed: The Shopify stock scan failed with HTTP 503 (Service Unavailable). Not...)

Shopify refuses the client credentials
  PASS  stopped with a clear error, no ClickUp change, no digest, alert sent  (ended at Stop: Shopify Auth Failed: Shopify did not give an access token (HTTP 400). Check SHOPIFY_CLIENT_...)
  PASS  token request retried 3 times  (3 requests)

ClickUp refuses the token while reading tasks
  PASS  stopped with a clear error, no ClickUp change, no digest, alert sent  (ended at Stop: ClickUp Failed: Could not read the open tasks in ClickUp list 901100200300 (HTTP 401, ...)
  PASS  Shopify never called  (3 reads)

ClickUp keeps rate limiting the task read
  PASS  gave up after 3 waits: stopped with HTTP 429, Shopify never called, nothing changed, alert sent  (12 reads, ended at Stop: ClickUp Failed: Could not read the open tasks in ClickUp list 901100200300 (...)

Slack is down
  PASS  digest retried 3 times, run flagged for the alert  (ended at Stop: Slack Failed)

ClickUp keeps rate limiting a change
  PASS  3 quick tries, then 3 more after each of 3 waits, then the change is given up for this run  (12 attempts)
  PASS  digest and alert say the rate limit was the reason, run failed  (ended at Stop: ClickUp Changes Failed)

58/58 checks passed
```

## Found while testing and reviewing

| What the test or review showed | Fix |
|---|---|
| The error alert workflow never ran, although it was set as the Error Workflow | n8n 2.x only runs an error workflow that is active. The setup steps now say to activate it, and the test activates its copy |
| A ClickUp 401 while reading the task list was reported without its status | With pagination on, n8n puts the status in `error.statusCode` and `error.response.status`. Every error message now reads all three |
| Error messages in the digest showed the raw body, like `500 - "{\"err\": ...}"` | The ClickUp and Shopify messages are cleaned up to `HTTP 500, Internal Server Error (SHARD_001)` |
| The open tasks are read minutes before the creates on a big catalog, so a second run or a person could open the same task in between | A look-up right before every create (exact match on the SKU field). The scenario "A task appears in ClickUp while the run is going" proves it |
| A 429 on the task read got only the node's 3 quick tries, which cannot outlast a per minute limit | A wait until `X-RateLimit-Reset` (paged requests keep the response headers in n8n's error item), up to 3 times |
| The task read ended when `last_page` was anything but `false`, so a missing field would have hidden tasks after the first 100 | It ends only when `last_page` is `true` or a page is empty |
| Run Now answered 202 before the settings were checked | It answers after the check: 202, or 500 with the problems |
| Alerts showed n8n's batching hint for a 429, the raw ClickUp body for a 401, and a double period after ACCESS_DENIED | Clear messages, checked word for word by the tests |
| The THROTTLED test would also have passed with a fixed 2 second wait | The mock now needs 3 seconds, and the test checks the wait is between 2.8 and 4.5 seconds |

## What the workflow sent (first run)

**Shopify: get token** (`POST /admin/oauth/access_token`, form encoded)

```
grant_type=client_credentials&client_id=mock-client-id&client_secret=mock-client-secret
```

**Shopify: get variants** (`POST /admin/api/2026-10/graphql.json`, last page)

```json
{
  "query": "query LowStockScan($first: Int!, $after: String, $query: String, $ns: String!, $key: String!) {\n  productVariants(first: $first, after: $after, query: $query, sortKey: ID) {\n    nodes {\n      id\n      sku\n      title\n      inventoryQuantity\n      inventoryItem { tracked }\n      product { id title vendor }\n      reorderPoint: metafield(namespace: $ns, key: $key) { value }\n    }\n    pageInfo { hasNextPage endCursor }\n  }\n}",
  "variables": {"first": 3, "after": "eyJsYXN0X2lkIjogMTEwMSwgImxhc3RfdmFsdWUiOiAiMTEwMSJ9", "query": "product_status:ACTIVE", "ns": "custom", "key": "reorder_point"}
}
```

**ClickUp: get open tasks** (`GET /api/v2/list/901100200300/task`)

```
?page=0&archived=false&include_closed=false&subtasks=true      then page=1
```

**ClickUp: find SKU task**, right before each create (`GET /api/v2/list/901100200300/task`)

```
?custom_fields=[{"field_id":"3f6c2a1e-8b4d-4c7a-9e21-5d0b7f9a1c33","operator":"==","value":"MUG-BLK-12"}]&include_closed=false&archived=false&subtasks=true
```

**ClickUp: create task** (`POST /api/v2/list/901100200300/task`)

```json
{
  "name": "Reorder MUG-BLK-12: Stoneware Mug (Black / 12 oz)",
  "description": "Stock is at or below the reorder point in Shopify.\n\nSKU: MUG-BLK-12\nProduct: Stoneware Mug (Black / 12 oz)\nOn hand: 0\nReorder point: 12\nSupplier: Harbor Ceramics Co., orders@harborceramics.example.com\nLead time: 21 days. The due date is when new stock should arrive if it is ordered today.\nShopify: https://admin.shopify.com/store/driftwood-test/products/1001/variants/101\n\nOpened by the daily low stock check. It adds a comment when stock drops further and closes this task when stock is back above the reorder point.",
  "priority": 1,
  "due_date": 1794341529832,
  "due_date_time": false,
  "notify_all": false,
  "assignees": [
    81234567
  ],
  "tags": [
    "low-stock"
  ],
  "custom_fields": [
    {
      "id": "3f6c2a1e-8b4d-4c7a-9e21-5d0b7f9a1c33",
      "value": "MUG-BLK-12"
    }
  ]
}
```

**ClickUp: all calls after the read, in order**

```
GET  /api/v2/list/901100200300/task?custom_fields=[...MUG-BLK-12...]   (look-up)
POST /api/v2/list/901100200300/task  {"name": "Reorder MUG-BLK-12: Stoneware Mug (Black / 12 oz)", ...}
GET  /api/v2/list/901100200300/task?custom_fields=[...MUG-WHT-12...]   (look-up)
POST /api/v2/list/901100200300/task  {"name": "Reorder MUG-WHT-12: Stoneware Mug (White / 12 oz)", ...}
POST /api/v2/task/86b0ced01/comment  {"comment_text": "Stock dropped from 9 to 7 on hand (reorder point 8).", "notify_all": false}
PUT  /api/v2/task/86b0twl01  {"status": "complete"}
POST /api/v2/task/86b0twl01/comment  {"comment_text": "Back in stock: 40 on hand, above the reorder point of 10. Closed automatically by the low stock check.", "notify_all": false}
GET  /api/v2/list/901100200300/task?custom_fields=[...PLANT-POT-S...]   (look-up)
POST /api/v2/list/901100200300/task  {"name": "Reorder PLANT-POT-S: Terracotta Planter (Small)", ...}
PUT  /api/v2/task/86b0snd01  {"priority": 2}
```

The digest of the first run is shown in the [README](../README.md#what-the-team-gets).

## When ClickUp fails one create

**Slack digest** (the rest of the run went through)

```
*Low stock check*, Oct 21, 2026 4:12 AM (run now)
Checked 13 variants on 5 pages: 8 low, 0 back in stock. Skipped 1 not tracked, 1 without a SKU.

*New reorder tasks (1)*
• SOAP-LAV Lavender Soap: 3 on hand, reorder point 10. High priority, Driftwood Wholesale, due Nov 4. <https://app.clickup.com/t/86b100013|Open task>

*Still low, no change (6)*
• MUG-BLK-12 Stoneware Mug (Black / 12 oz): 0 on hand, reorder point 12. <https://app.clickup.com/t/86b100002|Open task>
...

*Needs a look (4)*
• PLANT-POT-S: the reorder point metafield is "abc", which is not a whole number, so the default of 10 was used.
...

*Could not update ClickUp (1)*
• TOWEL-WHT: could not create the reorder task (HTTP 500, Internal Server Error (SHARD_001)). It is tried again on the next run.
```

**Error alert** (from the error alert workflow)

```
:rotating_light: *TEST shopify-low-stock-to-clickup (temporary)* failed at *Stop: ClickUp Changes Failed*
1 ClickUp change(s) failed. TOWEL-WHT: could not create the reorder task (HTTP 500, Internal Server Error (SHARD_001)). It is tried again on the next run.
http://localhost:5678/workflow/jFPso1PUFgaN1P9L/executions/7001
```

On the next run the task for TOWEL-WHT was created once, and nothing else was created or commented.

## When a task appears during the run

A task for RUG-JUTE was added to the mock list while the run was reading Shopify. The look-up right before the create found it, so no second task was made, and the other low SKU still got its task. From the digest:

```
• TOWEL-GRY Bath Towel (Gray): 4 on hand, reorder point 10. High priority, Driftwood Wholesale, due Nov 4. <https://app.clickup.com/t/86b100017|Open task>
• RUG-JUTE is low (2 on hand, reorder point 6), but an open reorder task for it was made while this run was going, so no second task was created. <https://app.clickup.com/t/86b0ext01|Open task>
```

## When ClickUp keeps rate limiting

```
:rotating_light: *TEST shopify-low-stock-to-clickup (temporary)* failed at *Stop: ClickUp Failed*
Could not read the open tasks in ClickUp list 901100200300 (HTTP 429, the rate limit was still reached after 3 waits). Nothing was changed.
http://localhost:5678/workflow/jFPso1PUFgaN1P9L/executions/7089

:rotating_light: *TEST shopify-low-stock-to-clickup (temporary)* failed at *Stop: ClickUp Changes Failed*
1 ClickUp change(s) failed. CANDLE-CED: could not add the stock comment (HTTP 429, the rate limit was still reached after 3 waits). It is tried again on the next run.
http://localhost:5678/workflow/jFPso1PUFgaN1P9L/executions/7112
```

All store names, products, SKUs, suppliers, emails and IDs above are made up.
