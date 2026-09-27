"""Deterministic fixture repository for jevify trigger evals.

build(root) creates a small git repository with:
- HEAD: one commit whose message explains most, but not all, of its changed hunks
- 28 call sites of billing.rates.fetch_rate, 7 still passing the ignored legacy= flag
- logs/worker.log: 400 mixed log lines
- review/findings.jsonl: 28 review findings about the HEAD commit

build(root, scale=N), 2 <= N <= MAX_SCALE, tells the same story N times larger, where reading
every item directly is costly: 7N billing modules whose 28N line items the rounding fix all
touches, unexplained drift in about 4% of HEAD's hunks, 28N review findings that each cite a
line-item function, 400N log lines with 5 + 3N kinds of ERROR/WARN line, and 28N fetch_rate
call sites. Scale 1 is byte-identical to the fixture of every recorded result.

answer_keys(root) derives the ground truth that evals/grade.py scores final answers against,
at whatever scale root was built.
"""

import ast
from datetime import datetime, timedelta, timezone
import json
import random
import re
import subprocess
from pathlib import Path

MODULES = ["invoices", "refunds", "payouts", "credits", "subscriptions", "taxes", "fees"]
COMMIT_SUBJECT = "fix(billing): round currency amounts half-even instead of truncating"
COMMIT_BODY = (
    "Amounts were truncated to cents with int(x * 100) / 100, which under-charged\n"
    "customers by up to a cent per line item. Every money computation now goes\n"
    "through billing.money.round_cents, which uses Decimal ROUND_HALF_EVEN.\n"
)
# Changes HEAD makes that its commit message does not explain (settings.py is the other one).
DRIFT = {
    "refunds": ["def refunds_window_days():", "    return 45", ""],
    "payouts": ["def payouts_batch_size():", "    return 500", ""],
}
LOG_KINDS = [
    ("INFO", "invoice {n} rendered in {ms}ms"),
    ("INFO", "payout batch {n} queued"),
    ("WARN", "rate cache miss for {cur}, refetching"),
    ("WARN", "retrying webhook delivery {n} (attempt {a})"),
    ("ERROR", "payout {n} failed: insufficient balance"),
    ("ERROR", "refund {n} failed: upstream timeout after {ms}ms"),
    ("ERROR", "invoice {n} total mismatch: expected {e} got {g}"),
]
# ERROR/WARN template -> real bug (True), transient noise (False), or debatable (None).
LOG_BUG = {
    "rate cache miss for {cur}, refetching": False,
    "retrying webhook delivery {n} (attempt {a})": False,
    "payout {n} failed: insufficient balance": None,
    "refund {n} failed: upstream timeout after {ms}ms": None,
    "invoice {n} total mismatch: expected {e} got {g}": True,
}
FINDING_CLAIMS = [
    "round_cents is called on a float product; precision may still be lost before rounding.",
    "fetch_rate is called with legacy=True, which is ignored by the current implementation.",
    "Missing test for negative amounts in this line-item function.",
    "Function name does not follow the module naming convention.",
]
HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
RATES_SOURCE = ("RATES = {'USD': 1.0, 'EUR': 0.92, 'GBP': 0.79}\n\n\n"
                "def fetch_rate(currency, legacy=False):\n"
                "    return RATES[currency]\n")
MONEY_BASE = "def round_cents(value):\n    return int(value * 100) / 100\n"
MONEY_HEAD = ("from decimal import ROUND_HALF_EVEN, Decimal\n\n\n"
              "def round_cents(value):\n"
              "    return float(Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN))\n")
SETTINGS_BASE = "LOG_LEVEL = 'INFO'\nHTTP_TIMEOUT_SECONDS = 30\nRETRY_LIMIT = 3\n"
SETTINGS_HEAD = "LOG_LEVEL = 'DEBUG'\nHTTP_TIMEOUT_SECONDS = 5\nRETRY_LIMIT = 3\n"

# Scaled fixtures (scale >= 2): module names are <region>_<module>, 7 per unit of scale.
MAX_SCALE = 16
REGIONS = ["us", "eu", "uk", "ca", "au", "jp", "br", "mx", "sg", "ch", "se", "nz", "za", "kr", "hk", "pl"]
# Unexplained changes, one per module, injected in this order (cycling): scale N injects
# max(2, round(1.12 * N)), which with the settings.py hunk is about 4% of HEAD's hunks.
DRIFT_KINDS = ["early_return", "constant", "reorder", "pii_log", "widen_except", "swap_args", "half_up",
               "threshold", "feature_flag", "boundary", "new_helper", "default", "abs"]
