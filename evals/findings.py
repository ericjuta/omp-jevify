"""Structured review findings (jevify-findings/1) for schema2 grading.

An answer is one JSON object, optionally inside one full JSON fence. It names claims by exact id and cites
bounded, exact quotations from the reviewed source revision. validate_findings checks the answer against a
source snapshot that grade() rebuilds from git trees; it never trusts hashes or quotes the answer supplies.

    Passing validation proves the document is well formed, its ids are exact, not duplicated, and not conflicting,
    and its quotations are the complete cited lines (LF-joined), so no fragment can stand in for a span.
its revisions are the reviewed ones. It does not prove
that a correctly named, correctly quoted passage logically supports the labels. Labels are scored only against
the frozen key. Rejected or ambiguous findings leave their claims unresolved and are never credited.
"""

import hashlib
import json
import re
import subprocess
from pathlib import Path

SCHEMA = "jevify-findings/1"
SOURCE_SCHEMA = "jevify-source/1"
VALIDATION_SCHEMA = "jevify-findings-validation/1"
CLAIMS_PATH = "review/claims.json"
RUBRIC_PATH = "review/rubric.json"
LABELS = {
    "factual": ("supported", "refuted", "underdetermined"),
    "action": ("fix_now", "defer", "no_action", "investigate"),
    "introduced": ("yes", "no", "unknown"),
}
DOCUMENT_KEYS = frozenset({"schema", "source_revision", "findings"})
FINDING_KEYS = frozenset({"claim_ids", *LABELS, "evidence"})
EVIDENCE_KEYS = frozenset({"revision", "file", "start", "end", "quote"})
_FENCE = re.compile(r"```(?:json)?[ \t]*\n(.*?)\n[ \t]*```", re.DOTALL)


class _Reject(Exception):
    """A deterministic check failed; the message is the public reason recorded for the finding or document."""


class _Frozen(dict):
    def _immutable(self, *args, **kwargs):
        raise TypeError("source snapshot is immutable")

    __setitem__ = __delitem__ = update = pop = popitem = clear = setdefault = _immutable


def _freeze(value):
    if isinstance(value, dict):
        return _Frozen({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True).stdout


def _tree_oid(repo, revision):
    done = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{revision}^{{tree}}"],
                          cwd=repo, capture_output=True, text=True, check=False)
    return done.stdout.strip() if done.returncode == 0 else None


def _tree_files(repo, tree):
    files = {}
    for record in _git(repo, "ls-tree", "-r", "-z", tree).split(b"\0"):
        if not record:
            continue
        meta, _, path = record.partition(b"\t")
        _mode, kind, oid = meta.decode("ascii").split()
        if kind != "blob":
            continue
        data = _git(repo, "cat-file", "blob", oid)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        files[path.decode("utf-8")] = {"sha256": hashlib.sha256(data).hexdigest(), "text": text}
    return files


def source_snapshot(repo):
    """Immutable, JSON-serializable content identity of HEAD and its parent (BASE = HEAD~1) git trees.

    Trees are keyed by tree object id, so commit ids and timestamps never enter the identity. Each file is
    recorded as its sha256 and, when it is UTF-8, its text. The snapshot contains only repository content.
    """
    repo = Path(repo)
    head = _tree_oid(repo, "HEAD")
    if head is None:
        raise ValueError(f"{repo} has no HEAD tree")
    base = _tree_oid(repo, "HEAD~1")
    trees = {head: _tree_files(repo, head)}
    if base is not None and base not in trees:
        trees[base] = _tree_files(repo, base)
    return _freeze({"schema": SOURCE_SCHEMA, "head": head, "base": base, "trees": trees})


def _public_claim_ids(text):
    document = json.loads(text) if text is not None else None
    claims = document.get("claims") if isinstance(document, dict) else None
    ids = [claim.get("id") if isinstance(claim, dict) else None for claim in claims] if isinstance(claims, list) else []
    if not ids or any(not isinstance(cid, str) or not cid for cid in ids) or len(set(ids)) != len(ids):
        raise ValueError(f"{CLAIMS_PATH} needs claims with unique non-empty string ids")
    return ids


