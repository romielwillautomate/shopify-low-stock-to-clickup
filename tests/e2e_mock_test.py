"""End-to-end test for the Shopify low stock to ClickUp workflow against MOCK APIs.

No real Shopify store, ClickUp workspace or Slack workspace is used. The script:
  1. starts mock Shopify (OAuth token + Admin GraphQL), ClickUp API v2 and Slack APIs on this machine,
  2. imports temporary copies of the workflow and the error alert workflow into your n8n
     (API hosts pointed at the mocks, fake keys, short waits),
  3. plays the scenarios below and checks every request the workflow made and the node each run ended on,
  4. prints PASS or FAIL per check, deletes every temporary copy and exits non-zero on any FAIL.

Scenarios: a settings mistake, Slack not set, the daily schedule (static token), Run Now without and with a wrong secret,
a full first run over 5 Shopify pages and 2 ClickUp pages with a throttled page and a low query budget,
a repeat run that must change nothing, stock drops with a ClickUp 429 and a Shopify 429, a ClickUp failure
and its recovery, a task that appears in ClickUp while a run is going, ClickUp 429s on the task read
(a few, and without end), and failures of Shopify (access denied, endless throttling, 503, token), ClickUp and Slack.

The mocks are strict: they check auth headers, content types, required fields, GraphQL variables and fields,
and they answer with the real APIs' response shapes, so a request the real API would reject fails here too.

Requirements: Python 3.9+, a running n8n with the public API enabled.
Environment:
  N8N_URL      default http://localhost:5678
  N8N_API_KEY  your n8n API key (Settings > n8n API)
  MOCK_HOST    how n8n reaches this machine; default host.docker.internal (n8n in Docker).
               Use 127.0.0.1 if n8n runs directly on this machine.
Usage: python tests/e2e_mock_test.py
"""
import base64, json, math, os, re, sys, threading, time, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

N8N = os.environ.get('N8N_URL', 'http://localhost:5678').rstrip('/')
KEY = os.environ.get('N8N_API_KEY', '')
MOCK_PORT = 8782
MOCK = f"http://{os.environ.get('MOCK_HOST', 'host.docker.internal')}:{MOCK_PORT}"
ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / 'workflow' / 'shopify-low-stock-to-clickup.json'
ERROR_WORKFLOW = ROOT / 'workflow' / 'error-alert-slack.json'
NAME = 'TEST shopify-low-stock-to-clickup (temporary)'
PATH = 'shopify-low-stock-to-clickup-e2e'
DAY_MS = 86_400_000

SHOP = 'driftwood-test.myshopify.com'
API_VERSION = '2026-10'
STATIC_TOKEN = 'shpat_mock_static_token'
CLIENT_ID, CLIENT_SECRET = 'mock-client-id', 'mock-client-secret'
CLICKUP_TOKEN = 'pk_81234567_MOCKTOKEN'
LIST_ID = '901100200300'
SKU_FIELD = '3f6c2a1e-8b4d-4c7a-9e21-5d0b7f9a1c33'
ASSIGNEE = 81234567
RUN_SECRET = 'test-run-now-secret'
SUPPLIERS = {
    'Harbor Ceramics': {'name': 'Harbor Ceramics Co.', 'email': 'orders@harborceramics.example.com', 'leadTimeDays': 21},
    'PLANT-*': {'name': 'Greenline Pottery', 'email': 'sales@greenline.example.com', 'leadTimeDays': 7},
    'default': {'name': 'Driftwood Wholesale', 'email': 'buying@driftwood-wholesale.example.com'},
}
ENV = {
    'SHOPIFY_STORE_DOMAIN': SHOP, 'SHOPIFY_ACCESS_TOKEN': '', 'SHOPIFY_CLIENT_ID': CLIENT_ID, 'SHOPIFY_CLIENT_SECRET': CLIENT_SECRET,
    'SHOPIFY_API_VERSION': API_VERSION, 'SHOPIFY_PAGE_SIZE': '3', 'SHOPIFY_VARIANT_QUERY': '', 'REORDER_POINT_METAFIELD': '',
    'DEFAULT_REORDER_POINT': '10', 'DEFAULT_LEAD_TIME_DAYS': '14', 'SUPPLIERS_JSON': json.dumps(SUPPLIERS),
    'CLICKUP_API_TOKEN': CLICKUP_TOKEN, 'CLICKUP_LIST_ID': LIST_ID, 'CLICKUP_SKU_FIELD_ID': SKU_FIELD,
    'CLICKUP_ASSIGNEE_IDS': str(ASSIGNEE), 'CLICKUP_TAG': '', 'CLICKUP_CLOSED_STATUS': '', 'CLICKUP_RATE_LIMIT_WAIT_SECONDS': '2',
    'RUN_NOW_SECRET': RUN_SECRET, 'STOCK_CHECK_CRON': '0 0 1 1 *', 'SLACK_WEBHOOK_URL': MOCK + '/slack/hook',
}
# The workflow builds every Shopify URL from 'https://{shop}' and every ClickUp URL from the v2 base.
# The shop domain stays in the mock path, so the mock can check it.
HOSTS = {"'https://{shop}'": f"'{MOCK}/{{shop}}'", 'https://api.clickup.com/api/v2': MOCK + '/api/v2'}

# ---------------------------------------------------------------- fake store
PRODUCTS = {
    1: ('Stoneware Mug', 'Harbor Ceramics', 'ACTIVE'), 2: ('Cedar Candle', 'Northwick Candle Co.', 'ACTIVE'),
    3: ('Bath Towel', 'Loomhouse Textiles', 'ACTIVE'), 4: ('Wool Blanket', 'Loomhouse Textiles', 'ACTIVE'),
    5: ('Sample Pack', 'Driftwood Home Goods', 'ACTIVE'), 6: ('Terracotta Planter', 'Greenline Pottery', 'ACTIVE'),
    7: ('Sandalwood Candle', 'Northwick Candle Co.', 'ACTIVE'), 8: ('Jute Rug', 'Loomhouse Textiles', 'ACTIVE'),
    9: ('Glass Vase', 'Clearwater Glass', 'ACTIVE'), 10: ('Linen Napkins', 'Loomhouse Textiles', 'DRAFT'),
    11: ('Lavender Soap', 'Northwick Candle Co.', 'ACTIVE'), 12: ('Towel Gift Set', 'Loomhouse Textiles', 'ACTIVE'),
}
VARIANTS = [  # id, product, sku, variant title, on hand, tracked, reorder point metafield
    (101, 1, 'MUG-BLK-12', 'Black / 12 oz', 0, True, '12'),
    (102, 1, 'MUG-WHT-12', 'White / 12 oz', 5, True, None),
    (201, 2, 'CANDLE-CED', 'Default Title', 7, True, '8'),
    (301, 3, 'TOWEL-GRY', 'Gray', 40, True, '10'),
    (302, 3, 'TOWEL-WHT', 'White', 25, True, None),
    (401, 4, 'BLANKET-OAT', 'Oat', 3, False, None),        # inventory not tracked
    (501, 5, '', 'Default Title', 0, True, None),           # no SKU
    (601, 6, 'PLANT-POT-S', 'Small', 4, True, 'abc'),       # broken metafield
    (701, 7, 'CANDLE-SND', 'Default Title', 2, True, None),
    (801, 8, 'RUG-JUTE', '2 x 3 ft', 1, True, '6'),
    (901, 9, 'VASE-CLR', 'Clear', 2, True, '5'),
    (1001, 10, 'NAPKIN-LIN', 'Set of 4', 0, True, None),    # draft product, filtered out by the query
    (1101, 11, 'SOAP-LAV', 'Default Title', 25, True, '10'),
    (1201, 12, 'TOWEL-WHT', 'White', 0, True, None),        # same SKU as 302: stock is added up, so not low
]
STATUSES = {
    'to do': {'id': 'sc901_todo', 'status': 'to do', 'color': '#87909e', 'orderindex': 0, 'type': 'open'},
    'ordered': {'id': 'sc901_ord', 'status': 'ordered', 'color': '#5f55ee', 'orderindex': 1, 'type': 'custom'},
    'received': {'id': 'sc901_rcv', 'status': 'received', 'color': '#0f9d9f', 'orderindex': 2, 'type': 'done'},
    'complete': {'id': 'sc901_done', 'status': 'complete', 'color': '#008844', 'orderindex': 3, 'type': 'closed'},
}
PRIORITIES = {1: 'urgent', 2: 'high', 3: 'normal', 4: 'low'}
SEED_TASKS = [  # id, name, sku field, status, priority, description
    ('86b0ced01', 'Reorder CANDLE-CED: Cedar Candle', 'CANDLE-CED', 'to do', 3, 'SKU: CANDLE-CED\nOn hand: 9\nReorder point: 8'),
    ('86b0twl01', 'Reorder TOWEL-GRY: Bath Towel (Gray)', 'TOWEL-GRY', 'ordered', 2, 'SKU: TOWEL-GRY\nOn hand: 3\nReorder point: 10'),
    ('86b0snd01', 'Reorder CANDLE-SND: Sandalwood candle', None, 'to do', None, 'Ask Northwick for a quote before ordering.'),
    ('86b0rug01', 'Reorder RUG-JUTE: Jute Rug (2 x 3 ft)', 'RUG-JUTE', 'to do', 2, 'SKU: RUG-JUTE\nOn hand: 1\nReorder point: 6'),
    ('86b0vs101', 'Reorder VASE-CLR: Glass Vase (Clear)', 'VASE-CLR', 'to do', 2, 'SKU: VASE-CLR\nOn hand: 2\nReorder point: 5'),
    ('86b0vs201', 'Reorder VASE-CLR: Glass Vase (Clear)', 'VASE-CLR', 'to do', 2, 'SKU: VASE-CLR\nOn hand: 2\nReorder point: 5'),
    ('86b0lmp01', 'Reorder LAMP-BRASS: Brass Desk Lamp', 'LAMP-BRASS', 'to do', 3, 'SKU: LAMP-BRASS\nOn hand: 1'),
    ('86b0mug00', 'Reorder MUG-BLK-12: Stoneware Mug (Black / 12 oz)', 'MUG-BLK-12', 'complete', 1, 'On hand: 0'),
    ('86b0plt00', 'Reorder PLANT-POT-S: Terracotta Planter (Small)', 'PLANT-POT-S', 'received', 2, 'On hand: 2'),
]