# (base, HEAD) wordings of the comment above a money line and of a docstring's rounding line:
# the rewording the message explains, varied so that no single pattern filters it out.
COMMENT_PAIRS = [
    ("{name} settles in whole cents: truncate the remainder.",
     "{name} settles in whole cents: round the remainder half-even."),
    ("drop sub-cent digits before settling.", "ties at half a cent go to the even cent before settling."),
    ("anything below one cent is cut off here.", "take the nearest cent here; exact halves go to the even one."),
    ("settle toward zero, whole cents only.", "settle at the nearest whole cent (banker's rule)."),
]
DOC_PAIRS = [
    ("Truncates to whole cents.", "Rounds half-even to whole cents."),
    ("Sub-cent digits are discarded.", "Half-cent ties go to the even cent."),
    ("Fractions of a cent are dropped.", "Uses banker's rounding for fractions of a cent."),
]
# Review claim kind -> wordings. CODE_CLAIMS are true or false of the cited function's code.
SCALED_CLAIMS = {
    "legacy": ["fetch_rate is called with legacy=True, which is ignored by the current implementation.",
               "This passes legacy=True to fetch_rate; the flag is dead and should be dropped here."],
    "order": ["The amount is rounded to cents before it is multiplied by the rate, so the converted "
              "total is never rounded.",
              "Rounds before converting: round_cents gets the unconverted amount and the product with the "
              "rate is returned unrounded."],
    "product": ["round_cents is called on a float product; precision may still be lost before rounding.",
                "The float multiplication happens before round_cents, so binary representation error can "
                "flip a half-even tie."],
    "usd": ["fetch_rate is always called with 'USD' here, so the currency argument is ignored.",
            "The rate is hard-coded to USD; documents in other currencies are converted at the wrong rate."],
    "pii": ["This logs the customer's email address, which is PII and must not reach the logs."],
    "swallow": ["The except clause catches every exception and silently falls back to a rate of 1.0."],
    "truncates": ["This still truncates with int(x * 100) / 100 instead of rounding."],
    "import": ["round_cents is used here but never imported."],
    "naming": ["Function name does not follow the module naming convention."],
    "hints": ["Add type hints to the parameters and the return value."],
    "rename": ["Rename rate to exchange_rate for readability."],
    "test": ["Missing test for negative amounts in this line-item function."],
}
CODE_CLAIMS = ("legacy", "order", "product", "usd", "pii", "swallow")
CLAIM_WEIGHTS = {"legacy": 18, "order": 14, "product": 10, "usd": 10, "pii": 3, "swallow": 3,
                 "truncates": 8, "import": 5, "naming": 7, "hints": 8, "rename": 6, "test": 8}
CLAIM_LINE = {"legacy": "call", "usd": "call", "rename": "call", "order": "money", "product": "money",
              "truncates": "money", "import": "money", "pii": "log", "swallow": "except",
              "naming": "def", "hints": "def", "test": "def"}
# (level, template, verdict) kinds a scaled log adds to LOG_KINDS, taken in order.
LOG_BUGS_EXTRA = [
    ("ERROR", "payout {n} settled with negative amount {neg} {cur}", True),
    ("WARN", "charge {cid} captured again for order {n}", True),
    ("ERROR", "ledger entry {n} unbalanced: debit {e} credit {g}", True),
    ("WARN", "retrying ledger write {n} (attempt {big} of 3)", True),
    ("ERROR", "credit note {n} of {g} exceeds its invoice total {e}", True),
    ("WARN", "subscription {n} renewed twice in period {period}", True),
    ("ERROR", "invoice {n} issued with currency None", True),
    ("ERROR", "payout {n} sent without a ledger debit", True),
    ("WARN", "tax {g} exceeds subtotal {e} on invoice {n}", True),
    ("ERROR", "refund {n} issued twice for charge {cid}", True),
]
LOG_DEBATABLE_EXTRA = [
    ("WARN", "card declined for customer {n}: {decline}", None),
    ("WARN", "fx rate for {cur} is {age}s old, using it anyway", None),
    ("ERROR", "tax service rejected invoice {n} with HTTP 422", None),
    ("WARN", "dispute opened on charge {cid}", None),
]
LOG_INFO_EXTRA = [
    ("INFO", "subscription {n} renewed for {cur}", None),
    ("INFO", "webhook {n} delivered in {ms}ms", None),
    ("INFO", "fx rates refreshed ({cur} base)", None),
    ("INFO", "charge {cid} captured for order {n}", None),
]
# Transient noise kinds: every service with every pattern, in a fixed shuffled order.
NOISE_SERVICES = ["tax-api", "fx-feed", "ledger-db", "webhooks", "pdf-renderer", "email-relay",
                  "risk-score", "search-index"]
NOISE_PATTERNS = [
    ("WARN", "{svc} request {n} timed out after {ms}ms, retrying"),
    ("WARN", "{svc} returned 503, retry {a} of 3 in {ms}ms"),
    ("WARN", "{svc} rate limited, backing off {ms}ms"),
    ("WARN", "{svc} cache miss for key {n}, refetching"),
    ("ERROR", "{svc} connection reset, reconnecting (attempt {a})"),
    ("ERROR", "{svc} health check failed once, next check in {ms}ms"),
]


def _git(root, *args):
    return subprocess.run(
        ["git", "-c", "user.name=jevify-eval", "-c", "user.email=eval@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
        cwd=root, check=True, capture_output=True, text=True).stdout


def _module(name, rounded, drift):
    lines = [
        "from billing.money import round_cents",
        "from billing.rates import fetch_rate",
        "",
        "",
    ]
    for i in range(4):
        amount = f"amount_{i}"
        expr = f"round_cents({amount} * rate)" if rounded else f"int({amount} * rate * 100) / 100"
        legacy = ", legacy=True" if (i + len(name)) % 3 == 0 else ""
        lines += [
            f"def {name}_line_{i}({amount}, currency):",
            f'    """Price line item {i} of a {name} document.',
            "",
            "    The amount is in the document currency; the rate converts it to the",
            "    settlement currency before rounding to cents.",
            '    """',
            f"    rate = fetch_rate(currency{legacy})",
            f"    return {expr}",
            "",
            "",
        ]
    if drift:
        lines += drift
    return "\n".join(lines).rstrip() + "\n"


def build(root, scale=1):
    if not isinstance(scale, int) or not 1 <= scale <= MAX_SCALE:
        raise ValueError(f"scale must be an integer from 1 to {MAX_SCALE}, not {scale!r}")
    root = Path(root)
    (root / "billing").mkdir(parents=True)
    base, head = _small_files() if scale == 1 else _scaled_files(scale)
    _write(root, base)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "feat(billing): initial billing modules, sample logs and review findings")
    # HEAD: the rounding fix, plus unexplained drift in a few places.
    _write(root, head)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", f"{COMMIT_SUBJECT}\n\n{COMMIT_BODY}")
    return root


