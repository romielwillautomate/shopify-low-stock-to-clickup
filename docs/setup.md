# Setup: Shopify, ClickUp, Slack and n8n

This takes about 20 minutes. You need to be the owner of the Shopify store (or a staff member allowed to build apps), a ClickUp member who can create lists and custom fields, and access to the environment of your self-hosted n8n.

## 1. Shopify access

The workflow only reads from Shopify. It needs two Admin API scopes: `read_products` (variants, SKUs, metafields) and `read_inventory` (stock on hand and whether inventory is tracked).

Shopify has two kinds of apps for this. Use the first one unless you already have the second.

### Option A: an app in the Dev Dashboard (new setups)

Since January 1, 2026, new custom apps are made in Shopify's Dev Dashboard. These apps do not show you a permanent token. Instead the workflow trades the app's client ID and secret for a token at the start of every run (Shopify's client credentials grant). The token lasts 24 hours, which is why it is fetched every time.

1. Go to [dev.shopify.com/dashboard](https://dev.shopify.com/dashboard/), open **Apps** and select **Create app**, then **Start from Dev Dashboard**. Name it, for example "Low stock check", and select **Create**.
2. On the **Versions** page, fill in the **App URL** (this app has no screens of its own, so Shopify's placeholder `https://shopify.dev/apps/default-app-home` is fine), pick the newest **Webhooks API version**, add the scopes `read_products` and `read_inventory`, and select **Release**.
3. Open the app and select **Install app** on the **Installs** card. Pick your store and select **Install**.
4. Open **App settings**. Copy the **Client ID** into `SHOPIFY_CLIENT_ID` and the **Client secret** into `SHOPIFY_CLIENT_SECRET`.

The client credentials grant only works for stores in the same Shopify organization as the app (otherwise Shopify answers `shop_not_permitted`). If the run stops at "Stop: Shopify Auth Failed", that is the first thing to check.

### Option B: a custom app made in the Shopify admin (before 2026)

If your store already has a custom app made under **Settings → Apps and sales channels → Develop apps**, you can keep using it:

1. Open the app, and under **Configuration** make sure the Admin API scopes include `read_products` and `read_inventory`.
2. Under **API credentials**, copy the **Admin API access token** (it starts with `shpat_`) into `SHOPIFY_ACCESS_TOKEN`.

Leave `SHOPIFY_CLIENT_ID` and `SHOPIFY_CLIENT_SECRET` empty in that case. If both options are set, the client credentials are used.

Also set `SHOPIFY_STORE_DOMAIN` to the store's `.myshopify.com` address (shown under **Settings → Domains**). A custom domain like `shop.example.com` does not work for the Admin API.

## 2. Reorder points in Shopify

Each variant can have its own reorder point. Without one, the workflow uses `DEFAULT_REORDER_POINT`.

1. In the Shopify admin, go to **Settings → Metafields and metaobjects → Variants** and select **Add definition**.
2. Name it "Reorder point". Check that the namespace and key read `custom.reorder_point` (or put whatever they read into `REORDER_POINT_METAFIELD`).
3. Choose the type **Integer** and save.
4. Open a product, open a variant, and fill in **Reorder point** at the bottom of the page.

A variant is low when its stock on hand is at or below its reorder point. Only variants with **Track quantity** turned on are checked.

## 3. The ClickUp list and token

1. Create a list for reorders, for example "Reorders" in your Purchasing space. The default statuses ("to do" and "complete") work. If you use other statuses, set `CLICKUP_CLOSED_STATUS` to the one that ends a task (a status in the Done or Closed group). Tasks in those groups count as finished, so a new task is opened the next time that SKU runs low.
2. Open the list. The URL ends in `/li/901100200300` or similar. That number is `CLICKUP_LIST_ID`.
3. Generate a personal API token: click your avatar in the upper right, choose **Settings**, then **Apps**, and under **API Token** select **Generate**. Copy it (it starts with `pk_`) into `CLICKUP_API_TOKEN`. The workflow acts as this user, so use an account that can see and edit the list.

To assign new tasks to someone, you need their ClickUp user ID. This command lists the members of your workspaces with their IDs:

```
curl -s https://api.clickup.com/api/v2/team -H "Authorization: pk_your_token"
```

Put one or more IDs, separated by commas, into `CLICKUP_ASSIGNEE_IDS`.

## 4. The SKU custom field

The workflow can find a task's SKU from its name (`Reorder MUG-BLK-12: Stoneware Mug`), but names get edited. A SKU custom field is safer, and it gives you a SKU column in the list view.

1. In the list, add a custom field of type **Text** named "SKU".
2. Get its ID:

```
curl -s https://api.clickup.com/api/v2/list/YOUR_LIST_ID/field -H "Authorization: pk_your_token"
```

3. Find the field named "SKU" in the answer and copy its `id` (it looks like `0a1b2c3d-1234-5678-9abc-0123456789ab`) into `CLICKUP_SKU_FIELD_ID`.

New tasks get the SKU filled in. Tasks people create by hand are still matched by name if the field is empty, as long as the name starts with `Reorder SKU:`.

The field also guards against duplicates. Right before it creates a task, the workflow asks ClickUp for open tasks in the list whose SKU field is exactly that SKU (`custom_fields=[{"field_id": "...", "operator": "==", "value": "MUG-BLK-12"}]`). If a second run or a person made one a moment ago, no task is created. Without `CLICKUP_SKU_FIELD_ID` this look-up is skipped.

## 5. Suppliers

`SUPPLIERS_JSON` tells the workflow who supplies what and how long delivery takes. It must be valid JSON on one line. Each key is one of:

- an exact SKU, like `"MUG-BLK-12"`,
- a SKU prefix ending in `*`, like `"PLANT-*"` (the longest matching prefix wins),
- a Shopify vendor name, like `"Harbor Ceramics"`,
- `"default"` for everything else.

Each value can have `name`, `email` and `leadTimeDays` (a whole number). Example:

```
SUPPLIERS_JSON={"Harbor Ceramics": {"name": "Harbor Ceramics Co.", "email": "orders@harborceramics.example.com", "leadTimeDays": 21}, "PLANT-*": {"name": "Greenline Pottery", "email": "sales@greenline.example.com", "leadTimeDays": 7}, "default": {"name": "Driftwood Wholesale", "email": "buying@driftwood-wholesale.example.com"}}
```

If the JSON is broken, the run stops before calling any API and the alert says so.

## 6. Slack (optional)

1. Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps), turn on **Incoming Webhooks** and add a webhook for the channel that should get the digest.
2. Copy the webhook URL into `SLACK_WEBHOOK_URL`. The same URL is used by the error alert workflow.

Leave it empty to run without Slack. The ClickUp side works the same.

## 7. n8n

1. Add the variables from `.env.example` to your n8n environment (for Docker, the `environment:` section or the env file of the n8n service) and restart n8n. Self-hosted n8n needs `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` so the nodes can read `$env`.
2. Import `workflow/shopify-low-stock-to-clickup.json` and `workflow/error-alert-slack.json`.
3. **Activate the error alert workflow.** n8n 2.x only runs an error workflow that is active (this was checked on n8n 2.19.5: an inactive one never ran).
4. Open the stock check, go to **Settings** and choose the error alert workflow as its **Error Workflow**.
5. Set `RUN_NOW_SECRET` to a long random string and `STOCK_CHECK_CRON` to when you want the check to run (the default is 7:00 every day, in n8n's time zone). Activate the stock check.

## 8. Try it

Run it once by hand:

```
curl -X POST https://YOUR-N8N-DOMAIN/webhook/shopify-low-stock-run-now -H "X-Run-Now-Secret: YOUR-SECRET"
```

You get `202` and `{"status": "started", ...}` as soon as the settings have been checked. A few seconds later (longer for a big catalog) the digest arrives in Slack and the tasks appear in ClickUp. Run it again: nothing new should be created, and the digest lists the same items under "Still low".

## Troubleshooting

If something is off, open the workflow's **Executions** list in n8n:

| You see | Likely cause |
|---|---|
| 401 from Run Now | The `X-Run-Now-Secret` header does not match `RUN_NOW_SECRET`, or `RUN_NOW_SECRET` is not set |
| 500 from Run Now with `"status": "not started"` | A setting is missing or wrong. The `problems` list in the answer names each one |
| Run stopped at "Stop: Bad Settings" | Same as above, for a scheduled run. The error message lists every setting that is missing or wrong |
| Run stopped at "Stop: ClickUp Failed" | `CLICKUP_API_TOKEN` or `CLICKUP_LIST_ID` is wrong, or the token's user cannot see the list. With `HTTP 429`, other tools using the same token used up ClickUp's per minute limit; run the check at a quieter time |
| Run stopped at "Stop: Shopify Auth Failed" | Wrong client ID or secret, the app is not installed on the store, or the store is in another Shopify organization |
| Run stopped at "Stop: Shopify Failed" with `ACCESS_DENIED` | The app is missing `read_products` or `read_inventory`. Add the scope, release a new version and approve it on the store |
| Run stopped at "Stop: Shopify Failed" with `THROTTLED` | Another app is using the store's API budget. Lower `SHOPIFY_PAGE_SIZE` or run at a quieter time |
| Digest says "Could not update ClickUp" | Often `CLICKUP_CLOSED_STATUS` is not a status in the list, or `CLICKUP_SKU_FIELD_ID` is from another list. The error is in the digest |
| Every item shows under "Needs a look" as not found | `SHOPIFY_VARIANT_QUERY` filters them out, or their inventory is not tracked |
| No digest, but tasks are created | `SLACK_WEBHOOK_URL` is empty or wrong |
| A run failed but no alert arrived | The error alert workflow is not active, or it is not chosen as the Error Workflow |
