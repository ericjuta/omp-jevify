"""Bounded, provider-free evidence capture and validation.

Occurrences prove source text, never a semantic verdict or permission to act. Public
artifacts are immutable and canonical; origin refresh never rewrites old decisions.
"""

import bisect
import contextlib
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import stat


PLAN_SCHEMA = "jevify-evidence/1"
RESULT_SCHEMA = "jevify-evidence-result/1"
ADOPTION_SCHEMA = "jevify-evidence-adoption/1"
ARTIFACT_DIRECTORY = ".jevify-evidence"
ROLES = ("original_comment", "current_code", "base_code", "governing_contract",
         "helper", "consumer", "external_history")
DELETABLE_ROLES = ("current_code", "helper", "consumer")
STATUSES = ("candidate", "missing", "ambiguous", "rejected", "error")
DEFAULT_LIMITS = {
    "max_sources": 256, "max_records": 10000, "max_fields": 64,
    "max_source_bytes": 4 * 1024 * 1024, "max_total_source_bytes": 32 * 1024 * 1024,
    "max_result_bytes": 16 * 1024 * 1024, "max_value_bytes": 64 * 1024,
    "max_inventory_files": 10000, "max_metadata_bytes": 64 * 1024,
    "max_requests": 32, "max_input_bytes": 1024 * 1024,
    "max_request_bytes": 128 * 1024, "max_response_bytes": 256 * 1024,
    "deadline_ms": 10000,
    "max_state_bytes": 4 * 1024 * 1024, "max_total_state_bytes": 32 * 1024 * 1024,
}


def _json_value(value, depth=0):
    if depth > 32:
        raise ValueError("JSON nesting exceeds 32 levels")
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if abs(value) > 9007199254740991:
            raise ValueError("canonical JSON integers must be safe integers")
        return
    if isinstance(value, str):
        value.encode("utf-8")  # Lone surrogates are not raw UTF-8 source text.
        return
    if isinstance(value, list):
        for item in value:
            _json_value(item, depth + 1)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for key, item in value.items():
            key.encode("utf-8")
            _json_value(item, depth + 1)
        return
    raise ValueError("canonical JSON requires strings, safe integers, booleans, lists and objects")