def contract_from_snapshot(snapshot):
    """The output obligation for schema2 claims at snapshot HEAD; '' when no public claims or rubric apply."""
    head, base = snapshot["head"], snapshot["base"]
    files = snapshot["trees"][head]
    if CLAIMS_PATH not in files or RUBRIC_PATH not in files:
        return ""
    ids = _public_claim_ids(files[CLAIMS_PATH]["text"])
    rubric = json.loads(files[RUBRIC_PATH]["text"] or "null")
    if not isinstance(rubric, dict) or not rubric:
        raise ValueError(f"{RUBRIC_PATH} needs a non-empty JSON object")
    revisions = f'"{head}" (HEAD tree)' + (f' or "{base}" (BASE tree, HEAD~1)' if base is not None else "")
    lines = [
        f"Output contract for structured findings ({SCHEMA}). Use it for every strategy; only this format is credited.",
        "Return exactly one JSON object, or that same object inside one fenced ```json block, and no other content.",
        "The object has exactly these keys:",
        f'- "schema": "{SCHEMA}"',
        f'- "source_revision": "{head}" (the HEAD tree of the change under review)',
        '- "findings": a list of finding objects.',
        "Each finding has exactly these keys:",
        f'- "claim_ids": a non-empty list of distinct exact claim ids from: {", ".join(ids)}.',
        '- "factual": one of "supported", "refuted", "underdetermined".',
        '- "action": one of "fix_now", "defer", "no_action", "investigate".',
        '- "introduced": one of "yes", "no", "unknown".',
        '- "evidence": a non-empty list of objects with exactly "revision", "file", "start", "end", "quote".'
        f' "revision" is one of {revisions}. "file" is a repository path at that revision. "start" and "end" are'
        ' 1-based inclusive line numbers. "quote" must be the complete text of lines start through end, joined'
        ' with newline characters, with no omitted or partial lines.',
        "A finding gives its three labels to every id it lists, so list only ids it addresses.",
        "Omit claims you do not address. Never invent ids or revisions.",
    ]
    return "\n".join(lines)


def output_contract(repo):
    """The common prompt suffix for schema2 claims in repo (see contract_from_snapshot)."""
    return contract_from_snapshot(source_snapshot(repo))


def contract_digest(text):
    return None if not text else hashlib.sha256(text.encode("utf-8")).hexdigest()


def _no_duplicate_keys(pairs):
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key in a JSON object")
    return dict(pairs)


def _reject_constant(name):
    raise ValueError(f"non-finite number {name} is not allowed")


def _parse_document(answer):
    body = answer.strip()
    fenced = _FENCE.fullmatch(body)
    if fenced is not None:
        body = fenced.group(1)
    try:
        document = json.loads(body, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as error:
        raise _Reject(f"answer is not one JSON object ({type(error).__name__})") from None
    if not isinstance(document, dict):
        raise _Reject("answer is not one JSON object")
    return document


def _check_citation(citation, snapshot, where):
    if not isinstance(citation, dict) or set(citation) != EVIDENCE_KEYS:
        raise _Reject(f"{where}: keys must be exactly {sorted(EVIDENCE_KEYS)}")
    revision = citation["revision"]
    if revision == snapshot["head"]:
        tree = snapshot["trees"][snapshot["head"]]
    elif snapshot["base"] is not None and revision == snapshot["base"]:
        tree = snapshot["trees"][snapshot["base"]]
    else:
        raise _Reject(f"{where}: revision is neither the HEAD nor the BASE tree (wrong revision)")
    entry = tree.get(citation["file"]) if isinstance(citation["file"], str) else None
    if entry is None:
        raise _Reject(f"{where}: file is not in that revision")
    if entry["text"] is None:
        raise _Reject(f"{where}: file is not UTF-8 text")
    start, end, quote = citation["start"], citation["end"], citation["quote"]
    lines = entry["text"].splitlines()
    if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines):
        raise _Reject(f"{where}: start and end must be 1-based line numbers in the file with start <= end")
    if not isinstance(quote, str) or not quote.strip():
        raise _Reject(f"{where}: quote must be non-empty text")
    # Canonical form: the full cited lines joined by LF. No substring, no stripping, no partial lines.
    if quote != "\n".join(lines[start - 1:end]):
        raise _Reject(f"{where}: quote must equal the full text of lines {start}-{end} joined by LF")