def _write(root, files):
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)


def _small_files():
    """(base, HEAD) file contents of the scale-1 fixture."""
    base = {"billing/__init__.py": "", "billing/rates.py": RATES_SOURCE, "billing/money.py": MONEY_BASE,
            "settings.py": SETTINGS_BASE}
    base.update({f"billing/{name}.py": _module(name, rounded=False, drift=None) for name in MODULES})
    rng = random.Random(7)
    rows = []
    for i in range(400):
        level, template = rng.choice(LOG_KINDS)
        rows.append(f"2026-09-01T12:{i // 60:02d}:{i % 60:02d}Z {level} " + template.format(
            n=rng.randint(1000, 9999), ms=rng.randint(40, 9000), cur=rng.choice(["USD", "EUR", "GBP"]),
            a=rng.randint(1, 3), e=f"{rng.randint(1, 500)}.{rng.randint(0, 99):02d}",
            g=f"{rng.randint(1, 500)}.{rng.randint(0, 99):02d}"))
    base["logs/worker.log"] = "\n".join(rows) + "\n"
    findings = []
    for i, name in enumerate(MODULES * 4):
        findings.append({
            "id": f"F{i + 1:02d}", "file": f"billing/{name}.py", "line": 3 + (i % 4) * 5,
            "claim": rng.choice(FINDING_CLAIMS)})
    base["review/findings.jsonl"] = "\n".join(json.dumps(f) for f in findings) + "\n"
    head = {"billing/money.py": MONEY_HEAD, "settings.py": SETTINGS_HEAD}
    head.update({f"billing/{name}.py": _module(name, rounded=True, drift=DRIFT.get(name)) for name in MODULES})
    return base, head


def plan(scale):
    """Construction of the scale-N fixture (N >= 2), shared by build() and answer_keys().

    Returns 7N modules {"name", "path", "drift", "items"}; "drift" is the DRIFT_KINDS entry
    injected into the module, if any. Each of its 4 items is a line-item function: its "name",
    "index", the features its code has ("legacy": passes legacy=True; "logs": takes a customer
    and logs its id; "guarded": wraps fetch_rate in try/except KeyError; "bulk": discounts the
    rate above BULK_THRESHOLD; "comment": a comment above the money line; "stale_doc": a
    docstring line about truncating; "usd": fetch_rate("USD") whatever the currency; "default":
    currency defaults to "USD"; "unit": truncates the amount before converting it, rather than
    after), "wording": which COMMENT_PAIRS and DOC_PAIRS entry its comment and docstring use,
    and "drift": the kind injected into this function, if any.
    """
    rng = random.Random(scale)
    modules = []
    for doc in MODULES:
        for region in REGIONS[:scale]:
            name = f"{region}_{doc}"
            items = [{
                "name": f"{name}_line_{i}", "index": i, "legacy": (i + len(name)) % 3 == 0,
                "logs": rng.random() < 0.3, "guarded": rng.random() < 0.25, "bulk": rng.random() < 0.2,
                "comment": rng.random() < 0.35, "stale_doc": rng.random() < 0.3, "usd": rng.random() < 0.08,
                "default": rng.random() < 0.15, "unit": rng.random() < 0.12,
                "wording": (rng.randrange(len(COMMENT_PAIRS)), rng.randrange(len(DOC_PAIRS))), "drift": None,
            } for i in range(4)]
            modules.append({"name": name, "path": f"billing/{name}.py", "drift": None, "items": items})
    for kind in (DRIFT_KINDS * 2)[:max(2, round(0.04 * 28 * scale))]:
        module = rng.choice([m for m in modules if m["drift"] is None])
        module["drift"] = kind
        items = module["items"]
        if kind in ("constant", "threshold"):  # changes a module constant that some line item reads
            if not any(item["bulk"] for item in items):
                rng.choice(items)["bulk"] = True
            continue
        if kind == "new_helper":  # appended to the module
            continue
        # A changed def line shares a hunk with the previous function's change unless it is the first.
        item = items[0] if kind in ("swap_args", "default", "feature_flag") else rng.choice(items)
        item["drift"] = kind
        if kind == "swap_args":
            item["default"] = False
        elif kind == "default":
            item.update(default=True, logs=False)
        elif kind == "feature_flag":  # keeps the def line and the new flag check in one hunk
            item.update(logs=False, guarded=False)
        elif kind in ("reorder", "half_up", "abs"):
            item["unit"] = False
        elif kind == "pii_log":
            item["logs"] = True
        elif kind == "widen_except":
            item["guarded"] = True
        elif kind == "boundary":
            item["bulk"] = True
    return modules