LOG = []
LOCK = threading.Lock()
M = {}


def reset_mock():
    """Fresh store, list and failure switches."""
    base = 1_760_000_000_000
    tasks = {}
    for n in range(100):  # other work in the same list, so the open reorder tasks land on page 2
        tid = f'86a{n:06d}'
        tasks[tid] = dict(id=tid, name=f'Weekly team sync notes #{n + 1}', sku=None, status='to do', priority=None,
                          description='Agenda and notes.', created=base + n, due=None, assignees=[], tags=[])
    for i, (tid, name, sku, status, prio, desc) in enumerate(SEED_TASKS):
        tasks[tid] = dict(id=tid, name=name, sku=sku, status=status, priority=prio, description=desc,
                          created=base + 1000 + i, due=None, assignees=[ASSIGNEE], tags=['low-stock'])
    M.clear()
    M.update(variants={v[0]: dict(zip(('id', 'product', 'sku', 'title', 'qty', 'tracked', 'mf'), v)) for v in VARIANTS},
             tokens=set(), tasks=tasks, comments={}, seq=0,
             # failure switches
             throttle_once=set(), low_budget=set(), throttled_seen=set(), throttle_forever=False, shopify_429=0,
             shopify_503=False, access_denied=False, token_fail=False, on_graphql=None,
             clickup_429=0, clickup_read_429=0, fail_create=set(), scan_fail=False, slack_fail=False)


def qty(sku, n):
    next(v for v in M['variants'].values() if v['sku'] == sku)['qty'] = n


def cursor_for(vid):
    return base64.b64encode(json.dumps({'last_id': vid, 'last_value': str(vid)}).encode()).decode()


# ---------------------------------------------------------------- mock Shopify GraphQL checks
def graphql_problems(body):
    """What the real Admin GraphQL API would reject in this request."""
    q, v = body.get('query'), body.get('variables')
    if not isinstance(q, str) or not q.strip().startswith('query'):
        return ['Parse error: expected a query operation']
    if not isinstance(v, dict):
        return ['Variables must be a JSON object']
    if q.count('{') != q.count('}') or q.count('(') != q.count(')'):
        return ['Parse error on "}" (RCURLY): unbalanced braces or parentheses']
    sig = re.match(r'\s*query\s+\w*\s*\(([^)]*)\)', q)
    declared = dict(re.findall(r'\$(\w+)\s*:\s*([\w!\[\]]+)', sig.group(1))) if sig else {}
    body_text = q[sig.end():] if sig else q
    p = []
    for name in sorted(set(re.findall(r'\$(\w+)', body_text)) - set(declared)):
        p.append(f'Variable ${name} is used by LowStockScan but not declared')
    for name, typ in declared.items():
        if typ.endswith('!') and v.get(name) is None:
            p.append(f'Variable ${name} of type {typ} was provided invalid value')
    args = re.search(r'productVariants\(([^)]*)\)', q)
    if not args or '$first' not in args.group(1) or 'after: $after' not in args.group(1):
        p.append('productVariants must be paged with first and after')
    first = v.get('first')
    if not isinstance(first, int) or isinstance(first, bool) or not 1 <= first <= 250:
        p.append('The first argument must be between 1 and 250')
    for field in ('sku', 'inventoryQuantity', 'hasNextPage', 'endCursor'):
        if not re.search(rf'\b{field}\b', q):
            p.append(f'missing field {field} (the workflow needs it)')
    if not re.search(r'inventoryItem\s*\{\s*tracked\s*\}', q):
        p.append('missing inventoryItem { tracked }')
    if not re.search(r'metafield\(\s*namespace:\s*\$ns\s*,\s*key:\s*\$key\s*\)\s*\{\s*value\s*\}', q):
        p.append('missing metafield(namespace:, key:) { value }')
    if not re.search(r'product\s*\{\s*id\s+title\s+vendor\s*\}', q):
        p.append('missing product { id title vendor }')
    return p


def shopify_graphql(headers, body):
    token = headers.get('x-shopify-access-token', '')
    if token != STATIC_TOKEN and token not in M['tokens']:
        return 401, {'errors': '[API] Invalid API key or access token (unrecognized login or wrong password)'}
    if not headers.get('content-type', '').startswith('application/json'):
        return 415, {'errors': 'Unsupported Media Type'}
    if M['on_graphql']:   # something happens elsewhere while the scan runs (used once, then cleared)
        hook, M['on_graphql'] = M['on_graphql'], None
        hook()
    if M['shopify_503']:
        return 503, {'errors': 'Service Unavailable'}
    if M['shopify_429'] > 0:
        M['shopify_429'] -= 1
        return 429, {'errors': 'Exceeded the rate limit. Reduce request rates to resume uninterrupted service.'}
    if M['access_denied']:
        return 200, {'data': None, 'errors': [{
            'message': 'Access denied for productVariants field. Required access: `read_products` access scope.',
            'locations': [{'line': 2, 'column': 3}], 'path': ['productVariants'],
            'extensions': {'code': 'ACCESS_DENIED', 'documentation': 'https://shopify.dev/api/usage/access-scopes'}}]}
    problems = graphql_problems(body)
    if problems:
        return 200, {'errors': [{'message': m, 'locations': [{'line': 1, 'column': 1}]} for m in problems]}
    v = body['variables']
    # product_status takes a comma-separated list of statuses, like product_status:ACTIVE,DRAFT
    statuses = re.search(r'\bproduct_status:([\w,]+)', v.get('query') or '')
    allowed = {s.upper() for s in statuses.group(1).split(',')} if statuses else None
    rows = sorted((x for x in M['variants'].values() if allowed is None or PRODUCTS[x['product']][2] in allowed),
                  key=lambda x: x['id'])
    start = 0
    if v.get('after'):
        try:
            last = json.loads(base64.b64decode(v['after']))['last_id']
            start = next(i for i, x in enumerate(rows) if x['id'] == last) + 1
        except Exception:
            return 200, {'errors': [{'message': 'Invalid cursor for current pagination sort.', 'extensions': {'code': 'BAD_USER_INPUT'}}]}
    page_no = sum(1 for x in rows[:start]) // v['first'] if start else 0
    requested = 2 + v['first'] * 3
    # (11 needed - 2 available) / 3 per second = 3 seconds. Not 2, so the test can tell this wait from the 2 second fallback.
    throttled = {'requestedQueryCost': requested, 'actualQueryCost': None,
                 'throttleStatus': {'maximumAvailable': 2000.0, 'currentlyAvailable': 2, 'restoreRate': 3.0}}
    if M['throttle_forever'] or (page_no in M['throttle_once'] and page_no not in M['throttled_seen']):
        M['throttled_seen'].add(page_no)
        return 200, {'errors': [{'message': 'Throttled', 'extensions': {'code': 'THROTTLED', 'documentation': 'https://shopify.dev/api/usage/limits#rate-limits'}}],
                     'extensions': {'cost': throttled}}
    chunk = rows[start:start + v['first']]
    alias = (re.search(r'(\w+)\s*:\s*metafield\(', body['query']) or [None, 'metafield'])[1]
    nodes = []
    for x in chunk:
        title, vendor, _ = PRODUCTS[x['product']]
        nodes.append({'id': f"gid://shopify/ProductVariant/{x['id']}", 'sku': x['sku'], 'title': x['title'],
                      'inventoryQuantity': x['qty'], 'inventoryItem': {'tracked': x['tracked']},
                      'product': {'id': f"gid://shopify/Product/{1000 + x['product']}", 'title': title, 'vendor': vendor},
                      alias: {'value': x['mf']} if x['mf'] is not None else None})
    has_next = start + len(chunk) < len(rows)
    actual = 2 + len(chunk) * 3
    status = {'maximumAvailable': 2000.0, 'currentlyAvailable': 2000 - actual, 'restoreRate': 100.0}
    if page_no in M['low_budget']:
        status = {'maximumAvailable': 2000.0, 'currentlyAvailable': 5, 'restoreRate': 5.0}
    return 200, {'data': {'productVariants': {'nodes': nodes, 'pageInfo': {
        'hasNextPage': has_next, 'endCursor': cursor_for(chunk[-1]['id']) if chunk else None}}},
        'extensions': {'cost': {'requestedQueryCost': requested, 'actualQueryCost': actual, 'throttleStatus': status}}}


