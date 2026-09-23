"""Deterministic fixture repository for jevify trigger evals.

build(root) creates a small git repository with:
- HEAD: one commit whose message explains most, but not all, of its changed hunks
- 28 call sites of billing.rates.fetch_rate, 7 still passing the ignored legacy= flag
- logs/worker.log: 400 mixed log lines
- review/findings.jsonl: 28 review findings about the HEAD commit
"""

import json
import random
import subprocess
from pathlib import Path

MODULES = ["invoices", "refunds", "payouts", "credits", "subscriptions", "taxes", "fees"]
COMMIT_SUBJECT = "fix(billing): round currency amounts half-even instead of truncating"
COMMIT_BODY = (
    "Amounts were truncated to cents with int(x * 100) / 100, which under-charged\n"
    "customers by up to a cent per line item. Every money computation now goes\n"
    "through billing.money.round_cents, which uses Decimal ROUND_HALF_EVEN.\n"
)


def _git(root, *args):
    subprocess.run(
        ["git", "-c", "user.name=jevify-eval", "-c", "user.email=eval@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
        cwd=root, check=True, capture_output=True, text=True)


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


def build(root):
    root = Path(root)
    pkg = root / "billing"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "rates.py").write_text(
        "RATES = {'USD': 1.0, 'EUR': 0.92, 'GBP': 0.79}\n\n\n"
        "def fetch_rate(currency, legacy=False):\n"
        "    return RATES[currency]\n")
    (pkg / "money.py").write_text(
        "def round_cents(value):\n    return int(value * 100) / 100\n")
    (root / "settings.py").write_text(
        "LOG_LEVEL = 'INFO'\nHTTP_TIMEOUT_SECONDS = 30\nRETRY_LIMIT = 3\n")
    for name in MODULES:
        (pkg / f"{name}.py").write_text(_module(name, rounded=False, drift=None))

    rng = random.Random(7)
    logs = root / "logs"
    logs.mkdir()
    kinds = [
        ("INFO", "invoice {n} rendered in {ms}ms"),
        ("INFO", "payout batch {n} queued"),
        ("WARN", "rate cache miss for {cur}, refetching"),
        ("WARN", "retrying webhook delivery {n} (attempt {a})"),
        ("ERROR", "payout {n} failed: insufficient balance"),
        ("ERROR", "refund {n} failed: upstream timeout after {ms}ms"),
        ("ERROR", "invoice {n} total mismatch: expected {e} got {g}"),
    ]
    rows = []
    for i in range(400):
        level, template = rng.choice(kinds)
        rows.append(f"2026-09-01T12:{i // 60:02d}:{i % 60:02d}Z {level} " + template.format(
            n=rng.randint(1000, 9999), ms=rng.randint(40, 9000), cur=rng.choice(["USD", "EUR", "GBP"]),
            a=rng.randint(1, 3), e=f"{rng.randint(1, 500)}.{rng.randint(0, 99):02d}",
            g=f"{rng.randint(1, 500)}.{rng.randint(0, 99):02d}"))
    (logs / "worker.log").write_text("\n".join(rows) + "\n")

    review = root / "review"
    review.mkdir()
    findings = []
    for i, name in enumerate(MODULES * 4):
        findings.append({
            "id": f"F{i + 1:02d}", "file": f"billing/{name}.py", "line": 3 + (i % 4) * 5,
            "claim": rng.choice([
                "round_cents is called on a float product; precision may still be lost before rounding.",
                "fetch_rate is called with legacy=True, which is ignored by the current implementation.",
                "Missing test for negative amounts in this line-item function.",
                "Function name does not follow the module naming convention.",
            ])})
    (review / "findings.jsonl").write_text("\n".join(json.dumps(f) for f in findings) + "\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "feat(billing): initial billing modules, sample logs and review findings")

    # HEAD: the rounding fix, plus unexplained drift in a few places.
    (pkg / "money.py").write_text(
        "from decimal import ROUND_HALF_EVEN, Decimal\n\n\n"
        "def round_cents(value):\n"
        "    return float(Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN))\n")
    drift = {
        "refunds": ["def refunds_window_days():", "    return 45", ""],
        "payouts": ["def payouts_batch_size():", "    return 500", ""],
    }
    for name in MODULES:
        (pkg / f"{name}.py").write_text(_module(name, rounded=True, drift=drift.get(name)))
    (root / "settings.py").write_text(
        "LOG_LEVEL = 'DEBUG'\nHTTP_TIMEOUT_SECONDS = 5\nRETRY_LIMIT = 3\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", f"{COMMIT_SUBJECT}\n\n{COMMIT_BODY}")
    return root