def _item_lines(module, item, head):
    """([(text, drift)], {spot: offset}) of one line-item function at base or HEAD. drift marks
    the lines the injected drift changes or adds (HEAD) or changes or removes (base); spots are
    the offsets of its def, fetch_rate call, money (last return), log, except and early lines."""
    name, i, kind = item["name"], item["index"], item["drift"]
    amount = f"amount_{i}"
    lines, spots = [], {}

    def add(text, drift=False, spot=None):
        if spot:
            spots[spot] = len(lines)
        lines.append((text, drift))

    params = [amount, *(["customer"] if item["logs"] else []), 'currency="USD"' if item["default"] else "currency"]
    if head and kind == "swap_args":
        params[0], params[-1] = params[-1], params[0]
    elif head and kind == "default":
        params[-1] = 'currency="EUR"'
    elif head and kind == "feature_flag":
        params.append("skip_fx=False")
    add(f"def {name}({', '.join(params)}):", kind in ("swap_args", "default", "feature_flag"), "def")
    add(f'    """Price line item {i} of a {module["name"]} document.')
    add("")
    add("    The amount is in the document currency; the rate converts it to the")
    add("    settlement currency before rounding to cents.")
    if item["stale_doc"]:
        add("    " + DOC_PAIRS[item["wording"][1]][head])
    add('    """')
    if item["logs"]:
        if head and kind == "pii_log":
            add(f'    log.info("pricing %s for customer %s <%s>", "{name}", customer.id, customer.email)', True, "log")
        else:
            add(f'    log.info("pricing %s for customer %s", "{name}", customer.id)', kind == "pii_log", "log")
    if head and kind == "early_return":
        add('    if currency == "EUR":', True)
        add(f"        return round_cents({amount})", True, "early")
    currency = '"USD"' if item["usd"] else "currency"
    call = f"fetch_rate({currency}{', legacy=True' if item['legacy'] else ''})"
    if item["guarded"]:
        widened = head and kind == "widen_except"
        add("    try:")
        add(f"        rate = {call}", spot="call")
        add("    except Exception:" if widened else "    except KeyError:", kind == "widen_except", "except")
        add("        rate = 1.0" if widened else '        raise ValueError(f"unsupported currency {currency}")',
            kind == "widen_except")
    else:
        add(f"    rate = {call}", spot="call")
    if head and kind == "feature_flag":
        add("    if skip_fx:", True)
        add("        rate = 1.0", True)
    if item["bulk"]:
        add(f"    if {amount} {'>=' if head and kind == 'boundary' else '>'} BULK_THRESHOLD:", kind == "boundary")
        add("        rate *= BULK_RATE_DISCOUNT")
    if item["comment"]:
        add("    # " + COMMENT_PAIRS[item["wording"][0]][head].format(name=name))
    if not head:
        expr = f"int({amount} * 100) / 100 * rate" if item["unit"] else f"int({amount} * rate * 100) / 100"
    elif item["unit"] or kind == "reorder":
        expr = f"round_cents({amount}) * rate"
    elif kind == "half_up":
        expr = f"round_cents({amount} * rate + 0.005)"
    elif kind == "abs":
        expr = f"round_cents(abs({amount}) * rate)"
    else:
        expr = f"round_cents({amount} * rate)"
    add(f"    return {expr}", kind in ("reorder", "half_up", "abs"), "money")
    return lines, spots


def _module_lines(module, head):
    """([(text, drift)], {item name: {spot: line number}}) of one scaled module at base or HEAD."""
    items, kind = module["items"], module["drift"]
    logs, bulk = any(item["logs"] for item in items), any(item["bulk"] for item in items)
    lines = [("import logging", False), ("", False)] if logs else []
    lines += [("from billing.money import round_cents", False), ("from billing.rates import fetch_rate", False)]
    if logs or bulk:
        lines.append(("", False))
    if logs:
        lines.append(("log = logging.getLogger(__name__)", False))
    if bulk:
        lines.append((f"BULK_THRESHOLD = {'5_000' if head and kind == 'threshold' else '10_000'}",
                      kind == "threshold"))
        lines.append((f"BULK_RATE_DISCOUNT = {'0.95' if head and kind == 'constant' else '0.98'}",
                      kind == "constant"))
    marks = {}
    for item in items:
        lines += [("", False), ("", False)]
        body, spots = _item_lines(module, item, head)
        marks[item["name"]] = {spot: len(lines) + 1 + offset for spot, offset in spots.items()}
        lines += body
    if head and kind == "new_helper":
        lines += [("", False), ("", False), (f"def {module['name']}_small_balance_waiver():", True),
                  ("    return 0.5", True)]
    return lines, marks


def _text(lines):
    return "\n".join(text for text, _ in lines) + "\n"


def _claim_holds(kind, item):
    """Whether a code-dependent review claim (CODE_CLAIMS) is true of a line item at HEAD."""
    drift = item["drift"]
    return {"legacy": item["legacy"], "order": item["unit"] or drift == "reorder",
            "product": not (item["unit"] or drift == "reorder"), "usd": item["usd"],
            "pii": drift == "pii_log", "swallow": drift == "widen_except"}[kind]


def _scaled_findings(modules, marks, scale):
    """28N review findings, each citing a line of one line-item function; each code-dependent
    claim kind alternates between functions it is true of and functions it is false of, while
    both exist."""
    rng = random.Random(100 + scale)
    items = [(module, item) for module in modules for item in module["items"]]
    kinds, weights = zip(*CLAIM_WEIGHTS.items())
    used, findings, drawn = set(), [], dict.fromkeys(CODE_CLAIMS, 0)
    while len(findings) < 28 * scale:
        kind = rng.choices(kinds, weights)[0]
        pool = [(module, item) for module, item in items if (kind, item["name"]) not in used
                and (kind != "pii" or item["logs"]) and (kind != "swallow" or item["guarded"])]
        if kind in CODE_CLAIMS:
            want = drawn[kind] % 2 == 0
            pool = [pair for pair in pool if _claim_holds(kind, pair[1]) == want] or pool
        if not pool:
            continue
        module, item = rng.choice(pool)
        used.add((kind, item["name"]))
        if kind in CODE_CLAIMS:
            drawn[kind] += 1
        findings.append({"file": module["path"], "function": item["name"],
                         "line": marks[item["name"]][CLAIM_LINE[kind]], "claim": rng.choice(SCALED_CLAIMS[kind])})
    findings.sort(key=lambda f: (f["file"], f["line"], f["claim"]))
    width = len(str(len(findings)))
    return [{"id": f"F{n:0{width}d}", **finding} for n, finding in enumerate(findings, 1)]