def shopify_token(headers, raw):
    if not headers.get('content-type', '').startswith('application/x-www-form-urlencoded'):
        return 400, {'error': 'invalid_request', 'error_description': 'The request must be form encoded.'}
    form = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
    if M['token_fail'] or form.get('grant_type') != 'client_credentials' or form.get('client_id') != CLIENT_ID or form.get('client_secret') != CLIENT_SECRET:
        return 400, {'error': 'invalid_client', 'error_description': 'Client authentication failed.'}
    M['seq'] += 1
    token = f"shpat_mock_{M['seq']:04d}"
    M['tokens'].add(token)
    return 200, {'access_token': token, 'scope': 'read_inventory,read_products', 'expires_in': 86399}


# ---------------------------------------------------------------- mock ClickUp
def task_json(t):
    out = {
        'id': t['id'], 'custom_id': None, 'custom_item_id': 0, 'name': t['name'], 'text_content': t['description'],
        'description': t['description'], 'status': STATUSES[t['status']], 'orderindex': '1.00000000000000000000',
        'date_created': str(t['created']), 'date_updated': str(t['created']),
        'date_closed': str(t['created']) if STATUSES[t['status']]['type'] == 'closed' else None, 'archived': False,
        'creator': {'id': ASSIGNEE, 'username': 'Riley Buyer', 'color': '#7b68ee', 'email': 'riley@example.com', 'profilePicture': None},
        'assignees': [{'id': a, 'username': 'Riley Buyer', 'color': '#7b68ee', 'initials': 'RB', 'email': 'riley@example.com', 'profilePicture': None} for a in t['assignees']],
        'watchers': [], 'checklists': [], 'tags': [{'name': g, 'tag_fg': '#ffffff', 'tag_bg': '#e93d82'} for g in t['tags']],
        'parent': None, 'priority': {'color': '#f8ae00', 'id': str(t['priority']), 'orderindex': str(t['priority']), 'priority': PRIORITIES[t['priority']]} if t['priority'] else None,
        'due_date': str(t['due']) if t['due'] else None, 'start_date': None, 'points': None, 'time_estimate': None,
        'custom_fields': [dict({'id': SKU_FIELD, 'name': 'SKU', 'type': 'short_text', 'type_config': {}, 'date_created': '1760000000000',
                                'hide_from_guests': False, 'required': False}, **({'value': t['sku']} if t['sku'] else {}))],
        'dependencies': [], 'linked_tasks': [], 'team_id': '9011000001', 'url': f"https://app.clickup.com/t/{t['id']}",
        'permission_level': 'create', 'list': {'id': LIST_ID, 'name': 'Reorders', 'access': True},
        'project': {'id': '90110002', 'name': 'Purchasing', 'hidden': False, 'access': True},
        'folder': {'id': '90110002', 'name': 'Purchasing', 'hidden': False, 'access': True}, 'space': {'id': '90110001'},
    }
    return out


def clickup_err(code, err, ecode):
    return code, {'err': err, 'ECODE': ecode}


def create_problems(b):
    if not isinstance(b.get('name'), str) or not b['name'].strip():
        return 'Task name invalid', 'INPUT_005'
    if 'priority' in b and b['priority'] not in (1, 2, 3, 4, None):
        return 'Priority invalid', 'INPUT_003'
    if 'due_date' in b and (not isinstance(b['due_date'], int) or isinstance(b['due_date'], bool)):
        return 'Due date invalid', 'INPUT_002'
    for key in ('due_date_time', 'notify_all'):
        if key in b and not isinstance(b[key], bool):
            return f'{key} must be a boolean', 'INPUT_001'
    if 'description' in b and not isinstance(b['description'], str):
        return 'Description invalid', 'INPUT_006'
    if 'assignees' in b and (not isinstance(b['assignees'], list) or any(not isinstance(a, int) or isinstance(a, bool) for a in b['assignees'])):
        return 'Assignees must be an array of user IDs', 'INPUT_007'
    if 'tags' in b and (not isinstance(b['tags'], list) or any(not isinstance(g, str) for g in b['tags'])):
        return 'Tags must be an array of strings', 'INPUT_008'
    if 'custom_fields' in b:
        if not isinstance(b['custom_fields'], list):
            return 'Custom fields must be an array', 'FIELD_001'
        for f in b['custom_fields']:
            if not isinstance(f, dict) or f.get('id') != SKU_FIELD:
                return 'Custom field not found on this list', 'FIELD_033'
            if not isinstance(f.get('value'), str):
                return 'Value must be a string', 'FIELD_015'
    if 'status' in b and str(b['status']).lower() not in STATUSES:
        return 'Status does not exist', 'CRTSK_001'
    return None


TEXT_OPERATORS = ('=', '==', '!=', '!==', 'IS NULL', 'IS NOT NULL')


def custom_field_filter(raw):
    """Parses Get Tasks' custom_fields query parameter: a stringified JSON array of {field_id, operator, value}."""
    try:
        filters = json.loads(raw)
    except ValueError:
        return None, 'custom_fields must be a stringified JSON array'
    if not isinstance(filters, list) or not filters:
        return None, 'custom_fields must be a stringified JSON array'
    for f in filters:
        if not isinstance(f, dict) or 'field_id' not in f or 'operator' not in f:
            return None, 'Each custom field filter needs field_id, operator and value'
        if f['field_id'] != SKU_FIELD:
            return None, 'Custom field not found'
        if f['operator'] not in TEXT_OPERATORS:
            return None, 'Operator is not valid for a text Custom Field'
        if not f['operator'].startswith('IS') and not isinstance(f.get('value'), str):
            return None, 'Value must be a string for a text Custom Field'
    return filters, None


def custom_field_match(t, f):
    v, op = t['sku'], f['operator']
    if op == 'IS NULL':
        return v is None
    if op == 'IS NOT NULL':
        return v is not None
    exact = v is not None and v == f['value']                                 # == is an exact match
    fuzzy = v is not None and f['value'].lower() in v.lower()                 # = is a fuzzy "contains"
    return {'==': exact, '!==': not exact, '=': fuzzy, '!=': not fuzzy}[op]