def _check_finding(index, finding, snapshot, inventory):
    where = f"finding {index}"
    if not isinstance(finding, dict) or set(finding) != FINDING_KEYS:
        raise _Reject(f"{where}: keys must be exactly {sorted(FINDING_KEYS)}")
    ids = finding["claim_ids"]
    if not isinstance(ids, list) or not ids or not all(isinstance(cid, str) for cid in ids):
        raise _Reject(f"{where}: claim_ids must be a non-empty list of id strings")
    if len(set(ids)) != len(ids):
        raise _Reject(f"{where}: claim_ids repeats an id")
    unknown = sorted(cid for cid in ids if cid not in inventory)
    if unknown:
        raise _Reject(f"{where}: unknown claim id(s) {unknown}")
    labels = {}
    for facet, choices in LABELS.items():
        if finding[facet] not in choices:
            raise _Reject(f"{where}: {facet} must be one of {list(choices)}")
        labels[facet] = finding[facet]
    evidence = finding["evidence"]
    if not isinstance(evidence, list) or not evidence:
        raise _Reject(f"{where}: evidence must be a non-empty list")
    for position, citation in enumerate(evidence):
        _check_citation(citation, snapshot, f"{where} evidence {position}")
    return labels


def _named_ids(finding):
    ids = finding.get("claim_ids") if isinstance(finding, dict) else None
    return [cid for cid in ids if isinstance(cid, str)] if isinstance(ids, list) else []


def validate_findings(answer, snapshot, inventory):
    """Validate one answer against the frozen source snapshot and the key's claim inventory. No model call.

    Returns a record. ``status`` is accepted or rejected for the whole document; a rejected document leaves
    every inventory claim unresolved. Each finding is accepted or rejected with a reason. Every inventory
    claim is asserted (exactly one accepted finding names it, with labels), unresolved (a rejected finding
    names it, or several findings do), or omitted (no finding names it). Unknown ids are listed and never
    credited. Human-readable text comes only from this record (render_findings).
    """
    inventory = frozenset(inventory)
    record = {"schema": VALIDATION_SCHEMA, "expected_revision": snapshot["head"], "status": "accepted",
              "reason": None, "findings": [], "unknown_ids": [], "claims": {}}
    try:
        document = _parse_document(answer)
        if set(document) != DOCUMENT_KEYS:
            raise _Reject(f"document keys must be exactly {sorted(DOCUMENT_KEYS)}")
        if document["schema"] != SCHEMA:
            raise _Reject(f"schema must be {SCHEMA!r}")
        if document["source_revision"] != snapshot["head"]:
            raise _Reject("source_revision is not the reviewed HEAD tree (stale or wrong revision)")
        if not isinstance(document["findings"], list):
            raise _Reject("findings must be a list")
    except _Reject as reject:
        record.update(status="rejected", reason=str(reject))
        record["claims"] = {cid: {"status": "unresolved", "reason": f"document rejected: {reject}"}
                            for cid in sorted(inventory)}
        return record
    named = {cid: [] for cid in inventory}
    unknown = set()
    for index, finding in enumerate(document["findings"]):
        try:
            labels, status, reason = _check_finding(index, finding, snapshot, inventory), "accepted", None
        except _Reject as reject:
            labels, status, reason = None, "rejected", str(reject)
        ids = _named_ids(finding)
        record["findings"].append({"index": index, "claim_ids": ids, "status": status, "reason": reason})
        for cid in ids:
            if cid in inventory:
                named[cid].append((index, status, labels, reason))
            else:
                unknown.add(cid)
    record["unknown_ids"] = sorted(unknown)
    for cid in sorted(inventory):
        entries = named[cid]
        if not entries:
            record["claims"][cid] = {"status": "omitted", "reason": None}
        elif len(entries) > 1:
            record["claims"][cid] = {"status": "unresolved",
                                     "reason": f"named by {len(entries)} findings (duplicate or conflicting records)"}
        elif entries[0][1] == "rejected":
            record["claims"][cid] = {"status": "unresolved", "reason": f"finding {entries[0][0]} rejected: {entries[0][3]}"}
        else:
            index, _, labels, _ = entries[0]
            record["claims"][cid] = {"status": "asserted", "finding": index, **labels, "reason": None}
    return record


def render_findings(record):
    """Human-readable lines built only from a validation record, never from the raw answer."""
    lines = []
    for cid, claim in sorted(record["claims"].items()):
        if claim["status"] == "asserted":
            lines.append(f"{cid}: factual={claim['factual']} action={claim['action']} "
                         f"introduced={claim['introduced']} (finding {claim['finding']})")
        elif claim["status"] == "unresolved":
            lines.append(f"{cid}: unresolved ({claim['reason']})")
    return "\n".join(lines)