def _canonical(value):
    _json_value(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _text(value, label, *, optional=False, maximum=4096):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{label} needs a nonempty UTF-8 string of at most {maximum} bytes")
    return value


def _keys(value, required, optional=(), *, label):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise ValueError(f"invalid {label} keys")


def _names(value, label, maximum):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{label} must be a bounded list")
    result = [_text(item, label, maximum=256) for item in value]
    if len(result) != len(set(result)):
        raise ValueError(f"duplicate {label}")
    return result


def _limits(overrides):
    if overrides is None:
        return dict(DEFAULT_LIMITS)
    if not isinstance(overrides, dict) or set(overrides) - set(DEFAULT_LIMITS):
        raise ValueError("unknown evidence limits")
    limits = dict(DEFAULT_LIMITS)
    for name, value in overrides.items():
        if type(value) is not int or not 0 < value <= DEFAULT_LIMITS[name]:
            raise ValueError(f"{name} must be a positive integer no greater than {DEFAULT_LIMITS[name]}")
        limits[name] = value
    return limits


def _relative(value, *, directory=False):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("path must be a root-relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or (not directory and str(path) == "."):
        raise ValueError("path escapes its declared root")
    return path


@contextlib.contextmanager
def _directory(root, parts=(), *, create=False):
    """Walk with directory descriptors: neither reads nor publication follow symlinks."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _read(root, relative, maximum):
    path = _relative(str(relative))
    with _directory(root, path.parts[:-1]) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            size = os.fstat(stream.fileno())
            if not stat.S_ISREG(size.st_mode):
                raise ValueError(f"not a regular file: {relative}")
            if size.st_size > maximum:
                raise ValueError(f"file exceeds byte limit: {relative}")
            data = stream.read(maximum + 1)
            if len(data) > maximum:
                raise ValueError(f"file exceeds byte limit: {relative}")
            return data


def _publish(root, relative, data):
    """Publish complete bytes with no-clobber link; crash leaves no admitted partial file."""
    path = _relative(str(relative))
    with _directory(root, path.parts[:-1], create=True) as parent:
        temporary = ".pending-" + secrets.token_hex(16)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fchmod(stream.fileno(), 0o400)
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent,
                        follow_symlinks=False)
            except FileExistsError:
                if _read(root, path, len(data)) != data:
                    raise ValueError(f"immutable artifact conflicts: {relative}")
            os.fsync(parent)
        finally:
            os.unlink(temporary, dir_fd=parent)


def _decode(data, label):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{label} is not UTF-8") from error


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(data):
    try:
        value = json.loads(_decode(data, "artifact"), object_pairs_hook=_pairs,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError("invalid JSON number")))
        _json_value(value)
        return value
    except (RecursionError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid evidence JSON") from error


def _rows(value, label, maximum):
    if not isinstance(value, (dict, list)):
        raise ValueError(f"{label} must be an ID mapping or list")
    if len(value) > maximum:
        raise ValueError(f"too many {label}")
    if isinstance(value, dict):
        rows = []
        for key, entry in value.items():
            if not isinstance(entry, dict) or ("id" in entry and entry["id"] != key):
                raise ValueError(f"{label} mapping ID mismatch")
            rows.append(dict(entry, id=key))
    else:
        rows = value
    return rows


def _fields(fields, limits):
    if isinstance(fields, list):
        names = _names(fields, "field names", limits["max_fields"])
        fields = {name: {} for name in names}
    if not isinstance(fields, dict) or len(fields) > limits["max_fields"]:
        raise ValueError("fields must be a bounded mapping or list")
    result = {}
    for name, field in fields.items():
        _text(name, "field name", maximum=256)
        _keys(field, (), ("description", "source_roles"), label="field")
        roles = _names(field.get("source_roles", list(ROLES)), "source roles", len(ROLES))
        if set(roles) - set(ROLES):
            raise ValueError("unknown field source role")
        result[name] = {"description": _text(field.get("description", name), "field description"),
                        "source_roles": roles}
    return result


def _source(root, descriptor, limits):
    _keys(descriptor, ("role",), ("id", "path", "text", "origin", "revision", "snapshot_identity",
                                  "subject", "version", "refresh", "classification", "sensitive", "revision_path"),
          label="source descriptor")
    role = descriptor["role"]
    if role not in ROLES or (("path" in descriptor) == ("text" in descriptor)):
        raise ValueError("source needs a known role and exactly one of path or text")
    if "path" in descriptor:
        relative = _relative(descriptor["path"])
        if ARTIFACT_DIRECTORY in relative.parts:
            raise ValueError("artifact paths cannot be live origins")
        data = _read(root, relative, limits["max_source_bytes"])
        origin = {"kind": "file", "path": str(relative)}
        refresh = descriptor.get("refresh", "live")
    else:
        if not isinstance(descriptor["text"], str):
            raise ValueError("source text must be a UTF-8 string")
        data = descriptor["text"].encode("utf-8")
        if len(data) > limits["max_source_bytes"]:
            raise ValueError("source text exceeds byte limit")
        origin = {"kind": "external", "locator": _text(descriptor.get("origin"), "origin", optional=True)}
        refresh = descriptor.get("refresh", "snapshot")
        _text(descriptor.get("snapshot_identity"), "external snapshot identity")
    if refresh not in ("live", "snapshot") or (refresh == "live" and origin["kind"] != "file"):
        raise ValueError("live refresh requires a local file origin")
    text = _decode(data, "source")
    sha = hashlib.sha256(data).hexdigest()
    identity = _text(descriptor.get("snapshot_identity", sha), "snapshot identity")
    classification = descriptor.get("classification", "private")
    if "sensitive" in descriptor and type(descriptor["sensitive"]) is not bool:
        raise ValueError("sensitive must be boolean")
    if classification not in ("public", "private", "secret"):
        raise ValueError("unknown source classification")
    if descriptor.get("sensitive"):
        classification = "secret"
    revision = _text(descriptor.get("revision"), "revision", optional=True)
    revision_origin = None
    if "revision_path" in descriptor:
        revision_path = _relative(descriptor["revision_path"])
        if ARTIFACT_DIRECTORY in revision_path.parts:
            raise ValueError("artifact paths cannot be revision origins")
        revision_bytes = _read(root, revision_path, 4096)
        actual_revision = _text(_decode(revision_bytes, "revision marker"), "revision marker")
        if revision is not None and revision != actual_revision:
            raise ValueError("revision marker does not match declared revision")
        revision = actual_revision
        revision_origin = {"path": str(revision_path), "sha256": hashlib.sha256(revision_bytes).hexdigest()}
    eligible = role in DELETABLE_ROLES and refresh == "live" and origin["kind"] == "file"
    source = {"role": role, "sha256": sha, "byte_length": len(data), "codepoint_length": len(text),
              "snapshot": str(root / ARTIFACT_DIRECTORY / "snapshots" / (sha + ".utf8")),
              "origin": origin, "revision": revision, "revision_origin": revision_origin,
              "snapshot_identity": identity, "refresh": refresh,
              "subject": _text(descriptor.get("subject"), "subject", optional=True),
              "version": _text(descriptor.get("version"), "version", optional=True),
              "read_only": not eligible, "deletion_eligible": eligible,
              "classification": classification, "transport_admitted": classification != "secret"}
    source["id"] = _text(descriptor["id"] if "id" in descriptor else "src-" + _digest(source),
                         "source ID", maximum=256)
    return source, data


def _glob_match(parts, pattern):
    previous = [True] + [False] * len(parts)
    for component in pattern:
        current = [previous[0] if component == "**" else False]
        for index, part in enumerate(parts, 1):
            current.append((previous[index] or current[index - 1]) if component == "**"
                           else previous[index - 1] and fnmatch.fnmatchcase(part, component))
        previous = current
    return previous[-1]


def _inventory(root, descriptor, limits):
    _keys(descriptor, ("id", "directory", "pattern"), label="inventory descriptor")
    identifier = _text(descriptor["id"], "inventory ID", maximum=256)
    directory = _relative(descriptor["directory"], directory=True)
    pattern = str(_relative(descriptor["pattern"]))
    pattern_parts = PurePosixPath(pattern).parts
    if len(pattern_parts) > 32:
        raise ValueError("inventory pattern exceeds depth bound")
    # Inspect every traversed directory with no-follow descriptors, including no-match cases.
    files, visited, total = {}, 0, 0
    def walk(parts, relative_parts):
        nonlocal visited, total
        if len(relative_parts) > 64:
            raise ValueError("inventory exceeds directory depth bound")
        with _directory(root, parts) as fd:
            names = []
            with os.scandir(fd) as entries:
                for entry in entries:
                    if entry.name == ARTIFACT_DIRECTORY:
                        continue
                    visited += 1
                    if visited > limits["max_inventory_files"] * 4:
                        raise ValueError("inventory traversal exceeds bound")
                    names.append(entry.name)
            for name in sorted(names):
                mode = os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode
                relative = PurePosixPath(*relative_parts, name)
                if stat.S_ISLNK(mode):
                    raise ValueError("inventory contains a symlink")
                if stat.S_ISDIR(mode):
                    walk((*parts, name), (*relative_parts, name))
                elif stat.S_ISREG(mode) and _glob_match(relative.parts, pattern_parts):
                    if len(files) >= limits["max_inventory_files"]:
                        raise ValueError("inventory exceeds file count limit")
                    path = PurePosixPath(*parts, name)
                    data = _read(root, path, limits["max_source_bytes"])
                    total += len(data)
                    if total > limits["max_total_source_bytes"]:
                        raise ValueError("inventory exceeds total byte limit")
                    files[str(path)] = hashlib.sha256(data).hexdigest()
    walk(directory.parts, ())
    return {"id": identifier, "directory": str(directory), "pattern": pattern, "files": files}


def _missing(plan_digest, reason, status="missing"):
    return {"status": status, "value": None, "source_id": None, "start": None, "end": None,
            "reason": reason, "plan_digest": plan_digest, "occurrence": None}


def _ledger(records):
    entries = {}
    totals = {"records": len(records), "ready": 0, "unresolved": 0, "required_fields": 0,
              "unresolved_fields": 0, "required_questions": 0, "pending_questions": 0, "blocked_questions": 0}
    for identifier, record in records.items():
        fields = {name: item["status"] for name, item in record["observations"].items()}
        unresolved = sum(status != "candidate" for status in fields.values())
        disposition = "unresolved" if unresolved else "ready"
        questions = dict(record["questions"])
        entries[identifier] = {"fields": fields, "questions": questions, "disposition": disposition}
        totals[disposition] += 1
        totals["required_fields"] += len(fields)
        totals["unresolved_fields"] += unresolved
        totals["required_questions"] += len(questions)
        for status in questions.values():
            totals[status + "_questions"] += 1
    return {"records": entries, "totals": totals, "complete": False}


def prepare_evidence(records, fields, *, root, sources, limits=None):
    """Freeze all declared records and raw sources without making network requests."""
    limits = _limits(limits)
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("evidence root is not a directory")
    fields = _fields(fields, limits)
    inventories = []
    if isinstance(sources, dict) and set(sources) == {"items", "inventories"}:
        inventories = sources["inventories"]
        sources = sources["items"]
    if not isinstance(inventories, list) or len(inventories) > limits["max_sources"]:
        raise ValueError("inventories must be a bounded list")
    captured, payloads = {}, {}
    total = 0
    for descriptor in _rows(sources, "sources", limits["max_sources"]):
        source, data = _source(root, descriptor, limits)
        if source["id"] in captured:
            raise ValueError("duplicate source ID")
        captured[source["id"]] = source
        total += len(data)
        if total > limits["max_total_source_bytes"]:
            raise ValueError("sources exceed total byte limit")
        payloads[source["sha256"]] = data
    normalized = {}
    for record in _rows(records, "records", limits["max_records"]):
        _keys(record, ("id", "source_id", "required_fields", "required_questions"),
              ("dependencies", "subject", "version", "facts"), label="record")
        identifier = _text(record["id"], "record ID", maximum=256)
        source_id = _text(record["source_id"], "source ID", maximum=256)
        if identifier in normalized or source_id not in captured:
            raise ValueError("duplicate record ID or unknown original source")
        required = _names(record["required_fields"], "required fields", limits["max_fields"])
        if set(required) - set(fields):
            raise ValueError("unknown required field")
        questions = _names(record["required_questions"], "required questions", limits["max_fields"])
        dependencies = _names(record.get("dependencies", []), "dependencies", limits["max_sources"])
        dependencies = sorted(set(dependencies) | {source_id})
        if set(dependencies) - set(captured):
            raise ValueError("unknown dependency")
        facts = record.get("facts", {})
        if not isinstance(facts, dict) or len(_canonical(facts)) > limits["max_metadata_bytes"]:
            raise ValueError("facts must be a bounded JSON object")
        # Canonical roundtrip prevents the caller mutating nested input facts after capture.
        normalized[identifier] = {"id": identifier, "source_id": source_id,
                                  "required_fields": required, "required_questions": questions,
                                  "dependencies": dependencies,
                                  "subject": _text(record.get("subject"), "subject", optional=True),
                                  "version": _text(record.get("version"), "version", optional=True),
                                  "facts": _load_json(_canonical(facts))}
    frozen_inventory = {}
    for descriptor in inventories:
        inventory = _inventory(root, descriptor, limits)
        if inventory["id"] in frozen_inventory:
            raise ValueError("duplicate inventory ID")
        frozen_inventory[inventory["id"]] = inventory
    # Initial observations do not include plan_digest: doing so would create a hash cycle.
    deterministic_records = {}
    for identifier, record in normalized.items():
        deterministic_records[identifier] = {
            "facts": record["facts"],
            "observations": {name: _missing(None, "not extracted") for name in record["required_fields"]},
            "questions": {qid: "blocked" if record["required_fields"] else "pending"
                          for qid in record["required_questions"]},
            "dependencies": record["dependencies"],
        }
    plan = {"schema": PLAN_SCHEMA, "root": str(root), "artifact_root": str(root / ARTIFACT_DIRECTORY),
            "sources": captured, "records": normalized, "fields": fields, "limits": limits,
            "inventories": frozen_inventory,
            "deterministic": {"records": deterministic_records, "ledger": _ledger(deterministic_records)}}
    plan["plan_digest"] = _digest(plan)
    raw_plan = _canonical(plan)
    if len(raw_plan) > limits["max_result_bytes"]:
        raise ValueError("plan exceeds artifact byte limit")
    for sha, data in payloads.items():
        _publish(root, PurePosixPath(ARTIFACT_DIRECTORY, "snapshots", sha + ".utf8"), data)
    _publish(root, PurePosixPath(ARTIFACT_DIRECTORY, "plans", plan["plan_digest"] + ".json"), raw_plan)
    return plan


def _plan(evidence):
    if not isinstance(evidence, dict):
        raise ValueError("not an evidence artifact")
    if evidence.get("schema") == PLAN_SCHEMA:
        return evidence
    if evidence.get("schema") == ADOPTION_SCHEMA and isinstance(evidence.get("plan"), dict):
        if evidence["plan"].get("schema") == PLAN_SCHEMA:
            return evidence["plan"]
    raise ValueError("unknown evidence schema version")


def _snapshot_text(plan, source):
    relative = PurePosixPath(ARTIFACT_DIRECTORY, "snapshots", source["sha256"] + ".utf8")
    data = _read(Path(plan["root"]), relative, plan["limits"]["max_source_bytes"])
    if hashlib.sha256(data).hexdigest() != source["sha256"] or len(data) != source["byte_length"]:
        raise ValueError("snapshot changed during evidence use")
    text = _decode(data, "snapshot")
    if len(text) != source["codepoint_length"]:
        raise ValueError("snapshot codepoint length differs")
    return text


def validate_evidence(evidence):
    """Recheck immutable capture, all live dependencies and the declared inventory."""
    plan = _plan(evidence)
    problems, changed, missing, snapshots, inventory_changes = [], [], [], [], {}
    stale = set()
    result = {"ok": False, "integrity_ok": True, "problems": problems, "changed": changed, "missing": missing,
              "snapshot_changed": snapshots, "inventory_changes": inventory_changes,
              "stale_records": [], "plan_digest": plan.get("plan_digest"), "complete": False}
    try:
        _keys(plan, ("schema", "plan_digest", "root", "artifact_root", "sources", "records", "fields",
                     "limits", "inventories", "deterministic"), label="plan")
        if plan["plan_digest"] != _digest({key: value for key, value in plan.items() if key != "plan_digest"}):
            raise ValueError("plan digest does not match its contents")
        limits = _limits(plan["limits"])
        root = Path(plan["root"])
        if not root.is_absolute() or str(root / ARTIFACT_DIRECTORY) != plan["artifact_root"]:
            raise ValueError("artifact root does not match source root")
        recorded_plan = _read(root, PurePosixPath(ARTIFACT_DIRECTORY, "plans", plan["plan_digest"] + ".json"),
                              limits["max_result_bytes"])
        if recorded_plan != _canonical(plan):
            raise ValueError("stored immutable plan differs")
        if evidence["schema"] == ADOPTION_SCHEMA:
            if evidence.get("plan_digest") != plan["plan_digest"]:
                raise ValueError("adoption belongs to a different plan")
            if evidence.get("digest") != _digest({key: value for key, value in evidence.items() if key != "digest"}):
                raise ValueError("adoption digest does not match its contents")
            stored = _read(root, PurePosixPath(ARTIFACT_DIRECTORY, "adoptions", evidence["digest"] + ".json"),
                           limits["max_result_bytes"])
            if stored != _canonical(evidence):
                raise ValueError("stored immutable adoption differs")
            raw = _read(root, PurePosixPath(ARTIFACT_DIRECTORY, "results", evidence["result_digest"] + ".json"),
                        limits["max_result_bytes"])
            if hashlib.sha256(raw).hexdigest() != evidence["result_digest"]:
                raise ValueError("stored extraction result differs")
            if evidence["publication"] is not None:
                for relative, expected in evidence["publication"]["artifacts"].items():
                    captured = _read(root, relative, limits["max_result_bytes"])
                    if hashlib.sha256(captured).hexdigest() != expected:
                        raise ValueError("adapter publication artifact differs")
        for sid, source in plan["sources"].items():
            snapshot = PurePosixPath(ARTIFACT_DIRECTORY, "snapshots", source["sha256"] + ".utf8")
            try:
                if source["snapshot"] != str(root / snapshot):
                    raise ValueError("snapshot locator differs")
                data = _read(root, snapshot, limits["max_source_bytes"])
                if (hashlib.sha256(data).hexdigest() != source["sha256"] or len(data) != source["byte_length"]
                        or len(_decode(data, "snapshot")) != source["codepoint_length"]):
                    raise ValueError("snapshot content differs")
            except (OSError, ValueError) as error:
                snapshots.append(sid)
                result["integrity_ok"] = False
                problems.append(f"snapshot invalid: {sid}: {error}")
            if source["refresh"] == "live":
                try:
                    origin = source["origin"]
                    if origin["kind"] != "file":
                        raise ValueError("live source has no file origin")
                    data = _read(root, origin["path"], limits["max_source_bytes"])
                    if hashlib.sha256(data).hexdigest() != source["sha256"]:
                        changed.append(sid)
                        problems.append(f"source changed: {sid}")
                    if source["revision_origin"] is not None:
                        marker = source["revision_origin"]
                        revision_bytes = _read(root, marker["path"], 4096)
                        if hashlib.sha256(revision_bytes).hexdigest() != marker["sha256"]:
                            if sid not in changed:
                                changed.append(sid)
                            problems.append(f"source revision changed: {sid}")
                except FileNotFoundError:
                    missing.append(sid)
                    problems.append(f"source missing: {sid}")
                except (OSError, ValueError) as error:
                    if sid not in changed:
                        changed.append(sid)
                    problems.append(f"source unreadable: {sid}: {error}")
        for identifier, inventory in plan["inventories"].items():
            try:
                fresh = _inventory(root, {key: inventory[key] for key in ("id", "directory", "pattern")}, limits)
                old, new = inventory["files"], fresh["files"]
                delta = {"added": sorted(set(new) - set(old)), "removed": sorted(set(old) - set(new)),
                         "changed": sorted(name for name in set(new) & set(old) if new[name] != old[name])}
                if any(delta.values()):
                    inventory_changes[identifier] = delta
                    problems.append(f"inventory changed: {identifier}")
            except (OSError, ValueError, RecursionError) as error:
                inventory_changes[identifier] = {"error": str(error)}
                problems.append(f"inventory unreadable: {identifier}")
        affected = set(changed + missing + snapshots)
        for identifier, record in plan["records"].items():
            if inventory_changes or affected.intersection(record["dependencies"]):
                stale.add(identifier)
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as error:
        problems.append(f"evidence integrity failure: {error}")
        result["integrity_ok"] = False
        stale.update(plan.get("records", {}) if isinstance(plan.get("records"), dict) else ())
    result["ok"] = not problems
    result["stale_records"] = sorted(stale)
    return result


def _require_current(evidence):
    validation = validate_evidence(evidence)
    if not validation["ok"]:
        raise ValueError("stale or corrupt evidence: " + "; ".join(validation["problems"]))
    return validation


def _occurrence(text, source_id, start, end):
    lines = text.splitlines(keepends=True)
    offsets, offset = [], 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    first = bisect.bisect_right(offsets, start) - 1
    last = bisect.bisect_right(offsets, end - 1) - 1
    line_start = offsets[first]
    line_end = offsets[last] + len(lines[last])
    # Only four prefixes are encoded; no per-codepoint UTF-8 map allocation.
    return {"source_id": source_id, "start": start, "end": end,
            "byte_start": len(text[:start].encode("utf-8")), "byte_end": len(text[:end].encode("utf-8")),
            "line_start": first + 1, "line_end": last + 1,
            "line_quote": "\n".join(text.splitlines()[first:last + 1]),
            "line_byte_start": len(text[:line_start].encode("utf-8")),
            "line_byte_end": len(text[:line_end].encode("utf-8"))}


def _observation(plan, record, name, item, texts):
    _keys(item, ("status", "value", "source_id", "start", "end", "reason"), label="field result")
    if item["status"] not in STATUSES:
        raise ValueError("unknown field result status")
    if item["reason"] is not None:
        _text(item["reason"], "field reason", maximum=plan["limits"]["max_metadata_bytes"])
    if item["source_id"] is not None:
        _text(item["source_id"], "field source ID", maximum=256)
    value = item["value"]
    if value is not None and (not isinstance(value, str) or
                              len(value.encode("utf-8")) > plan["limits"]["max_value_bytes"]):
        raise ValueError("field value exceeds string bound")
    normalized = dict(item, plan_digest=plan["plan_digest"], occurrence=None)
    if value is None:
        normalized.update(status="missing", start=None, end=None, reason=item["reason"] or "null value")
        return normalized
    if item["status"] != "candidate":
        if item["start"] is not None or item["end"] is not None:
            raise ValueError("non-candidate field cannot have occurrence offsets")
        return normalized
    reason = None
    sid = item["source_id"]
    if not isinstance(sid, str) or sid not in plan["sources"] or sid not in record["dependencies"]:
        reason = "candidate source is not a declared record dependency"
    else:
        source = plan["sources"][sid]
        if source["role"] not in plan["fields"][name]["source_roles"]:
            reason = "candidate source role is not admitted for field"
        elif any(source[key] is not None and record[key] is not None and source[key] != record[key]
                 for key in ("subject", "version")):
            reason = "candidate source has the wrong subject or version"
    if reason is None:
        text = texts[sid]
        start, end = item["start"], item["end"]
        if not value:
            reason = "empty candidate is not an occurrence"
        elif start is None and end is None:
            start = text.find(value)
            if start < 0 or text.find(value, start + 1) >= 0:
                reason = "unlocated candidate is absent or nonunique"
            else:
                end = start + len(value)
        if reason is None:
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
                reason = "candidate offsets are outside raw codepoint bounds"
            elif text[start:end] != value:
                reason = "candidate value does not equal raw source occurrence"
            else:
                normalized.update(start=start, end=end, occurrence=_occurrence(text, sid, start, end))
    if reason is not None:
        normalized.update(status="rejected", reason=reason)
    return normalized


def _adapter_publication(plan, result, result_path, raw):
    """Adapter candidates need a committed publication and completed request outcomes."""
    provenance = result.get("provenance", {})
    if provenance.get("schema") != "jevify-extraction-receipt/1":
        if (str(provenance.get("schema", "")).startswith("jevify-extraction-")
                or (result_path.parent == Path(plan["artifact_root"]) / "results"
                    and result_path.stem.startswith(plan["plan_digest"]))):
            raise ValueError("unknown or missing extraction receipt schema")
        return None
    _keys(provenance, ("schema", "run_id", "generation", "disposition", "requested_model", "requested_provider",
                       "attempts", "request_attempts", "completed_requests", "original_records", "required_fields",
                       "input_bytes", "elapsed_ms", "usage_complete"), label="extraction accounting")
    record_count = len(plan["records"])
    field_count = sum(len(record["required_fields"]) for record in plan["records"].values())
    if (provenance["requested_model"] != "cloudflare/clairic" or provenance["requested_provider"] != "openrouter"
            or provenance["original_records"] != record_count or provenance["required_fields"] != field_count
            or provenance["usage_complete"] is not False):
        raise ValueError("extraction accounting does not match the frozen plan")
    root = Path(plan["root"])
    run = _text(provenance.get("run_id"), "extraction run ID", maximum=128)
    generation = provenance.get("generation")
    attempts = provenance.get("attempts")
    if (type(generation) is not int or not 0 <= generation <= 32 or not isinstance(attempts, list)
            or len(attempts) > plan["limits"]["max_requests"]):
        raise ValueError("invalid extraction generation or attempt bound")
    key = plan["plan_digest"] + (f"--{generation}" if generation else "")
    expected_path = root / ARTIFACT_DIRECTORY / "results" / (key + ".json")
    if result_path != expected_path:
        raise ValueError("adapter result is not at its frozen generation path")
    artifacts = {}
    def artifact(directory, name, maximum):
        relative = PurePosixPath(ARTIFACT_DIRECTORY, directory, name + ".json")
        data = _read(root, relative, maximum)
        artifacts[str(relative)] = hashlib.sha256(data).hexdigest()
        decoded = _load_json(data)
        if not isinstance(decoded, dict):
            raise ValueError("extraction artifact must be an object")
        return decoded
    receipt = artifact("attempts", key + "-99", plan["limits"]["max_metadata_bytes"])
    _keys(receipt, ("schema", "plan_digest", "run_id", "generation", "result_sha256", "summary"),
          label="publication receipt")
    raw_digest = hashlib.sha256(raw).hexdigest()
    if (receipt["schema"] != "jevify-extraction-publication/1" or receipt["plan_digest"] != plan["plan_digest"]
            or receipt["run_id"] != run or receipt["generation"] != generation
            or receipt["result_sha256"] != raw_digest):
        raise ValueError("adapter publication receipt identity mismatch")
    artifacts[str(expected_path.relative_to(root))] = raw_digest
    claim = artifact("attempts", key, plan["limits"]["max_metadata_bytes"])
    _keys(claim, ("schema", "plan_digest", "run_id", "generation", "disposition", "requested_model"), label="attempt reservation")
    if (claim.get("schema") != "jevify-extraction-attempt/1" or claim.get("plan_digest") != plan["plan_digest"]
            or claim.get("run_id") != run or claim.get("generation") != generation
            or claim.get("disposition") != "reserved" or claim.get("requested_model") != provenance.get("requested_model")):
        raise ValueError("adapter publication has no matching reservation")
    known_candidates, completed, input_bytes = set(), 0, 0
    for index, attempt in enumerate(attempts, 1):
        if not isinstance(attempt, dict) or type(attempt.get("completed")) is not bool:
            raise ValueError("invalid extraction attempt")
        _keys(attempt, ("attempt_id", "record_id", "source_id", "source_digest", "fields", "input_bytes",
                        "request_bytes", "disposition", "requested_model", "observed_model", "observed_provider",
                        "usage", "elapsed_ms", "completed"), label="extraction attempt")
        if (attempt["requested_model"] != provenance["requested_model"]
                or type(attempt["request_bytes"]) is not int or not 0 <= attempt["request_bytes"] <= plan["limits"]["max_request_bytes"]
                or type(attempt["elapsed_ms"]) is not int or not 0 <= attempt["elapsed_ms"] <= plan["limits"]["deadline_ms"]):
            raise ValueError("extraction attempt exceeds frozen request bounds")
        identifier, sid = attempt.get("record_id"), attempt.get("source_id")
        if (not isinstance(identifier, str) or identifier not in plan["records"] or not isinstance(sid, str)
                or sid not in plan["records"][identifier]["dependencies"]
                or attempt.get("attempt_id") != f"{run}:{index}"):
            raise ValueError("extraction attempt source identity mismatch")
        source = plan["sources"][sid]
        names = [name for name in plan["records"][identifier]["required_fields"]
                 if source["role"] in plan["fields"][name]["source_roles"]]
        if (attempt.get("source_digest") != source["sha256"] or attempt.get("fields") != len(names)
                or attempt.get("input_bytes") != source["byte_length"] or not source["transport_admitted"]):
            raise ValueError("extraction attempt does not match admitted source")
        intent = artifact("attempts", f"{key}-{index}", plan["limits"]["max_metadata_bytes"])
        if (intent.get("schema") != "jevify-extraction-intent/1" or intent.get("plan_digest") != plan["plan_digest"]
                or intent.get("run_id") != run or intent.get("generation") != generation
                or any(intent.get(name) != attempt.get(name) for name in
                       ("attempt_id", "record_id", "source_id", "source_digest", "fields", "input_bytes", "request_bytes", "requested_model"))):
            raise ValueError("extraction intent mismatch")
        outcome = artifact("attempts", f"{key}-{index}-0", plan["limits"]["max_result_bytes"])
        _keys(outcome, ("schema", "plan_digest", "run_id", "generation", "attempt", "observations"), label="attempt outcome")
        if (outcome["schema"] != "jevify-extraction-outcome/1" or outcome["plan_digest"] != plan["plan_digest"]
                or outcome["run_id"] != run or outcome["generation"] != generation or outcome["attempt"] != attempt):
            raise ValueError("extraction outcome mismatch")
        _keys(outcome["observations"], names, label="outcome observations")
        if attempt["completed"]:
            if attempt.get("disposition") not in ("complete", "rejected_occurrence", "cap_reached"):
                raise ValueError("failed or truncated attempt cannot be completed")
            completed += 1
        for name, observation in outcome["observations"].items():
            if not isinstance(observation, dict):
                raise ValueError("invalid extraction outcome observation")
            if observation.get("status") == "candidate":
                if not attempt["completed"] or observation.get("source_id") != sid:
                    raise ValueError("candidate has no completed matching source attempt")
                known_candidates.add(_canonical([identifier, name, observation]))
        input_bytes += source["byte_length"]
    candidates = 0
    for identifier, record in result["records"].items():
        _keys(record, ("observations",), label="adapter record")
        if not isinstance(record["observations"], dict):
            raise ValueError("invalid adapter observations")
        for name, observation in record["observations"].items():
            if isinstance(observation, dict) and observation.get("status") == "candidate":
                candidates += 1
                if _canonical([identifier, name, observation]) not in known_candidates:
                    raise ValueError("candidate is absent from completed request outcomes")
    disposition = provenance.get("disposition")
    if disposition not in ("complete", "disabled", "no_credential", "transport_failure", "invalid_response",
                            "truncated_response", "rejected_occurrence", "cap_reached"):
        raise ValueError("extraction disposition has no committed publication contract")
    if disposition in ("disabled", "no_credential") and (attempts or candidates):
        raise ValueError("zero-transmission extraction contains requests or candidates")
    expected_status = "complete" if disposition in ("complete", "rejected_occurrence") else "partial" if completed else "error"
    if (provenance.get("request_attempts") != len(attempts) or provenance.get("completed_requests") != completed
            or provenance.get("input_bytes") != input_bytes or input_bytes > plan["limits"]["max_input_bytes"]
            or result["status"] != expected_status):
        raise ValueError("extraction completion accounting mismatch")
    summary = receipt["summary"]
    expected_summary = {"disposition": disposition, "plan_digest": plan["plan_digest"], "run_id": run,
                        "generation": generation, "result_sha256": raw_digest, "result_path": str(expected_path),
                        "receipt_path": None, "accounting": provenance, "transmission_uncertain": False,
                        "candidates": candidates, "request_attempts": len(attempts),
                        "records": record_count, "fields": field_count, "reused": False}
    _keys(summary, expected_summary, label="publication summary")
    if any(summary.get(name) != value for name, value in expected_summary.items()):
        raise ValueError("publication summary does not bind the extraction result")
    return {"artifacts": artifacts}


def adopt_evidence(plan, result_path):
    """Validate extraction candidates; retain every original and every unresolved facet."""
    if not isinstance(plan, dict) or plan.get("schema") != PLAN_SCHEMA:
        raise ValueError("adoption requires a frozen evidence plan")
    _require_current(plan)
    result_path = Path(result_path).absolute()
    raw = _read(result_path.parent, result_path.name, plan["limits"]["max_result_bytes"])
    result = _load_json(raw)
    _keys(result, ("schema", "plan_digest", "status", "records"), ("provenance",), label="result")
    if result["schema"] != RESULT_SCHEMA or result["plan_digest"] != plan["plan_digest"]:
        raise ValueError("result schema or extraction plan identity mismatch")
    if result["status"] not in ("complete", "partial", "error") or not isinstance(result["records"], dict):
        raise ValueError("invalid extraction result status or records")
    if set(result["records"]) - set(plan["records"]):
        raise ValueError("unknown record in extraction result")
    provenance = result.get("provenance", {})
    if not isinstance(provenance, dict) or len(_canonical(provenance)) > plan["limits"]["max_metadata_bytes"]:
        raise ValueError("provenance must be a bounded JSON object")
    try:
        publication = _adapter_publication(plan, result, result_path, raw)
    except (OSError, KeyError, TypeError) as error:
        raise ValueError("adapter publication is incomplete or malformed") from error
    root = Path(plan["root"])
    texts = {sid: _snapshot_text(plan, source) for sid, source in plan["sources"].items()}
    records = {}
    for identifier, record in plan["records"].items():
        supplied = result["records"].get(identifier, {"observations": {}})
        _keys(supplied, ("observations",), label="record result")
        if not isinstance(supplied["observations"], dict) or set(supplied["observations"]) - set(record["required_fields"]):
            raise ValueError("unknown field in extraction result")
        observations = {}
        for name in record["required_fields"]:
            if name in supplied["observations"]:
                observations[name] = _observation(plan, record, name, supplied["observations"][name], texts)
            else:
                observations[name] = _missing(plan["plan_digest"], "extraction failed" if result["status"] == "error"
                                              else "required field not returned",
                                              "error" if result["status"] == "error" else "missing")
        ready = all(item["status"] == "candidate" for item in observations.values())
        records[identifier] = {"facts": record["facts"], "observations": observations,
                               "questions": {qid: "pending" if ready else "blocked" for qid in record["required_questions"]},
                               "dependencies": record["dependencies"]}
    canonical_result = _canonical(result)
    result_digest = hashlib.sha256(canonical_result).hexdigest()
    # Freeze nested plan/facts/provenance too; returned artifacts never alias caller input.
    evidence = _load_json(_canonical({"schema": ADOPTION_SCHEMA, "plan": plan, "plan_digest": plan["plan_digest"],
                                     "result_digest": result_digest, "records": records, "ledger": _ledger(records),
                                     "provenance": provenance, "publication": publication}))
    evidence["digest"] = _digest(evidence)
    data = _canonical(evidence)
    if len(data) > plan["limits"]["max_result_bytes"]:
        raise ValueError("adoption exceeds artifact byte limit")
    _require_current(plan)  # Reject an origin change during extraction/validation.
    _publish(root, PurePosixPath(ARTIFACT_DIRECTORY, "results", result_digest + ".json"), canonical_result)
    _publish(root, PurePosixPath(ARTIFACT_DIRECTORY, "adoptions", evidence["digest"] + ".json"), data)
    return evidence


def evidence_states(evidence, Q):
    """Render every original with source-bound observations and explicit unresolved facets."""
    validation = _require_current(evidence)
    plan = _plan(evidence)
    if not isinstance(Q, dict):
        raise ValueError("Q must map question IDs to questions")
    records = evidence["records"] if evidence["schema"] == ADOPTION_SCHEMA else plan["deterministic"]["records"]
    ledger = _load_json(_canonical(evidence["ledger"] if evidence["schema"] == ADOPTION_SCHEMA
                                   else plan["deterministic"]["ledger"]))
    states, outcomes, problems, unresolved = {}, {}, [], []
    source_texts = {}
    source_headers = {}
    for sid, source in plan["sources"].items():
        if source["transport_admitted"]:
            source_texts[sid] = _snapshot_text(plan, source)
            source_headers[sid] = "SOURCE " + _canonical(source).decode("utf-8")
    total_state_bytes = 0
    for identifier, record in plan["records"].items():
        missing_questions = [qid for qid in record["required_questions"] if qid not in Q]
        if missing_questions:
            problems.append(f"record {identifier} requires missing questions: {', '.join(missing_questions)}")
            for qid in missing_questions:
                if ledger["records"][identifier]["questions"][qid] != "blocked":
                    ledger["totals"]["pending_questions"] -= 1
                    ledger["totals"]["blocked_questions"] += 1
                ledger["records"][identifier]["questions"][qid] = "blocked"
        item = records[identifier]
        blocked_fields = [name for name, field in item["observations"].items() if field["status"] != "candidate"]
        if blocked_fields or missing_questions:
            unresolved.append(identifier)
        secret = any(not plan["sources"][sid]["transport_admitted"] for sid in record["dependencies"])
        chunks = []
        if not secret:
            chunks = [f"RECORD {identifier}", "Required questions: " + ", ".join(record["required_questions"]),
                      "DETERMINISTIC FACTS\n" + _canonical(item["facts"]).decode("utf-8")]
            for sid in record["dependencies"]:
                chunks.extend([source_headers[sid], source_texts[sid]])
            chunks.extend(["UNVERIFIED EXTRACTION OBSERVATIONS\n" + _canonical(item["observations"]).decode("utf-8"),
                           "QUESTION FACETS\n" + _canonical(ledger["records"][identifier]["questions"]).decode("utf-8"),
                           "An occurrence proves only text presence, not correctness or action authority."])
        state_bytes = sum(len(chunk.encode("utf-8")) for chunk in chunks) + 2 * max(0, len(chunks) - 1)
        oversize = (state_bytes > plan["limits"]["max_state_bytes"]
                    or total_state_bytes + state_bytes > plan["limits"]["max_total_state_bytes"])
        if oversize or secret:
            # No fragment is silently submitted. The original remains captured, and the
            # empty state is explicit in outcomes/ledger rather than dropping its ID.
            states[identifier] = ""
            problems.append(f"record {identifier} contains a secret dependency" if secret else
                            f"record {identifier} exceeds rendered evidence byte limits")
            if identifier not in unresolved:
                unresolved.append(identifier)
            for qid, status in ledger["records"][identifier]["questions"].items():
                if status == "pending":
                    ledger["totals"]["pending_questions"] -= 1
                    ledger["totals"]["blocked_questions"] += 1
                ledger["records"][identifier]["questions"][qid] = "blocked"
        else:
            states[identifier] = "\n\n".join(chunks)
            total_state_bytes += state_bytes
        if identifier in unresolved and ledger["records"][identifier]["disposition"] == "ready":
            ledger["records"][identifier]["disposition"] = "unresolved"
            ledger["totals"]["ready"] -= 1
            ledger["totals"]["unresolved"] += 1
        outcomes[identifier] = {"status": "secret" if secret else "oversize" if oversize else "unresolved" if identifier in unresolved else "ready",
                                "reason": "secret dependency withheld" if secret else "render exceeds byte limits" if oversize else
                                          "missing fields/questions" if identifier in unresolved else None,
                                "truncated": oversize}
    return {"states": states, "report": {"ok": not problems, "complete": False, "problems": problems,
                                           "unresolved_ids": unresolved, "evidence_digest": evidence.get("digest", plan["plan_digest"]),
                                           "validation": validation, "outcomes": outcomes, "ledger": ledger},
            "ledger": ledger, "evidence": evidence}


def assert_deletion_eligible(evidence, source_ids=None, *, paths=None):
    """Eligibility is an additional guard, never review approval or action authority."""
    _require_current(evidence)
    plan = _plan(evidence)
    sources = plan["sources"]
    selected = set()
    if source_ids is not None:
        selected.update(_names(source_ids, "deletion source IDs", plan["limits"]["max_sources"]))
    root = Path(plan["root"])
    if paths is not None:
        if not isinstance(paths, (list, tuple)):
            raise ValueError("deletion paths must be a list")
        for path in paths:
            path = Path(path)
            if not path.is_absolute():
                path = root / path
            canonical = path.resolve()
            if ARTIFACT_DIRECTORY in canonical.parts or canonical.is_relative_to(Path(plan["artifact_root"])):
                raise ValueError("immutable snapshots and evidence artifacts are never deletable")
            matches = [sid for sid, source in sources.items() if source["origin"]["kind"] == "file"
                       and (root / source["origin"]["path"]).resolve() == canonical]
            if not matches:
                raise ValueError("deletion target is not a declared source")
            selected.update(matches)
    if not selected:
        raise ValueError("deletion needs explicit source IDs or target paths")
    for sid in tuple(selected):
        if sid not in sources:
            raise ValueError("unknown deletion source ID")
        origin = sources[sid]["origin"]
        if origin["kind"] == "file":
            selected.update(other for other, source in sources.items() if source["origin"] == origin)
    for sid in selected:
        source = sources[sid]
        if (source["role"] not in DELETABLE_ROLES or source["origin"]["kind"] != "file"
                or source["refresh"] != "live" or source["read_only"] or not source["deletion_eligible"]):
            raise ValueError(f"source is read-only and cannot be deleted: {sid}")
    return {"eligible": sorted(selected), "grants_authority": False}