def clickup(method, path, query, headers, body):
    auth = headers.get('authorization')
    if not auth:
        return clickup_err(400, 'Authorization header required', 'OAUTH_017')
    if auth != CLICKUP_TOKEN:
        return clickup_err(401, 'Token invalid', 'OAUTH_025')
    limited = {'X-RateLimit-Limit': '100', 'X-RateLimit-Remaining': '0'}
    if method in ('POST', 'PUT'):
        if not headers.get('content-type', '').startswith('application/json'):
            return clickup_err(400, 'Content-Type must be application/json', 'INPUT_000')
        if M['clickup_429'] > 0:
            M['clickup_429'] -= 1
            return 429, {'err': 'Rate limit reached', 'ECODE': 'APP_002'}, dict(limited, **{'X-RateLimit-Reset': str(int(time.time()) + 60)})
    if method == 'GET' and M['clickup_read_429'] > 0:
        M['clickup_read_429'] -= 1
        # The limit resets in 4 seconds. The workflow should wait for that, not its 2 second fallback.
        return 429, {'err': 'Rate limit reached', 'ECODE': 'APP_002'}, dict(limited, **{'X-RateLimit-Reset': str(int(time.time()) + 4)})
    m = re.fullmatch(r'/api/v2/list/(\w+)/task', path)
    if m and method == 'GET':
        if M['scan_fail']:
            return clickup_err(401, 'Token invalid', 'OAUTH_025')
        if m.group(1) != LIST_ID:
            return clickup_err(404, 'List not found', 'ITEM_013')
        page = int((query.get('page') or ['0'])[0])
        include_closed = (query.get('include_closed') or ['false'])[0] == 'true'
        rows = [t for t in sorted(M['tasks'].values(), key=lambda t: t['created'])
                if include_closed or STATUSES[t['status']]['type'] != 'closed']
        if 'custom_fields' in query:
            filters, problem = custom_field_filter(query['custom_fields'][0])
            if problem:
                return clickup_err(400, problem, 'FIELD_036')
            rows = [t for t in rows if all(custom_field_match(t, f) for f in filters)]
        chunk = rows[page * 100:(page + 1) * 100]
        return 200, {'tasks': [task_json(t) for t in chunk], 'last_page': (page + 1) * 100 >= len(rows)}
    if m and method == 'POST':
        if m.group(1) != LIST_ID:
            return clickup_err(404, 'List not found', 'ITEM_013')
        problem = create_problems(body)
        if problem:
            return clickup_err(400, *problem)
        sku = next((f['value'] for f in body.get('custom_fields', []) if f.get('id') == SKU_FIELD), None)
        if sku in M['fail_create']:
            return clickup_err(500, 'Internal Server Error', 'SHARD_001')
        M['seq'] += 1
        tid = f"86b1{M['seq']:05d}"
        M['tasks'][tid] = dict(id=tid, name=body['name'], sku=sku, status=str(body.get('status', 'to do')).lower(),
                               priority=body.get('priority'), description=body.get('description', ''), created=int(time.time() * 1000),
                               due=body.get('due_date'), assignees=body.get('assignees', []), tags=body.get('tags', []))
        return 200, task_json(M['tasks'][tid])
    m = re.fullmatch(r'/api/v2/task/(\w+)/comment', path)
    if m and method == 'POST':
        if m.group(1) not in M['tasks']:
            return clickup_err(404, 'Task not found', 'ITEM_015')
        if not isinstance(body.get('comment_text'), str) or not body['comment_text'].strip():
            return clickup_err(400, 'Comment text invalid', 'COMM_002')
        if not isinstance(body.get('notify_all'), bool):
            return clickup_err(400, 'notify_all is required', 'COMM_003')
        M['seq'] += 1
        M['comments'].setdefault(m.group(1), []).append(body['comment_text'])
        return 200, {'id': str(90000 + M['seq']), 'hist_id': f"hist{M['seq']}", 'date': int(time.time() * 1000)}
    m = re.fullmatch(r'/api/v2/task/(\w+)', path)
    if m and method == 'PUT':
        t = M['tasks'].get(m.group(1))
        if not t:
            return clickup_err(404, 'Task not found', 'ITEM_015')
        if not body:
            return clickup_err(400, 'Nothing to update', 'INPUT_009')
        if 'status' in body and str(body['status']).lower() not in STATUSES:
            return clickup_err(400, 'Status does not exist', 'CRTSK_001')
        if 'priority' in body and body['priority'] not in (1, 2, 3, 4, None):
            return clickup_err(400, 'Priority invalid', 'INPUT_003')
        if 'status' in body:
            t['status'] = str(body['status']).lower()
        if 'priority' in body:
            t['priority'] = body['priority']
        return 200, task_json(t)
    return clickup_err(404, 'Route not found', 'APP_001')


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, obj, headers=None):
        data = (obj if isinstance(obj, str) else json.dumps(obj)).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'text/html' if isinstance(obj, str) else 'application/json')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        raw = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        headers = {k.lower(): v for k, v in self.headers.items()}
        url = urlsplit(self.path)
        path, query = url.path, parse_qs(url.query)
        try:
            body = json.loads(raw) if raw and headers.get('content-type', '').startswith('application/json') else {}
        except ValueError:
            return self._send(400, {'errors': 'Invalid JSON'})
        with LOCK:
            svc = ('slack' if path.startswith('/slack') else 'clickup' if path.startswith('/api/v2/')
                   else 'shopify_token' if path.endswith('/admin/oauth/access_token') else 'shopify' if '.myshopify.com/' in path else 'other')
            LOG.append(dict(svc=svc, method=method, path=path, query=query, headers=headers, body=body, raw=raw, t=time.time()))
            extra = None
            if svc == 'slack':
                if M['slack_fail']:
                    result = (500, 'internal_error')
                elif not isinstance(body.get('text'), str) or not body['text']:
                    result = (400, 'no_text')
                else:
                    result = (200, 'ok')
            elif svc == 'clickup':
                result = clickup(method, path, query, headers, body)
            elif svc == 'shopify_token':
                result = shopify_token(headers, raw) if method == 'POST' and path == f'/{SHOP}/admin/oauth/access_token' else (404, {'errors': 'Not Found'})
            elif svc == 'shopify':
                result = shopify_graphql(headers, body) if method == 'POST' and path == f'/{SHOP}/admin/api/{API_VERSION}/graphql.json' else (404, {'errors': 'Not Found'})
            else:
                result = (404, {'message': 'not mocked'})
            if len(result) == 3:
                result, extra = result[:2], result[2]
        return self._send(result[0], result[1], extra)

    def do_GET(self):
        self._handle('GET')

    def do_POST(self):
        self._handle('POST')

    def do_PUT(self):
        self._handle('PUT')