def _log_kinds(scale):
    """(INFO kinds, ERROR/WARN kinds) of the scale-N log as (level, template, verdict): the
    LOG_KINDS ones, then 3N more, about 18% real bugs and 10% debatable, the rest noise."""
    total = 5 + 3 * scale
    bugs = max(1, round(0.18 * total)) - 1
    debatable = max(2, round(0.10 * total)) - 2
    noise = [(level, pattern.replace("{svc}", service), False)
             for service in NOISE_SERVICES for level, pattern in NOISE_PATTERNS]
    random.Random(300).shuffle(noise)
    keyed = [(level, template, LOG_BUG[template]) for level, template in LOG_KINDS if level != "INFO"]
    keyed += LOG_BUGS_EXTRA[:bugs] + LOG_DEBATABLE_EXTRA[:debatable] + noise[:total - 5 - bugs - debatable]
    info = [(level, template, None) for level, template in LOG_KINDS if level == "INFO"] + LOG_INFO_EXTRA
    return info, keyed


def _cents(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def _scaled_log(scale):
    """400N log lines; every kind appears at least twice, and bug kinds are rare."""
    rng = random.Random(200 + scale)
    info, keyed = _log_kinds(scale)
    kinds, weights = info + keyed, []
    bug_weights = iter([1.0, 0.4, 0.15] * 4)
    for kind in kinds:
        weights.append(10 if kind in info else next(bug_weights) if kind[2] is True else 3 if kind[2] is False else 2)
    picks = [kind for kind in kinds for _ in range(2)] + rng.choices(kinds, weights, k=400 * scale - 2 * len(kinds))
    rng.shuffle(picks)
    start, seconds, rows = datetime(2026, 9, 1, tzinfo=timezone.utc), 0, []
    for level, template, _ in picks:
        seconds += rng.randint(1, 15)
        low = rng.randint(100, 50000)
        values = {"n": rng.randint(1000, 99999), "ms": rng.randint(40, 9000), "cur": rng.choice(["USD", "EUR", "GBP"]),
                  "a": rng.randint(1, 3), "big": rng.randint(5, 9), "e": _cents(low),
                  "g": _cents(low + rng.randint(1, 5000)), "neg": "-" + _cents(rng.randint(100, 90000)),
                  "cid": f"ch_{rng.randint(100000, 999999)}", "period": f"2026-{rng.randint(1, 8):02d}",
                  "decline": rng.choice(["insufficient_funds", "expired_card", "do_not_honor"]),
                  "age": rng.randint(900, 7200)}
        stamp = (start + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append(f"{stamp} {level} " + template.format(**values))
    return "\n".join(rows) + "\n"


def _scaled_files(scale):
    """(base, HEAD) file contents of the scale-N fixture."""
    modules = plan(scale)
    base = {"billing/__init__.py": "", "billing/rates.py": RATES_SOURCE, "billing/money.py": MONEY_BASE,
            "settings.py": SETTINGS_BASE}
    head = {"billing/money.py": MONEY_HEAD, "settings.py": SETTINGS_HEAD}
    marks = {}
    for module in modules:
        base[module["path"]] = _text(_module_lines(module, head=False)[0])
        lines, spots = _module_lines(module, head=True)
        head[module["path"]] = _text(lines)
        marks.update(spots)
    base["logs/worker.log"] = _scaled_log(scale)
    base["review/findings.jsonl"] = "\n".join(map(json.dumps, _scaled_findings(modules, marks, scale))) + "\n"
    return base, head


def _functions(source):
    """(name, first line, last line) of every function defined in Python source."""
    return [(node.name, node.lineno, node.end_lineno) for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef)]


def _enclosing(functions, line):
    return next((f for f in functions if f[1] <= line <= f[2]), None)


def _hunks(root):
    """Hunks of `git show HEAD` with git's default splitting, whatever the user's diff config."""
    diff = _git(root, "show", "--format=", "--no-color", "--no-ext-diff", "--no-textconv",
                "--no-renames", "--diff-algorithm=myers", "-U3", "--inter-hunk-context=0",
                "--src-prefix=a/", "--dst-prefix=b/", "HEAD")
    hunks, path, hunk = [], None, None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            path, hunk = line.split(" b/", 1)[1], None
        elif match := HUNK_HEADER.match(line):
            old, new = int(match[1]), int(match[2])
            anchor = f"+{new}" if match[3] != "0" else f"-{old}"
            hunk = {"path": path, "id": f"{path}:{anchor}", "start": new, "added": [], "removed": []}
            hunks.append(hunk)
        elif hunk is not None:
            if line.startswith("+"):
                hunk["added"].append((new, line[1:]))
                new += 1
            elif line.startswith("-"):
                hunk["removed"].append((old, line[1:]))
                old += 1
            elif not line.startswith("\\"):
                old, new = old + 1, new + 1
    return sorted(hunks, key=lambda h: (h["path"], h["start"]))


def _scale_of(billing):
    """A fixture's scale, from its number of line-item modules (7 per unit of scale)."""
    count = sum(1 for path in billing if Path(path).stem not in ("__init__", "money", "rates"))
    scale, rest = divmod(count, len(MODULES))
    if rest or not 1 <= scale <= MAX_SCALE:
        raise ValueError(f"{count} billing modules: not a fixture built by build()")
    return scale


def _small_truth(source, functions):
    """Truth of the scale-1 fixture, from the constants build() writes it with."""
    drift, drifted = {}, {}
    for name, extra in DRIFT.items():
        path, texts = f"billing/{name}.py", set(extra) - {""}
        drift[path] = ({n for n, line in enumerate(source[path].splitlines(), 1) if line in texts}, set())
        drifted[f"{path}:{extra[0].removeprefix('def ').split('(')[0]}"] = True
    float_claim, legacy_claim, test_claim, name_claim = FINDING_CLAIMS

    def finding(record):
        path, line, claim = record["file"], record["line"], record["claim"]
        if claim != legacy_claim:
            return {float_claim: True, test_claim: None, name_claim: False}[claim]
        function = _enclosing(functions[path], line)
        scope = ("\n".join(source[path].splitlines()[function[1] - 1:function[2]])
                 if function else source[path])
        return True if "legacy=True" in scope else (None if "legacy=True" in source[path] else False)

    return {
        "drift": drift, "functions": drifted, "finding": finding,
        "money": lambda path, function, n, text: None if function == "round_cents" else "round_cents(" not in text,
        "docstring": lambda path, function, texts: not all(re.search(r"round_cents\(\w+ \* rate\)", text)
                                                          for text in texts),
        "log": [(level, template, LOG_BUG[template]) for level, template in LOG_KINDS if level != "INFO"],
    }


def _scaled_truth(scale, source):
    """Truth of the scale-N fixture, from plan(N), after checking every module against it."""
    modules = plan(scale)
    expected = {module["path"] for module in modules}
    found = {path for path in source if Path(path).stem not in ("__init__", "money", "rates")}
    if found != expected:
        raise ValueError(f"billing modules differ from build(root, scale={scale}): {sorted(found ^ expected)[:5]}")
    drift, drifted, money, docs, items = {}, {}, {}, {}, {}
    for module in modules:
        path, kind = module["path"], module["drift"]
        base = _module_lines(module, head=False)[0]
        head, marks = _module_lines(module, head=True)
        if _text(head) != source[path]:
            raise ValueError(f"{path} differs from what build(root, scale={scale}) writes")
        drift[path] = ({n for n, (_, tagged) in enumerate(head, 1) if tagged},
                       {n for n, (_, tagged) in enumerate(base, 1) if tagged})
        if kind == "new_helper":
            drifted[f"{path}:{module['name']}_small_balance_waiver"] = True
        for item in module["items"]:
            key, injected, spots = f"{path}:{item['name']}", item["drift"], marks[item["name"]]
            items[key] = item
            if injected:
                drifted[key] = True
            elif kind in ("constant", "threshold") and item["bulk"]:
                drifted[key] = None
            money[(path, spots["money"])] = item["unit"] or injected in ("reorder", "half_up", "abs")
            if "early" in spots:
                money[(path, spots["early"])] = True
            docs[key] = (True if item["unit"] or injected in ("reorder", "early_return")
                         else None if item["usd"] or injected in ("half_up", "abs", "feature_flag", "widen_except")
                         else False)
    kind_of = {text: kind for kind, texts in SCALED_CLAIMS.items() for text in texts}

    def finding(record):
        kind = kind_of[record["claim"]]
        if kind in CODE_CLAIMS:
            return _claim_holds(kind, items[f"{record['file']}:{record['function']}"])
        return None if kind == "test" else False

    return {
        "drift": drift, "functions": drifted, "finding": finding,
        "money": lambda path, function, n, text: None if function == "round_cents" else money[(path, n)],
        "docstring": lambda path, function, texts: docs[f"{path}:{function}"],
        "log": _log_kinds(scale)[1],
    }


def answer_keys(root):
    """Ground truth for grading final answers, computed from the fixture repository at root.

    Returns {key: {"question", "flag_label", "items": [{"id", "desc", "flag"}]}}. flag is True
    or False, or None where the truth is genuinely debatable; evals/grade.py judges and scores
    only the True/False items. Ids are unique within a key and the same on every build. Each
    desc says what the item is before quoting its content ('review finding F07 (cites ...), which
    claims: "..."'): with bare "path:line: text" descs Jev carried a verdict on one item to
    look-alikes (same claim, same file) several times as often. Truth follows the construction
    in build():

    - unexplained_hunks: every hunk of `git show HEAD`, id "path:+newstart". Flagged (3 of 30):
      the settings.py hunk (LOG_LEVEL, HTTP_TIMEOUT_SECONDS) and the last hunk of each DRIFT
      module, which also appends the module's extra function. The other 27 replace truncation
      with round_cents or rewrite round_cents itself, which is what the message describes.
    - unexplained_functions: the same drift one level up, every function in billing/ at HEAD,
      id "path:name". Flagged: the two DRIFT functions HEAD adds. None: functions HEAD leaves
      untouched (fetch_rate), neither part of the fix nor added by the commit.
    - legacy_callsites: every fetch_rate call in billing/; flagged when it still passes legacy=
      (7 of 28: one per module, by build()'s (i + len(name)) % 3 rule).
    - log_real_bugs: ERROR/WARN lines grouped by template, flags from LOG_BUG. The template is
      the unit a triage buckets by: all its lines share one cause and verdict (only ids and
      numbers vary; no mismatch line has expected == got). A total mismatch is a computation
      bug; cache misses and webhook retries heal themselves. None: insufficient balance (a
      business outcome, neither a bug nor transient) and upstream timeouts (transient, but
      HEAD also cut HTTP_TIMEOUT_SECONDS from 30 to 5).
    - valid_findings: the 28 findings; weak_findings is the same list with the flag inverted
      (None stays None). Cited lines are synthetic (3, 8, 13, 18), so a claim is checked
      against the function holding the cited line, or the whole file when the line is outside
      every function. Float product: valid everywhere; each line item rounds a float product,
      and round_cents(5.5 * 0.79) is 4.35 where half-even of the exact 4.345 is 4.34.
      legacy=True: valid when the cited function (or the file, for lines outside functions)
      passes it, None when the cited function does not but the file does (right issue, wrong
      place). Missing test: None (the fixture has no tests at all; nit or worth fixing is a
      judgment call). Naming: invalid, every cited function follows <module>_line_<i>.
    - odd_money_lines: the return line of every <module>_line_<i> function plus round_cents'
      own body. At HEAD all 28 line items return round_cents(amount * rate), so none is
      flagged: the correct answer is that none are odd. The float product they all round (a
      valid finding above) is shared by all 28, so it makes none an odd one out. The
      round_cents body is None (whether the rounding primitive itself is a line to classify is
      debatable).
    - docstring_mismatch: the docstring of every function that has one (the 28 line items).
      Each says the rate converts the amount before it is rounded to cents, and at HEAD every
      body returns round_cents(amount * rate), so none is flagged. RATES does not say which
      way it converts, so the code does not contradict "the rate converts it".

    A scaled fixture (build(root, scale=N), N >= 2) is recognised by its 7N line-item modules
    and checked line for line against plan(N), whose construction gives the truth. The keys,
    ids and descs are built the same way, with these flags:

    - unexplained_hunks: the settings.py hunk, and each hunk holding a line that a drift
      injection changes, adds or removes (one per module, each in exactly one hunk; DRIFT_KINDS
      in order). They share no token a grep could find: a changed module constant (constant,
      threshold), an early return that skips conversion for EUR, swapped parameters, a changed
      default currency, a new skip_fx flag, the customer's email added to a log call, a KeyError
      handler widened to swallow every exception with a 1.0 rate, a > turned into >=, an
      appended helper, and three that go through round_cents like the fix but change the
      computation, unexplained only against the message: rounding before converting where the
      code converted first (reorder), adding 0.005 first (half_up), abs() of the amount (abs).
      Some share a hunk with their line item's rounding change, some stand alone. Every other
      hunk replaces truncation with round_cents, keeping the order of operations (so functions
      that truncated before converting now round before converting), or rewords a comment or
      docstring line from truncating to rounding half-even: what the message describes.
    - unexplained_functions: flagged when a drift line is in the function, or it is the
      appended helper. None: fetch_rate, and functions whose only unexplained change is a
      module constant they read (constant, threshold: their code did not change, their
      behavior did).
    - legacy_callsites: as at scale 1, by the same rule (about a third of 28N).
    - log_real_bugs: 5 + 3N kinds: the five above, then LOG_BUGS_EXTRA (a negative payout, a
      charge captured again, an unbalanced ledger entry, a retry past its own limit of 3, ...),
      LOG_DEBATABLE_EXTRA (None: a declined card, a stale fx rate used anyway, a 422 from the
      tax service, a dispute: business outcomes or upstream data, not clearly bugs) and
      transient noise (False: a service timing out, returning 503, rate limiting, missing its
      cache, resetting a connection or failing one health check, then retrying). About 18% of
      kinds are bugs, and bug lines are rare: some kinds have a handful of lines among 400N.
    - valid_findings / weak_findings: 28N findings, each citing a line of the line-item
      function in its "function" field. A code-dependent claim (CODE_CLAIMS) is valid exactly
      when the cited function's code at HEAD makes it true: it passes legacy=True; it rounds
      the amount before multiplying by the rate; round_cents gets a float product (valid as at
      scale 1); fetch_rate gets a hard-coded "USD"; it logs customer.email; its except clause
      catches everything and falls back to 1.0. Claims false of every line item (it truncates
      with int(); round_cents is not imported; the name breaks the convention) and nits (type
      hints, renaming rate) are invalid; the missing negative-amount test stays None.
    - odd_money_lines: flagged returns do not round the converted amount: they round the
      unconverted amount (functions that truncated before converting, and reorder), add 0.005
      (half_up), take abs() (abs), or return early for EUR unconverted. None: round_cents' body.
    - docstring_mismatch: flagged where the code contradicts "converts ... before rounding to
      cents" (rounding before converting; the EUR early return). None where the code only adds
      something the docstring does not mention (half_up, abs, skip_fx, the 1.0 fallback, a
      hard-coded USD rate).
    """
    root = Path(root)

    def blob(path):
        return _git(root, "cat-file", "blob", f"HEAD:{path}")

    billing = [p for p in _git(root, "ls-tree", "-r", "--name-only", "HEAD", "billing/").split()
               if p.endswith(".py")]
    source = {path: blob(path) for path in billing}
    functions = {path: _functions(text) for path, text in source.items()}
    scale = _scale_of(billing)
    truth = _small_truth(source, functions) if scale == 1 else _scaled_truth(scale, source)

    def where(path, line):
        function = _enclosing(functions.get(path, []), line)
        return function[0] if function else "module level"

    diff = _hunks(root)
    hunks = []
    for hunk in diff:
        path = hunk["path"]
        drift_added, drift_removed = truth["drift"].get(path, (set(), set()))
        numbers = [n for n, _ in hunk["added"]] or [n for n, _ in hunk["removed"]]
        span = f"lines {numbers[0]}-{numbers[-1]}" if len(numbers) > 1 else f"line {numbers[0]}"
        places = ", ".join(dict.fromkeys(where(path, n) for n, text in hunk["added"] if text.strip()))
        changes = ([f"- {text.strip()}" for _, text in hunk["removed"] if text.strip()]
                   + [f"+ {text.strip()}" for _, text in hunk["added"] if text.strip()])
        hunks.append({"id": hunk["id"], "desc": f"changed hunk of {path}, {span} ({places or 'module level'}), "
                      "which makes these changes: " + " | ".join(changes),
                      "flag": path == "settings.py" or any(n in drift_added for n, _ in hunk["added"])
                      or any(n in drift_removed for n, _ in hunk["removed"])})

    added = {}
    for hunk in diff:
        added.setdefault(hunk["path"], set()).update(n for n, _ in hunk["added"])
    callsites, money, defined, docstrings = [], [], [], []
    for path in billing:
        lines = source[path].splitlines()
        for n, line in enumerate(lines, 1):
            if "fetch_rate(" in line and not line.lstrip().startswith("def "):
                callsites.append({"id": f"{path}:{n}", "flag": "legacy=" in line, "desc": (
                    f"fetch_rate call site at {path}:{n}, in {where(path, n)}, whose code is: {line.strip()}")})
        for node in ast.walk(ast.parse(source[path])):
            if not isinstance(node, ast.FunctionDef):
                continue
            returns = sorted(r.lineno for r in ast.walk(node) if isinstance(r, ast.Return))
            if re.fullmatch(rf"{Path(path).stem}_line_\d+", node.name) or node.name == "round_cents":
                for n in returns:
                    line = lines[n - 1]
                    money.append({"id": f"{path}:{n}", "desc": f"money line at {path}:{n}, in {node.name}, whose code is: "
                                  f"{line.strip()}", "flag": truth["money"](path, node.name, n, line)})
            body = range(node.lineno, node.end_lineno + 1)
            doc = ast.get_docstring(node)
            doc_lines = range(node.body[0].lineno, node.body[0].end_lineno + 1) if doc is not None else ()
            code = [lines[n - 1].strip() for n in body if n not in doc_lines and lines[n - 1].strip()]
            span = f"{node.name} in {path}, lines {node.lineno}-{node.end_lineno}"
            touched = not added.get(path, set()).isdisjoint(body)
            defined.append({"id": f"{path}:{node.name}", "desc": f"function {span}, whose code is: " + " | ".join(code),
                            "flag": truth["functions"].get(f"{path}:{node.name}", False if touched else None)})
            if doc is not None:
                docstrings.append({"id": f"{path}:{doc_lines[0]}",
                                   "flag": truth["docstring"](path, node.name, [lines[n - 1] for n in returns]),
                                   "desc": f"docstring of function {span}, which says: \"{' '.join(doc.split())}\""})

    log = blob("logs/worker.log").splitlines()
    log_items = []
    for level, template, verdict in truth["log"]:
        pattern = re.compile(rf"\S+ {level} " + re.sub(r"\\\{\w+\\\}", ".+?", re.escape(template)))
        numbers = [n for n, line in enumerate(log, 1) if pattern.fullmatch(line)]
        if numbers:
            log_items.append({"id": f"{level} {template}", "flag": verdict, "desc": (
                f"kind of {level} line in logs/worker.log with the message '{template}' ({len(numbers)} lines: "
                f"{', '.join(map(str, numbers))}), for example line {numbers[0]}: {log[numbers[0] - 1]}")})

    findings = []
    for finding in map(json.loads, blob("review/findings.jsonl").splitlines()):
        path, line, claim = finding["file"], finding["line"], finding["claim"]
        findings.append({"id": finding["id"], "flag": truth["finding"](finding), "desc": (
            f"review finding {finding['id']} (cites {path}:{line}, {where(path, line)}), which claims: \"{claim}\"")})
    weak = [{**item, "flag": None if item["flag"] is None else not item["flag"]} for item in findings]

    return {
        "unexplained_hunks": {
            "question": "Which changed hunks of the HEAD commit are not explained by its commit message?",
            "flag_label": "not explained by the commit message", "items": hunks},
        "unexplained_functions": {
            "question": "Which functions in billing/ does HEAD add or change for something its commit message does not explain?",
            "flag_label": "added or changed by HEAD for something other than the half-even rounding fix its "
                          "commit message describes", "items": defined},
        "legacy_callsites": {
            "question": "Which fetch_rate call sites in billing/ still pass legacy= and need changing?",
            "flag_label": "still passing legacy= (needs changing)", "items": callsites},
        "log_real_bugs": {
            "question": "Which kinds of ERROR/WARN line in logs/worker.log are real bugs rather than transient noise?",
            "flag_label": "a real bug rather than transient noise", "items": log_items},
        "valid_findings": {
            "question": "Which review findings in review/findings.jsonl are valid and worth fixing?",
            "flag_label": "valid and worth fixing", "items": findings},
        "weak_findings": {
            "question": "Which review findings in review/findings.jsonl are invalid, misleading or nits?",
            "flag_label": "invalid, misleading or a nit (not worth fixing)", "items": weak},
        "odd_money_lines": {
            "question": "Which money-computing lines in billing/ at HEAD are odd ones out: they truncate, or do "
                        "something other than round the converted amount through round_cents like the rest?",
            "flag_label": "an odd one out: it truncates, or does something other than round the converted amount "
                          "through round_cents like the rest", "items": money},
        "docstring_mismatch": {
            "question": "Which line-item docstrings in billing/ do not match what their function does at HEAD?",
            "flag_label": "a docstring that does not match what its function does at HEAD", "items": docstrings},
    }