# ---------------------------------------------------------------- n8n helpers
def api(method, path, body=None):
    req = urllib.request.Request(N8N + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'X-N8N-API-KEY': KEY, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode()
        return json.loads(text) if text else {}


def test_copy(source, env, path):
    """The workflow with $env values replaced by test values and the API hosts pointed at the mocks."""
    wf = json.loads(source.read_text(encoding='utf-8'))

    def fix(value):
        if isinstance(value, dict):
            return {k: fix(v) for k, v in value.items()}
        if isinstance(value, list):
            return [fix(v) for v in value]
        if not isinstance(value, str):
            return value
        value = re.sub(r'\$env\.([A-Z0-9_]+)', lambda m: json.dumps(env.get(m.group(1), '')), value)
        for real, mock in HOSTS.items():
            value = value.replace(real, mock)
        return value

    for node in wf['nodes']:
        node['parameters'] = fix(node.get('parameters', {}))
        if node['type'] == 'n8n-nodes-base.webhook':
            node['parameters']['path'] = path
    return wf


def import_workflow(wf, settings=None):
    return api('POST', '/api/v1/workflows', {'name': NAME, 'nodes': wf['nodes'], 'connections': wf['connections'],
                                             'settings': settings or wf.get('settings', {})})['id']


class Runner:
    def __init__(self, wid, path):
        self.wid, self.path, self.sent, self.results = wid, path, 0, []

    def executions(self):
        return api('GET', f'/api/v1/executions?workflowId={self.wid}&limit=250').get('data', [])

    def wait(self, timeout=150):
        end = time.time() + timeout
        while time.time() < end:
            done = [e for e in self.executions() if e.get('stoppedAt')]
            if len(done) >= self.sent:
                return max(done, key=lambda e: int(e['id']))
            time.sleep(0.5)
        raise TimeoutError('the workflow did not finish in time')

    def run_now(self, secret=RUN_SECRET):
        """POST to the Run Now webhook. Returns status, body, the finished execution and the requests it made."""
        mark = len(LOG)
        headers = {'Content-Type': 'application/json'}
        if secret is not None:
            headers['X-Run-Now-Secret'] = secret
        req = urllib.request.Request(f'{N8N}/webhook/{self.path}', method='POST', data=b'{}', headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                status, body = r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            status, body = e.code, e.read().decode()
        self.sent += 1
        started = time.time()
        ex = self.wait()
        return Run(status, body, ex, LOG[mark:], started, time.time())

    def check(self, scenario, label, ok, detail=''):
        self.results.append((scenario, label, bool(ok), detail))


class Run:
    def __init__(self, status, body, ex, log, started, ended):
        self.status, self.body, self.ex, self.log, self.started, self.ended = status, body, ex, log, started, ended
        data = api('GET', f"/api/v1/executions/{ex['id']}?includeData=true")
        result = (data.get('data') or {}).get('resultData') or {}
        self.mode, self.state = data.get('mode'), data.get('status')
        self.node, self.error = result.get('lastNodeExecuted'), (result.get('error') or {}).get('message', '')
        self.run_data = result.get('runData') or {}

    def output(self, node):
        """The first output item of a node's last run."""
        runs = self.run_data.get(node) or [{}]
        return ((runs[-1].get('data') or {}).get('main') or [[{}]])[0][0].get('json')

    def calls(self, svc, method=None, pattern=None):
        return [e for e in self.log if e['svc'] == svc and (method is None or e['method'] == method)
                and (pattern is None or re.fullmatch(pattern, e['path']))]

    def writes(self):
        return [e for e in self.log if e['svc'] == 'clickup' and e['method'] in ('POST', 'PUT')]

    def scans(self):
        """Reads of the whole reorder list (no custom field filter)."""
        return [e for e in self.calls('clickup', 'GET', r'/api/v2/list/\w+/task') if 'custom_fields' not in e['query']]

    def checks(self, sku=None):
        """The look-up right before a create: open tasks whose SKU field is exactly this SKU."""
        out = []
        for e in self.calls('clickup', 'GET', r'/api/v2/list/\w+/task'):
            if 'custom_fields' in e['query']:
                f = json.loads(e['query']['custom_fields'][0])
                if sku is None or f[0].get('value') == sku:
                    out.append(e)
        return out

    def secrets_hidden(self, issued_token=None):
        """No API secret anywhere in the run data. A token Shopify issued may only be in the token node's own output."""
        everything = json.dumps(self.run_data)
        rest = json.dumps({k: v for k, v in self.run_data.items() if k != 'Shopify: Get Token'})
        return (bool(self.run_data) and not any(x in everything for x in (CLICKUP_TOKEN, CLIENT_SECRET, STATIC_TOKEN))
                and not (issued_token and issued_token in rest))

    def digests(self):
        return [e['body']['text'] for e in self.log if e['svc'] == 'slack' and str(e['body'].get('text', '')).startswith('*Low stock check*')]

    def creates(self, sku=None):
        return [e for e in self.calls('clickup', 'POST', r'/api/v2/list/\w+/task')
                if sku is None or any(f.get('value') == sku for f in e['body'].get('custom_fields', []))]

    def comments(self, task_id):
        return [e for e in self.calls('clickup', 'POST', rf'/api/v2/task/{task_id}/comment')]

    def updates(self, task_id=None):
        return [e for e in self.calls('clickup', 'PUT', r'/api/v2/task/\w+') if task_id is None or e['path'].endswith('/' + task_id)]


def open_tasks(sku):
    return [t for t in M['tasks'].values() if (t['sku'] == sku or t['name'].startswith(f'Reorder {sku}:'))
            and STATUSES[t['status']]['type'] not in ('closed', 'done')]


def task_for(sku):
    tasks = open_tasks(sku)
    return tasks[0] if len(tasks) == 1 else None


def error_alert(after, timeout=25):
    """The Slack message the error alert workflow posts."""
    end = time.time() + timeout
    while time.time() < end:
        hits = [e['body'].get('text', '') for e in LOG if e['svc'] == 'slack' and e['t'] >= after
                and str(e['body'].get('text', '')).startswith(':rotating_light:')]
        if hits:
            return hits[0]
        time.sleep(0.5)
    return ''


def short(text, n=110):
    text = ' '.join(str(text).split())
    return text if len(text) <= n else text[:n] + '...'


# ---------------------------------------------------------------- scenarios
def bad_settings(results):
    env = dict(ENV, CLICKUP_LIST_ID='', SUPPLIERS_JSON='{"Harbor Ceramics": ')
    wid = import_workflow(test_copy(WORKFLOW, env, PATH + '-bad-settings'))
    try:
        api('POST', f'/api/v1/workflows/{wid}/activate')
        time.sleep(1.5)
        r = Runner(wid, PATH + '-bad-settings')
        run = r.run_now()
        s = 'Settings mistake (no list ID, broken SUPPLIERS_JSON)'
        try:
            body = json.loads(run.body)
        except ValueError:
            body = {}
        problems = body.get('problems') or []
        results.append((s, 'Run Now answers 500 with the list of problems, not 202',
                        run.status == 500 and body.get('status') == 'not started' and len(problems) == 2
                        and problems[0].startswith('SUPPLIERS_JSON is not valid JSON') and problems[1].startswith('CLICKUP_LIST_ID is missing'),
                        f'{run.status} {short(run.body, 70)}'))
        ok = (run.state == 'error' and run.node == 'Stop: Bad Settings' and 'CLICKUP_LIST_ID is missing' in run.error
              and 'SUPPLIERS_JSON is not valid JSON' in run.error)
        results.append((s, 'stopped before any API call, both problems named in the error',
                        ok and not [e for e in run.log if e['svc'] != 'other'], f'ended at {run.node}: {short(run.error, 90)}'))
    finally:
        api('POST', f'/api/v1/workflows/{wid}/deactivate')
        api('DELETE', f'/api/v1/workflows/{wid}')


def no_slack(results):
    """A copy with SLACK_WEBHOOK_URL empty: the ClickUp side works the same, nothing is posted."""
    env = dict(ENV, SLACK_WEBHOOK_URL='')
    wid = import_workflow(test_copy(WORKFLOW, env, PATH + '-no-slack'))
    try:
        api('POST', f'/api/v1/workflows/{wid}/activate')
        time.sleep(1.5)
        r = Runner(wid, PATH + '-no-slack')
        run = r.run_now()
        results.append(('Slack not set (SLACK_WEBHOOK_URL empty)', 'tasks still created and updated, nothing posted, run ends at Done',
                        run.status == 202 and len(run.creates()) == 3 and len(run.writes()) == 7 and not run.calls('slack')
                        and run.state == 'success' and run.node == 'Done' and 'Slack: Post Digest' not in run.run_data,
                        f'{len(run.writes())} writes, {len(run.calls("slack"))} Slack calls, ended at {run.node}'))
    finally:
        api('POST', f'/api/v1/workflows/{wid}/deactivate')
        api('DELETE', f'/api/v1/workflows/{wid}')


def schedule(results):
    """A copy that runs every minute, with a static Shopify token and no Run Now secret."""
    env = dict(ENV, STOCK_CHECK_CRON='* * * * *', SHOPIFY_ACCESS_TOKEN=STATIC_TOKEN, SHOPIFY_CLIENT_ID='', SHOPIFY_CLIENT_SECRET='',
               RUN_NOW_SECRET='')
    wid = import_workflow(test_copy(WORKFLOW, env, PATH + '-schedule'))
    try:
        while time.localtime().tm_sec > 45:   # keep the Run Now calls clear of the minute the schedule fires
            time.sleep(1)
        api('POST', f'/api/v1/workflows/{wid}/activate')
        time.sleep(1.5)
        r = Runner(wid, PATH + '-schedule')
        refused = [r.run_now(secret) for secret in ('', 'anything')]
        results.append(('Run Now with RUN_NOW_SECRET not set', 'every call refused with 401 (fails closed)',
                        all(x.status == 401 and x.node == 'Respond: Unauthorized' and not x.log for x in refused),
                        ', '.join(f'{x.status} {x.node}' for x in refused)))
        mark = len(LOG)
        end, ex = time.time() + 80, None
        while time.time() < end and not ex:
            ex = next((e for e in r.executions() if e.get('mode') == 'trigger'), None)
            time.sleep(1)
        api('POST', f'/api/v1/workflows/{wid}/deactivate')   # one scheduled run is enough
        if not ex:
            results.append(('Daily schedule (static access token)', 'the schedule started a run', False, 'no run within 80 seconds'))
            return
        r.sent += 1
        while not ex.get('stoppedAt'):
            time.sleep(0.5)
            ex = next(e for e in r.executions() if e['id'] == ex['id'])
        run = Run(None, None, ex, LOG[mark:], 0, 0)
        gql = run.calls('shopify', 'POST')
        results.append(('Daily schedule (static access token)', 'the schedule started a full run that posted the digest',
                        run.mode == 'trigger' and run.state == 'success' and run.node == 'Done' and len(run.digests()) == 1 and '(scheduled)' in run.digests()[0]
                        and 'Respond: Started' in run.run_data,   # the Run Now reply node does nothing in a scheduled run
                        f'mode {run.mode}, ended at {run.node}, passed the reply node'))
        results.append(('Daily schedule (static access token)', 'used SHOPIFY_ACCESS_TOKEN, no token request',
                        gql and all(e['headers'].get('x-shopify-access-token') == STATIC_TOKEN for e in gql) and not run.calls('shopify_token'),
                        f'{len(gql)} GraphQL requests'))
    finally:
        try:
            api('POST', f'/api/v1/workflows/{wid}/deactivate')
        finally:
            api('DELETE', f'/api/v1/workflows/{wid}')


def main_scenarios(r):
    # 1. Run Now security
    for label, secret in (('no secret header', None), ('wrong secret', 'guess')):
        run = r.run_now(secret)
        r.check(f'Run Now, {label}', 'refused with 401, nothing called',
                run.status == 401 and run.node == 'Respond: Unauthorized' and not run.log, f'{run.status}, ended at {run.node}')

    # 2. First run
    M['throttle_once'].add(1)   # Shopify throttles page 2 once
    M['low_budget'].add(2)      # after page 3 the query budget is nearly empty
    run = r.run_now()
    s = 'First run'
    r.check(s, 'Run Now answers 202 right away', run.status == 202 and json.loads(run.body).get('status') == 'started', f'{run.status} {run.body[:40]}')
    tok = run.calls('shopify_token')
    form = parse_qs(tok[0]['raw'].decode()) if tok else {}
    r.check(s, 'one client credentials token request, form encoded',
            len(tok) == 1 and tok[0]['headers'].get('content-type', '').startswith('application/x-www-form-urlencoded')
            and form == {'grant_type': ['client_credentials'], 'client_id': [CLIENT_ID], 'client_secret': [CLIENT_SECRET]}, f'{len(tok)} request(s)')
    gql = run.calls('shopify', 'POST')
    afters = [e['body']['variables'].get('after') for e in gql]
    cursors = [None, cursor_for(201), cursor_for(401), cursor_for(701), cursor_for(1101)]   # 3 per page, the draft is filtered out
    r.check(s, 'GraphQL paged with the cursor over 5 pages, page 2 asked twice',
            afters == [cursors[0], cursors[1], cursors[1], cursors[2], cursors[3], cursors[4]], f'{len(gql)} requests')
    v = gql[0]['body']['variables'] if gql else {}
    token = M['tokens'] and sorted(M['tokens'])[0]
    r.check(s, 'GraphQL request: versioned URL, new token, variables',
            all(e['path'] == f'/{SHOP}/admin/api/{API_VERSION}/graphql.json' and e['headers'].get('x-shopify-access-token') == token for e in gql)
            and v == {'first': 3, 'after': None, 'query': 'product_status:ACTIVE', 'ns': 'custom', 'key': 'reorder_point'}, json.dumps(v))
    # The throttled answer says 9 more points are needed at 3 per second: 3 seconds, not the 2 second fallback
    r.check(s, 'THROTTLED page: waited the time the budget needs (3 s), then asked for the same page',
            len(gql) == 6 and 2.8 <= gql[2]['t'] - gql[1]['t'] < 4.5, f'{gql[2]["t"] - gql[1]["t"]:.1f}s pause' if len(gql) > 2 else '')
    # After page 3 only 5 points are left at 5 per second and the next page needs 11: a 2 second pause
    r.check(s, 'low query budget: paused about 2 s before the next page', len(gql) == 6 and 1.8 <= gql[4]['t'] - gql[3]['t'] < 3.5,
            f'{gql[4]["t"] - gql[3]["t"]:.1f}s pause' if len(gql) > 4 else '')
    scan = run.scans()
    r.check(s, 'ClickUp open tasks read on 2 pages with the right filters and token',
            [e['query'].get('page') for e in scan] == [['0'], ['1']]
            and all(e['query'].get('include_closed') == ['false'] and e['query'].get('archived') == ['false']
                    and e['query'].get('subtasks') == ['true']
                    and e['headers'].get('authorization') == CLICKUP_TOKEN and e['path'] == f'/api/v2/list/{LIST_ID}/task' for e in scan),
            f'{len(scan)} requests')
    cu = [e for e in run.log if e['svc'] == 'clickup']
    pairs = []
    for i, e in enumerate(cu):
        if e['method'] == 'POST' and re.fullmatch(r'/api/v2/list/\w+/task', e['path']):
            prev = cu[i - 1] if i else None
            sku = e['body']['custom_fields'][0]['value']
            pairs.append(prev is not None and prev['method'] == 'GET' and prev['path'] == f'/api/v2/list/{LIST_ID}/task'
                         and json.loads(prev['query']['custom_fields'][0]) == [{'field_id': SKU_FIELD, 'operator': '==', 'value': sku}]
                         and prev['query'].get('include_closed') == ['false'] and prev['query'].get('archived') == ['false']
                         and prev['headers'].get('authorization') == CLICKUP_TOKEN)
    r.check(s, 'right before each create, ClickUp was asked for an open task with exactly that SKU',
            len(pairs) == 3 and all(pairs) and len(run.checks()) == 3, f'{len(run.checks())} look-ups, {sum(pairs)} right before a create')
    creates = run.creates()
    by_sku = {next(f['value'] for f in e['body']['custom_fields']): e['body'] for e in creates}
    r.check(s, 'three new tasks, only for low SKUs without an open task', sorted(by_sku) == ['MUG-BLK-12', 'MUG-WHT-12', 'PLANT-POT-S'] and len(creates) == 3,
            ', '.join(sorted(by_sku)))
    mug = by_sku.get('MUG-BLK-12', {})
    lo, hi = int(run.started * 1000) + 21 * DAY_MS - 5000, int(run.ended * 1000) + 21 * DAY_MS
    r.check(s, 'out of stock task: name, urgent, due in 21 days, assignee, tag, SKU field',
            mug.get('name') == 'Reorder MUG-BLK-12: Stoneware Mug (Black / 12 oz)' and mug.get('priority') == 1 and lo <= mug.get('due_date', 0) <= hi
            and mug.get('due_date_time') is False and mug.get('assignees') == [ASSIGNEE] and mug.get('tags') == ['low-stock']
            and mug.get('custom_fields') == [{'id': SKU_FIELD, 'value': 'MUG-BLK-12'}], short(mug.get('name', '')))
    d = mug.get('description', '')
    r.check(s, 'description has stock numbers, supplier, lead time and the Shopify link',
            all(x in d for x in ('On hand: 0', 'Reorder point: 12\n', 'Supplier: Harbor Ceramics Co., orders@harborceramics.example.com',
                                 'Lead time: 21 days', 'https://admin.shopify.com/store/driftwood-test/products/1001/variants/101')), short(d.splitlines()[0] if d else ''))
    pot = by_sku.get('PLANT-POT-S', {})
    lo7, hi7 = int(run.started * 1000) + 7 * DAY_MS - 5000, int(run.ended * 1000) + 7 * DAY_MS
    r.check(s, 'SKU prefix supplier, default reorder point after a broken metafield, high priority',
            pot.get('priority') == 2 and lo7 <= pot.get('due_date', 0) <= hi7 and 'Supplier: Greenline Pottery' in pot.get('description', '')
            and 'Reorder point: 10 (store default)' in pot.get('description', '') and by_sku.get('MUG-WHT-12', {}).get('priority') == 2,
            'PLANT-POT-S: 7 days, priority 2')
    ced = run.comments('86b0ced01')
    r.check(s, 'stock fell below the task description: one comment', len(ced) == 1 and ced[0]['body'] ==
            {'comment_text': 'Stock dropped from 9 to 7 on hand (reorder point 8).', 'notify_all': False}, short(ced[0]['body']['comment_text']) if ced else 'none')
    twl = [e for e in run.writes() if '86b0twl01' in e['path']]
    r.check(s, 'restocked SKU: task closed first, then a comment',
            [(e['method'], e['body']) for e in twl] == [('PUT', {'status': 'complete'}), ('POST', {'comment_text':
             'Back in stock: 40 on hand, above the reorder point of 10. Closed automatically by the low stock check.', 'notify_all': False})]
            and M['tasks']['86b0twl01']['status'] == 'complete', f'{len(twl)} calls')
    r.check(s, 'task made by hand: found by its name, priority raised to high, no comment',
            [e['body'] for e in run.updates('86b0snd01')] == [{'priority': 2}] and not run.comments('86b0snd01'), 'CANDLE-SND')
    touched = {e['path'].split('/')[4] for e in run.writes() if '/task/' in e['path']}
    r.check(s, 'nothing else changed (7 writes in total)', len(run.writes()) == 7 and touched == {'86b0ced01', '86b0twl01', '86b0snd01'},
            f'{len(run.writes())} writes')
    text = (run.digests() or [''])[0]
    r.check(s, 'one Slack digest with every section', len(run.digests()) == 1 and all(x in text for x in (
        '(run now)', 'Checked 13 variants on 5 pages: 7 low, 1 back in stock. Skipped 1 not tracked, 1 without a SKU.',
        '*New reorder tasks (3)*', '• MUG-BLK-12 Stoneware Mug (Black / 12 oz): 0 on hand, reorder point 12. Urgent priority, Harbor Ceramics Co., due ',
        '*Updated (2)*', 'CANDLE-CED Cedar Candle: dropped from 9 to 7 on hand (reorder point 8).',
        'CANDLE-SND Sandalwood Candle: priority raised to high (2 on hand, reorder point 10).',
        '*Restocked and closed (1)*', 'TOWEL-GRY Bath Towel (Gray): 40 on hand', '*Still low, no change (2)*',
        '*Needs a look (4)*', 'PLANT-POT-S: the reorder point metafield is "abc"', 'VASE-CLR has 2 open reorder tasks',
        'TOWEL-WHT is used on 2 variants. Their stock was added up: 25 on hand.',
        'LAMP-BRASS has an open reorder task, but no tracked Shopify variant'))
            and '<https://app.clickup.com/t/' in text, text.splitlines()[1] if text.count('\n') else text)
    r.check(s, 'run ended successfully at Done', run.state == 'success' and run.node == 'Done', f'{run.state}, ended at {run.node}')
    r.check(s, 'no secret anywhere in the run data or the digest',
            run.secrets_hidden(token) and not any(x in text for x in (CLICKUP_TOKEN, CLIENT_ID, CLIENT_SECRET, str(token))),
            'tokens stay in $env; the issued Shopify token only in its own node')

    # 3. Nothing changed since
    run = r.run_now()
    s = 'Second run, no stock change'
    text = (run.digests() or [''])[0]
    r.check(s, 'no new tasks, comments or updates (idempotent)', not run.writes() and len(run.calls('shopify', 'POST')) == 5, f'{len(run.writes())} writes')
    r.check(s, 'digest lists the 7 low SKUs as still low', '*Still low, no change (7)*' in text and '*New reorder tasks' not in text
            and '*Updated' not in text and run.node == 'Done', short(text.splitlines()[1] if text.count('\n') else text))

    # 4. Stock drops, ClickUp answers 429 three times, Shopify answers 429 three times
    qty('MUG-WHT-12', 0)
    qty('CANDLE-SND', 1)
    qty('RUG-JUTE', 30)
    M['clickup_429'] = 3
    M['shopify_429'] = 3
    run = r.run_now()
    s = 'Stock drops, ClickUp and Shopify rate limits'
    gql = run.calls('shopify', 'POST')
    r.check(s, 'Shopify 429: retried, waited, then the same first page again',
            len(gql) == 8 and [e['body']['variables']['after'] for e in gql[:4]] == [None] * 4 and run.node == 'Done', f'{len(gql)} GraphQL requests')
    wht = task_for('MUG-WHT-12')
    tries = run.comments(wht['id']) if wht else []
    r.check(s, 'ClickUp 429: 3 quick tries, a wait, then the same comment once',
            len(tries) == 4 and tries[3]['t'] - tries[2]['t'] >= 1.9 and M['comments'].get(wht['id']) == [
                'Stock dropped from 5 to 0 on hand (reorder point 10). It is now out of stock. Priority raised to urgent.'],
            f'{len(tries)} attempts, {tries[3]["t"] - tries[2]["t"]:.1f}s wait before the last' if len(tries) == 4 else f'{len(tries)} attempts')
    r.check(s, 'out of stock now: priority raised to urgent', wht and [e['body'] for e in run.updates(wht['id'])] == [{'priority': 1}] and wht['priority'] == 1,
            'MUG-WHT-12')
    snd = run.comments('86b0snd01')
    r.check(s, 'drop measured from the number saved in the last run', [e['body']['comment_text'] for e in snd] == [
        'Stock dropped from 2 to 1 on hand (reorder point 10).'] and not run.updates('86b0snd01'), 'CANDLE-SND 2 to 1')
    r.check(s, 'restocked task closed', [e['body'] for e in run.updates('86b0rug01')] == [{'status': 'complete'}]
            and len(run.comments('86b0rug01')) == 1 and M['tasks']['86b0rug01']['status'] == 'complete', 'RUG-JUTE')
    text = (run.digests() or [''])[0]
    r.check(s, 'digest shows the updates', all(x in text for x in (
        'MUG-WHT-12 Stoneware Mug (White / 12 oz): dropped from 5 to 0 on hand, now out of stock, priority raised to urgent (reorder point 10).',
        'CANDLE-SND Sandalwood Candle: dropped from 2 to 1 on hand (reorder point 10).', '*Restocked and closed (1)*', '*Still low, no change (4)*')),
            short(text.splitlines()[1] if text.count('\n') else text))

    # 5. ClickUp fails one create for good
    qty('TOWEL-WHT', 0)
    qty('SOAP-LAV', 3)
    M['fail_create'].add('TOWEL-WHT')
    run = r.run_now()
    s = 'ClickUp fails one create'
    alert = error_alert(run.started)
    text = (run.digests() or [''])[0]
    r.check(s, 'the failing create was tried 3 times, the next change still went through',
            len(run.creates('TOWEL-WHT')) == 3 and len(run.creates('SOAP-LAV')) == 1 and len(open_tasks('SOAP-LAV')) == 1, f"{len(run.creates('TOWEL-WHT'))} attempts")
    soap = run.creates('SOAP-LAV')[0]['body'] if run.creates('SOAP-LAV') else {}
    lo, hi = int(run.started * 1000) + 14 * DAY_MS - 5000, int(run.ended * 1000) + 14 * DAY_MS
    r.check(s, 'default supplier and default lead time', 'Supplier: Driftwood Wholesale, buying@driftwood-wholesale.example.com' in soap.get('description', '')
            and lo <= soap.get('due_date', 0) <= hi and soap.get('priority') == 2, 'SOAP-LAV: 14 days')
    r.check(s, 'digest still posted, with the failure', '*New reorder tasks (1)*' in text and '*Could not update ClickUp (1)*' in text
            and 'TOWEL-WHT: could not create the reorder task (HTTP 500' in text and 'It is tried again on the next run.' in text, 'failure listed')
    r.check(s, 'run ended as failed after the digest, error workflow alerted',
            run.state == 'error' and run.node == 'Stop: ClickUp Changes Failed' and NAME in alert and 'TOWEL-WHT' in alert,
            f'ended at {run.node}; alert: {short(alert, 60)}')
    r.check(s, 'the error items of the failed calls hold no secret', run.secrets_hidden(sorted(M['tokens'])[-1]), 'checked all run data')

    # 6. ClickUp is back
    M['fail_create'].clear()
    run = r.run_now()
    s = 'ClickUp is back'
    r.check(s, 'the failed task is created once, nothing is doubled',
            len(run.creates()) == 1 and len(run.creates('TOWEL-WHT')) == 1 and not run.comments(open_tasks('SOAP-LAV')[0]['id'])
            and run.node == 'Done', f'{len(run.creates())} create(s)')
    counts = {sku: len(open_tasks(sku)) for sku in ('MUG-BLK-12', 'MUG-WHT-12', 'CANDLE-CED', 'PLANT-POT-S', 'CANDLE-SND', 'SOAP-LAV', 'TOWEL-WHT', 'TOWEL-GRY', 'RUG-JUTE')}
    r.check(s, 'exactly one open task per low SKU after all runs', counts == {**{k: 1 for k in counts}, 'TOWEL-GRY': 0, 'RUG-JUTE': 0},
            ', '.join(f'{k} {n}' for k, n in counts.items()))

    # 7. Someone (or a second run) opens a task while this run is scanning Shopify
    qty('RUG-JUTE', 2)     # low again, its old task is closed
    qty('TOWEL-GRY', 4)    # low again, its old task is closed

    def task_appears():
        M['tasks']['86b0ext01'] = dict(id='86b0ext01', name='Reorder RUG-JUTE: Jute Rug (2 x 3 ft)', sku='RUG-JUTE', status='to do', priority=2,
                                       description='SKU: RUG-JUTE\nOn hand: 2\nReorder point: 6', created=int(time.time() * 1000), due=None,
                                       assignees=[ASSIGNEE], tags=['low-stock'])
    M['on_graphql'] = task_appears
    run = r.run_now()
    s = 'A task appears in ClickUp while the run is going'
    text = (run.digests() or [''])[0]
    r.check(s, 'the look-up right before the create found it: no second task',
            len(run.checks('RUG-JUTE')) == 1 and not run.creates('RUG-JUTE') and [t['id'] for t in open_tasks('RUG-JUTE')] == ['86b0ext01'],
            f"{len(run.creates('RUG-JUTE'))} create(s) for RUG-JUTE")
    cu = [e for e in run.log if e['svc'] == 'clickup']
    grey_check = next((i for i, e in enumerate(cu) if e in run.checks('TOWEL-GRY')), None)
    grey_create = next((i for i, e in enumerate(cu) if e in run.creates('TOWEL-GRY')), None)
    r.check(s, 'the other low SKU still got its task, right after its own look-up',
            len(run.checks('TOWEL-GRY')) == 1 and len(run.creates('TOWEL-GRY')) == 1 and grey_check is not None and grey_create == grey_check + 1
            and len(open_tasks('TOWEL-GRY')) == 1 and len(run.writes()) == 1 and run.state == 'success' and run.node == 'Done',
            f'{len(run.writes())} write(s), ended at {run.node}')
    r.check(s, 'the digest says why no task was created',
            '*New reorder tasks (1)*' in text and '• TOWEL-GRY Bath Towel (Gray): 4 on hand, reorder point 10.' in text
            and 'RUG-JUTE is low (2 on hand, reorder point 6), but an open reorder task for it was made while this run was going, '
                'so no second task was created. <https://app.clickup.com/t/86b0ext01|Open task>' in text, 'listed under Needs a look')

    # 8. ClickUp answers 429 to the task read: wait until X-RateLimit-Reset, then read again
    M['clickup_read_429'] = 3   # the node's own 3 tries all get a 429
    run = r.run_now()
    s = 'ClickUp rate limits the task read'
    scan = run.scans()
    gap = scan[3]['t'] - scan[2]['t'] if len(scan) > 3 else 0
    r.check(s, 'waited for the reset time ClickUp gave (4 s, not the 2 s fallback), then read every page',
            [e['query'].get('page') for e in scan] == [['0'], ['0'], ['0'], ['0'], ['1']] and 3.5 <= gap < 8, f'{len(scan)} reads, {gap:.1f}s wait')
    r.check(s, 'then the run went on as normal and changed nothing (idempotent)',
            not run.writes() and not run.checks() and len(run.digests()) == 1 and run.state == 'success' and run.node == 'Done',
            f'{len(run.writes())} writes, ended at {run.node}')

    # 9. Shopify and ClickUp failures: nothing may change
    for s, switch, node, needle, gql_count in (
            ('Shopify denies access (missing scope)', 'access_denied', 'Stop: Shopify Failed',
             'ACCESS_DENIED: Access denied for productVariants field. Required access: `read_products` access scope. Nothing was changed in ClickUp.', 1),
            ('Shopify keeps throttling', 'throttle_forever', 'Stop: Shopify Failed', 'kept throttling the stock scan (THROTTLED) after 5 waits', 6),
            ('Shopify is down (503)', 'shopify_503', 'Stop: Shopify Failed', 'HTTP 503', 3),
            ('Shopify refuses the client credentials', 'token_fail', 'Stop: Shopify Auth Failed', 'did not give an access token', 0),
            ('ClickUp refuses the token while reading tasks', 'scan_fail', 'Stop: ClickUp Failed', 'HTTP 401, Token invalid (OAUTH_025)', 0)):
        M[switch] = True
        run = r.run_now()
        M[switch] = False
        alert = error_alert(run.started)
        r.check(s, 'stopped with a clear error, no ClickUp change, no digest, alert sent',
                run.state == 'error' and run.node == node and needle in run.error and not run.writes() and not run.digests() and node in alert
                and len(run.calls('shopify', 'POST')) == gql_count, f'ended at {node}: {short(run.error, 70)}')
        if switch == 'token_fail':
            r.check(s, 'token request retried 3 times', len(run.calls('shopify_token')) == 3, f"{len(run.calls('shopify_token'))} requests")
        if switch == 'scan_fail':
            r.check(s, 'Shopify never called', not run.calls('shopify_token') and len(run.calls('clickup', 'GET')) == 3, f"{len(run.calls('clickup', 'GET'))} reads")

    # ClickUp answers 429 to every read: 3 waits, then a clear stop (the loop must end)
    M['clickup_read_429'] = 10 ** 6
    run = r.run_now()
    M['clickup_read_429'] = 0
    s = 'ClickUp keeps rate limiting the task read'
    alert = error_alert(run.started)
    reads = run.scans()
    r.check(s, 'gave up after 3 waits: stopped with HTTP 429, Shopify never called, nothing changed, alert sent',
            run.state == 'error' and run.node == 'Stop: ClickUp Failed' and 'HTTP 429, the rate limit was still reached after 3 waits' in run.error
            and len(reads) == 12
            and not run.calls('shopify_token') and not run.calls('shopify') and not run.writes() and not run.digests()
            and 'Stop: ClickUp Failed' in alert, f'{len(reads)} reads, ended at {run.node}: {short(run.error, 60)}')

    # 10. Slack is down
    M['slack_fail'] = True
    run = r.run_now()
    M['slack_fail'] = False
    r.check('Slack is down', 'digest retried 3 times, run flagged for the alert',
            len([e for e in run.log if e['svc'] == 'slack' and str(e['body'].get('text', '')).startswith('*Low stock check*')]) == 3
            and run.state == 'error' and run.node == 'Stop: Slack Failed' and not run.writes()
            and run.error == 'Could not post the stock digest to Slack (HTTP 500). The ClickUp changes were made.', f'ended at {run.node}')

    # 11. ClickUp answers 429 to every change: 3 waits per change, then it is reported, not dropped silently
    qty('CANDLE-CED', 6)   # dropped from 7: a comment is due
    before = list(M['comments'].get('86b0ced01', []))
    M['clickup_429'] = 10 ** 6
    run = r.run_now()
    M['clickup_429'] = 0
    s = 'ClickUp keeps rate limiting a change'
    alert = error_alert(run.started)
    text = (run.digests() or [''])[0]
    tries = run.comments('86b0ced01')
    r.check(s, '3 quick tries, then 3 more after each of 3 waits, then the change is given up for this run',
            len(tries) == 12 and len(run.writes()) == 12 and M['comments'].get('86b0ced01', []) == before, f'{len(tries)} attempts')
    r.check(s, 'digest and alert say the rate limit was the reason, run failed',
            'CANDLE-CED: could not add the stock comment (HTTP 429, the rate limit was still reached after 3 waits). It is tried again on the next run.' in text
            and '*Updated' not in text and run.state == 'error' and run.node == 'Stop: ClickUp Changes Failed' and 'CANDLE-CED' in alert,
            f'ended at {run.node}')


def main():
    if not KEY:
        raise SystemExit('Set N8N_API_KEY first.')
    server = ThreadingHTTPServer(('127.0.0.1', MOCK_PORT), Mock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    results, created = [], []
    try:
        reset_mock()
        bad_settings(results)
        reset_mock()
        no_slack(results)
        reset_mock()
        schedule(results)
        reset_mock()
        err_id = import_workflow(test_copy(ERROR_WORKFLOW, ENV, PATH + '-error'))
        created.append(err_id)
        api('POST', f'/api/v1/workflows/{err_id}/activate')   # n8n 2.x only runs an error workflow that is active
        wid = import_workflow(test_copy(WORKFLOW, ENV, PATH), {'executionOrder': 'v1', 'errorWorkflow': err_id})
        created.append(wid)
        api('POST', f'/api/v1/workflows/{wid}/activate')
        time.sleep(2)
        r = Runner(wid, PATH)
        r.results = results
        main_scenarios(r)
    finally:
        for wid in reversed(created):
            try:
                api('POST', f'/api/v1/workflows/{wid}/deactivate')
            except Exception:
                pass
            api('DELETE', f'/api/v1/workflows/{wid}')
        server.shutdown()

    passed = sum(ok for _, _, ok, _ in results)
    last = None
    for scenario, label, ok, detail in results:
        if scenario != last:
            print(f'\n{scenario}')
            last = scenario
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f'  ({detail})' if detail else ''))
    print(f'\n{passed}/{len(results)} checks passed')
    sys.exit(0 if results and passed == len(results) else 1)


if __name__ == '__main__':
    main()
