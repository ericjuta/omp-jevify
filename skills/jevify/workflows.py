"""Provider-free source-backed preparation, refresh, and explicit passage imports.

Resolvers are caller-owned capabilities, not search instructions executed by this
module. A resolver receives a JSON request with ``record_id``, ``role``, ``hint``,
``depth``, and remaining ``budget``. It returns exactly one of:
``{"source_id": registered_id}``, ``{"source": source_descriptor}``,
``{"unresolved": reason}``, or ``{"alternatives": [descriptors...]}``.
A successful result may also declare ``references`` for bounded expansion.
No response is a permission to edit and no preparation function invokes a judge.
"""

import copy
import hashlib
import json
import re
from collections import deque
from pathlib import Path


EXTERNAL_SCHEMA = "jevify-external-evidence/1"
READINESS_VERSION = "jevify-readiness/1"
DEFAULT_LIMITS = {"source_bytes": 1_048_576, "lookups": 32, "depth": 4}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _strings(value, name):
    if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
        raise ValueError(f"{name} must be a list of nonempty strings")
    if len(set(value)) != len(value):
        raise ValueError(f"{name} must not contain duplicates")
    return value


def _limits(value):
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - set(DEFAULT_LIMITS):
        raise ValueError("workflow limits accept source_bytes, lookups, and depth only")
    result = {**DEFAULT_LIMITS, **value}
    if any(type(v) is not int or v < 0 for v in result.values()):
        raise ValueError("workflow limits must be nonnegative integers")
    return result


def import_external_evidence(envelope, *, allowed_origins, reopen=None, previous=None):
    """Validate one explicit OptChat export; never inspect a store or import rules.

    ``reopen(locator)`` must be a trusted source-owner adapter. It returns None
    when the original is unavailable, otherwise ``{origin, source, range, content,
    attribution}`` with the exact locator, bytes and original speaker category.
    Optional ``verified_status`` is ``{kind, value, provenance}``; kind must be
    ``user_decision``, ``tool_call_status`` or ``project_observation``. Tool-call
    success is deliberately not promoted to project success. Without reopening,
    all exported status remains an attributed claim, excluded from readiness.

    ``previous`` is a sequence of earlier results. Identical imports have the
    same source ID; changed bytes under the same locator are refused. Reopening
    again can revoke verification without mutating an earlier import.
    """
    required = {"schema", "origin", "source", "range", "sha256", "content",
                "completeness", "attribution"}
    if not isinstance(envelope, dict) or set(envelope) != required:
        raise ValueError("external evidence requires the exact versioned envelope fields")
    if envelope["schema"] != EXTERNAL_SCHEMA:
        raise ValueError("unsupported external evidence schema")
    origin = _text(envelope["origin"], "external origin")
    if isinstance(allowed_origins, str) or origin not in allowed_origins:
        raise ValueError("external origin is not explicitly allowed")
    source = envelope["source"]
    source_keys = {"kind", "store_id", "message_id", "snapshot_id", "snapshot_cutoff", "message_index"}
    if not isinstance(source, dict) or set(source) != source_keys or source["kind"] != "optchat":
        raise ValueError("external source must be an OptChat store/message/snapshot identity")
    for name in ("store_id", "message_id", "snapshot_id"):
        _text(source[name], name)
        if source[name].startswith(("file:", "local:", "zoom:", "history:", "../", "/")):
            raise ValueError("external identities cannot be local retrieval locators")
    for name in ("snapshot_cutoff", "message_index"):
        if type(source[name]) is not int or source[name] < 0:
            raise ValueError(f"{name} must be a nonnegative original-message sequence number")
    if source["message_index"] > source["snapshot_cutoff"]:
        raise ValueError("external message is past the exported snapshot cutoff")
    span = envelope["range"]
    if (not isinstance(span, dict) or set(span) != {"start", "end"}
            or any(type(span[k]) is not int for k in span)
            or not 0 <= span["start"] < span["end"]):
        raise ValueError("external range must be a nonempty half-open UTF-8 byte range")
    content = envelope["content"]
    if not isinstance(content, str):
        raise ValueError("external content must be UTF-8 text")
    raw = content.encode("utf-8")
    if len(raw) != span["end"] - span["start"]:
        raise ValueError("external byte range does not match content")
    sha = hashlib.sha256(raw).hexdigest()
    if envelope["sha256"] != sha:
        raise ValueError("external content digest does not match")
    completeness = envelope["completeness"]
    if (not isinstance(completeness, dict) or set(completeness) != {"complete", "omissions"}
            or type(completeness["complete"]) is not bool):
        raise ValueError("external completeness requires complete and omissions")
    _strings(completeness["omissions"], "omissions")
    if completeness["complete"] and completeness["omissions"]:
        raise ValueError("complete external capture cannot declare omissions")
    attribution = envelope["attribution"]
    if (not isinstance(attribution, dict) or set(attribution) != {"kind", "claimed_status", "provenance"}
            or attribution["kind"] not in ("user_decision", "assistant_proposal", "tool_report", "discussion")):
        raise ValueError("external attribution must distinguish decisions, proposals, tool reports and discussion")
    if attribution["claimed_status"] is not None:
        _text(attribution["claimed_status"], "claimed_status")
    if attribution["provenance"] is not None:
        _text(attribution["provenance"], "status provenance")
    locator = {"origin": origin, "source": copy.deepcopy(source), "range": copy.deepcopy(span)}
    identity = _digest(locator)
    for prior in previous or ():
        if prior["identity"] == identity and prior["envelope"]["sha256"] != sha:
            raise ValueError("conflicting bytes under an existing external identity")
    verification, reason, verified_status = "unverifiable", "original source not reopened", None
    if reopen is not None:
        try:
            original = reopen(copy.deepcopy(locator))
        except Exception as error:
            original = None
            reason = f"source-owner reopen failed ({type(error).__name__})"
        if original is not None:
            if not isinstance(original, dict):
                raise ValueError("source-owner reopen must return a mapping or None")
            if (original.get("source") != source or original.get("range") != span
                    or original.get("origin") != origin):
                raise ValueError("reopened external locator belongs to a different store or snapshot")
            if original.get("content") != content or original.get("attribution") != attribution["kind"]:
                raise ValueError("reopened bytes or speaker attribution do not match the export")
            verification, reason = "verified", None
            status = original.get("verified_status")
            if status is not None:
                if (not isinstance(status, dict) or set(status) != {"kind", "value", "provenance"}
                        or status["kind"] not in ("user_decision", "tool_call_status", "project_observation")):
                    raise ValueError("source-owner status has an unsupported provenance contract")
                _text(status["value"], "verified status")
                _text(status["provenance"], "verified status provenance")
                if attribution["kind"] == "assistant_proposal":
                    raise ValueError("assistant proposal cannot acquire verified acceptance or completion")
                if status["kind"] == "user_decision" and attribution["kind"] != "user_decision":
                    raise ValueError("only an original user decision supports decision provenance")
                if status["kind"] != "user_decision" and attribution["kind"] != "tool_report":
                    raise ValueError("observed outcomes require an original tool report")
                verified_status = copy.deepcopy(status)
    descriptor = {"id": "external-" + identity, "role": "external_history", "text": content,
                  "origin": json.dumps(locator, ensure_ascii=False, sort_keys=True),
                  "snapshot_identity": _digest(source), "refresh": "snapshot"}
    result = {"schema": EXTERNAL_SCHEMA, "identity": identity, "envelope": copy.deepcopy(envelope),
              "source": descriptor, "verification": verification, "reason": reason,
              "verified_status": verified_status, "grants_authority": False}
    result["digest"] = _digest(result)
    return result


_ROLE_SOURCE = {
    "original_comment": "original_comment", "current_code": "current_code",
    "base_code": "base_code", "governing_contract": "governing_contract",
    "related_finding": "original_comment", "callsite": "current_code",
    "callee": "helper", "old_contract": "governing_contract",
    "new_contract": "governing_contract", "wrapper": "helper", "helper": "helper",
    "consumer": "consumer", "external_history": "external_history",
}
_DEFAULT_REQUIREMENTS = {
    "review": {"triage": ["current_code", "governing_contract"],
               "introduced_by_diff": ["base_code", "current_code"],
               "duplicate": ["related_finding"]},
    "documentation": {"alignment": ["current_code", "governing_contract"]},
    "migration": {"migration": ["callsite", "callee", "old_contract", "new_contract"]},
    "issue": {"diagnosis": ["current_code", "governing_contract"]},
}
_RECORD_KEYS = {"id", "source_id", "subject", "version", "facts", "claim", "dependencies",
                "required_fields", "required_questions", "requirements", "references", "versions",
                "external_evidence"}


def _source_descriptor(source, root):
    allowed = {"id", "role", "path", "text", "origin", "revision", "snapshot_identity", "subject",
               "version", "refresh", "classification", "sensitive", "revision_path"}
    if not isinstance(source, dict) or set(source) - allowed:
        raise ValueError("source descriptors must use the declared evidence contract")
    source = copy.deepcopy(source)
    role = source.get("role")
    if role not in _ROLE_SOURCE:
        raise ValueError(f"unknown source role {role!r}")
    source["role"] = _ROLE_SOURCE[role]
    if ("text" in source) == ("path" in source):
        raise ValueError("source requires exactly one of path or text")
    if "path" in source:
        rel = source["path"]
        if not isinstance(rel, str) or not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise ValueError("source path must stay within the declared root")
        target = root / rel
        if not target.resolve().is_relative_to(root):
            raise ValueError("source path escapes the declared root")
        if any(part.is_symlink() for part in [target, *target.parents] if part.is_relative_to(root)):
            raise ValueError("source paths cannot traverse symlinks")
    elif not isinstance(source["text"], str):
        raise ValueError("source text must be a string")
    source.setdefault("id", "source-" + _digest(source))
    _text(source["id"], "source ID")
    return source



def _resolver_receipt(answer):
    # Keep identities/alternatives, not a second copy of every resolved body in
    # metadata. The exact consulted bytes are persisted once by the registry.
    def summary(value):
        if not isinstance(value, dict):
            return value if isinstance(value, (str, int, bool, type(None))) else {"malformed_type": type(value).__name__}
        result = {key: copy.deepcopy(value[key]) for key in ("id", "role", "path", "revision", "version", "subject")
                  if key in value}
        if isinstance(value.get("text"), str):
            raw = value["text"].encode("utf-8")
            result.update({"sha256": hashlib.sha256(raw).hexdigest(), "byte_length": len(raw)})
        return result
    if not isinstance(answer, dict):
        return {"malformed_type": type(answer).__name__}
    result = {key: copy.deepcopy(answer[key]) for key in ("source_id", "unresolved", "references") if key in answer}
    if "source" in answer:
        result["source"] = summary(answer["source"])
    if "alternatives" in answer:
        alternatives = answer["alternatives"]
        result["alternatives"] = [summary(value) for value in alternatives] if isinstance(alternatives, list) else "malformed"
    return result

def _source_size(source, root):
    return len(source["text"].encode("utf-8")) if "text" in source else (root / source["path"]).stat().st_size


def _registry(sources, root):
    inventories = []
    if isinstance(sources, dict) and set(sources) == {"items", "inventories"}:
        sources, inventories = sources["items"], copy.deepcopy(sources["inventories"])
    if isinstance(sources, dict):
        descriptors = []
        for identifier, source in sources.items():
            if source.get("id", identifier) != identifier:
                raise ValueError("source mapping key and ID disagree")
            descriptors.append({**source, "id": identifier})
    elif isinstance(sources, list):
        descriptors = sources
    else:
        raise ValueError("sources must be a list or ID-keyed mapping")
    registry = {}
    for descriptor in descriptors:
        source = _source_descriptor(descriptor, root)
        if source["id"] in registry:
            raise ValueError("duplicate source ID")
        registry[source["id"]] = source
    return registry, inventories


def _records(records):
    if isinstance(records, dict):
        result = []
        for identifier, record in records.items():
            if record.get("id", identifier) != identifier:
                raise ValueError("record mapping key and ID disagree")
            result.append({**copy.deepcopy(record), "id": identifier})
    elif isinstance(records, list):
        result = copy.deepcopy(records)
    else:
        raise ValueError("records must be a list or ID-keyed mapping")
    seen = set()
    for record in result:
        if not isinstance(record, dict) or set(record) - _RECORD_KEYS:
            raise ValueError("unsupported workflow record fields")
        identifier = _text(record.get("id"), "record ID")
        _text(record.get("source_id"), "original source ID")
        if identifier in seen:
            raise ValueError("duplicate record ID")
        seen.add(identifier)
    return result


def _requirements(record, kind):
    questions = record.get("required_questions", list(_DEFAULT_REQUIREMENTS[kind]))
    _strings(questions, "required_questions")
    supplied = record.get("requirements", {})
    if not isinstance(supplied, dict) or set(supplied) - set(questions):
        raise ValueError("requirements must map declared question IDs to required roles")
    requirements = {}
    for question in questions:
        defaults = _DEFAULT_REQUIREMENTS[kind].get(question)
        if defaults is None and question not in supplied:
            raise ValueError(f"question {question!r} requires an explicit role policy")
        extra = supplied.get(question, [])
        _strings(extra, "required roles")
        roles = sorted(set(defaults or ()) | set(extra))
        if any(role not in _ROLE_SOURCE for role in roles):
            raise ValueError("unknown required source role")
        requirements[question] = roles
    return requirements


def _prepare(records, *, kind, root, sources, resolver, limits, evidence_api):
    root = Path(root).resolve()
    budget = _limits(limits)
    registry, inventories = _registry(sources, root)
    selected, prepared, fields = {}, [], {}
    counters = {"lookups": 0, "source_bytes": 0}
    counted = set()
    for record in _records(records):
        identifier, original = record["id"], record["source_id"]
        if original not in registry:
            raise ValueError(f"record {identifier!r} has no original source")
        selected[original] = registry[original]
        imports = {}
        for imported in record.get("external_evidence", []):
            if (not isinstance(imported, dict) or imported.get("schema") != EXTERNAL_SCHEMA
                    or imported.get("digest") != _digest({k: v for k, v in imported.items() if k != "digest"})):
                raise ValueError("external evidence must come from the versioned importer")
            sid = imported["source"]["id"]
            if sid not in registry or registry[sid] != imported["source"]:
                raise ValueError("imported external source identity does not match the registry")
            imports[sid] = imported
        requirements = _requirements(record, kind)
        versions = record.get("versions", {})
        if not isinstance(versions, dict) or any(role not in _ROLE_SOURCE or not isinstance(v, str)
                                                 for role, v in versions.items()):
            raise ValueError("versions must map source roles to explicit versions")
        dependencies, issues, attempts = {}, [], []
        queue = deque()
        explicit = record.get("dependencies", {})
        if isinstance(explicit, list):
            _strings(explicit, "dependencies")
            explicit_map = {}
            for sid in explicit:
                if sid not in registry:
                    raise ValueError("dependency names an undeclared source")
                explicit_map.setdefault(registry[sid]["role"], []).append(sid)
            explicit = explicit_map
        if not isinstance(explicit, dict):
            raise ValueError("dependencies must map roles to source IDs")
        for role, ids in explicit.items():
            if role not in _ROLE_SOURCE:
                raise ValueError("unknown dependency role")
            _strings(ids, "role dependencies")
            for sid in ids:
                queue.append(({"role": role, "source_id": sid}, 0, ()))
        references = record.get("references", [])
        if not isinstance(references, list):
            raise ValueError("references must be a list")
        queue.extend((reference, 0, ()) for reference in references)
        visited = set()
        while queue:
            reference, depth, ancestors = queue.popleft()
            if (not isinstance(reference, dict) or "role" not in reference
                    or set(reference) - {"role", "hint", "source_id"}
                    or ("hint" in reference) == ("source_id" in reference)):
                raise ValueError("reference needs role and exactly one of hint or source_id")
            role = reference["role"]
            if role not in _ROLE_SOURCE:
                raise ValueError("unknown reference role")
            expected_version = versions.get(role, record.get("version") if role in {
                "current_code", "callsite", "callee", "new_contract", "governing_contract", "helper", "wrapper", "consumer"} else None)
            key = _digest(reference)
            def issue(status, reason):
                issues.append({"role": role, "status": status, "reason": reason})
            if key in ancestors:
                issue("expansion_exhausted", "cyclic dependency expansion cannot establish readiness")
                continue
            if key in visited:
                continue
            visited.add(key)
            if depth > budget["depth"]:
                issue("expansion_exhausted", "resolver expansion depth exhausted")
                continue
            more = []
            if "source_id" in reference:
                sid = reference["source_id"]
                if sid not in registry:
                    issue("missing_evidence", "source ID is not registered")
                    continue
                source = registry[sid]
            else:
                if resolver is None:
                    issue("missing_evidence", "reference has no supplied resolver")
                    continue
                if counters["lookups"] >= budget["lookups"]:
                    issue("expansion_exhausted", "resolver lookup budget exhausted")
                    continue
                request = {"record_id": identifier, "role": role, "hint": copy.deepcopy(reference["hint"]),
                           "subject": record.get("subject"), "version": expected_version, "depth": depth,
                           "budget": {"lookups": budget["lookups"] - counters["lookups"],
                                      "source_bytes": budget["source_bytes"] - counters["source_bytes"],
                                      "depth": budget["depth"] - depth}}
                counters["lookups"] += 1
                try:
                    answer = resolver(copy.deepcopy(request))
                except Exception as error:
                    issue("missing_evidence", f"resolver failed ({type(error).__name__})")
                    attempts.append({"request": request, "status": "resolver_error"})
                    continue
                attempts.append({"request": request, "answer": _resolver_receipt(answer)})
                if (not isinstance(answer, dict)
                        or set(answer) - {"source_id", "source", "unresolved", "alternatives", "references"}
                        or sum(k in answer for k in ("source_id", "source", "unresolved", "alternatives")) != 1):
                    issue("missing_evidence", "malformed resolver result")
                    continue
                if "unresolved" in answer:
                    reason = answer["unresolved"]
                    issue("missing_evidence", reason if isinstance(reason, str) and reason.strip() else "resolver supplied no unresolved reason")
                    continue
                if "alternatives" in answer:
                    issue("conflicting_evidence", "reference has ambiguous alternatives; none was selected")
                    continue
                more = answer.get("references", [])
                if (not isinstance(more, list) or any(not isinstance(child, dict)
                        or child.get("role") not in _ROLE_SOURCE
                        or set(child) - {"role", "hint", "source_id"}
                        or ("hint" in child) == ("source_id" in child) for child in more)):
                    issue("missing_evidence", "resolver references are malformed")
                    continue
                if "source" in answer:
                    try:
                        source = _source_descriptor(answer["source"], root)
                    except ValueError as error:
                        issue("missing_evidence", str(error))
                        continue
                    sid = source["id"]
                    if sid in registry and registry[sid] != source:
                        issue("conflicting_evidence", "source ID conflicts with its declared identity")
                        continue
                    registry[sid] = source
                else:
                    sid = answer["source_id"]
                    if not isinstance(sid, str) or sid not in registry:
                        issue("missing_evidence", "resolver returned an undeclared source")
                        continue
                    source = registry[sid]
            if source["role"] != _ROLE_SOURCE[role]:
                issue("conflicting_evidence", "resolved source has the wrong role")
                continue
            if (record.get("subject") is not None and source.get("subject") is not None
                    and record["subject"] != source["subject"]):
                issue("conflicting_evidence", "resolved source has a different subject")
                continue
            if expected_version is not None and source.get("version") != expected_version:
                issue("version_mismatch", "resolved source version does not match the declared requirement")
                continue
            if sid not in counted and sid != original:
                try:
                    size = _source_size(source, root)
                except OSError:
                    issue("missing_evidence", "resolved source is unavailable")
                    continue
                if counters["source_bytes"] + size > budget["source_bytes"]:
                    issue("expansion_exhausted", "supporting source byte budget exhausted")
                    continue
                counters["source_bytes"] += size
                counted.add(sid)
            selected[sid] = source
            dependencies.setdefault(role, []).append(sid)
            queue.extend((child, depth + 1, (*ancestors, key)) for child in more)
        for role, ids in dependencies.items():
            dependencies[role] = sorted(set(ids))
            source_versions = {registry[sid].get("version") for sid in ids}
            if len(source_versions) > 1:
                issues.append({"role": role, "status": "version_mismatch",
                               "reason": "same dependency role resolves to incompatible versions"})
        facts = copy.deepcopy(record.get("facts", {}))
        if not isinstance(facts, dict) or "_workflow" in facts:
            raise ValueError("facts must be a mapping without reserved _workflow metadata")
        if "claim" in record:
            facts["claim"] = record["claim"]
        if facts.get("claim") is not None:
            facts["claim"] = _text(facts["claim"], "claim")
        required_fields = record.get("required_fields", [] if facts.get("claim") is not None or kind == "issue" else ["claim"])
        _strings(required_fields, "required_fields")
        required_fields = [field for field in required_fields if facts.get(field) is None]
        for field in required_fields:
            spec = fields.setdefault(field, {"description": "Verbatim " + field + "; preserve negation, conditions and exceptions",
                                             "source_roles": []})
            if registry[original]["role"] not in spec["source_roles"]:
                spec["source_roles"].append(registry[original]["role"])
        readiness = {}
        for question, required_roles in requirements.items():
            # Resolved expansion dependencies are mandatory too: an unresolved
            # wrapper cannot be hidden by a complete-looking renamed call site.
            relevant = set(required_roles)
            if kind == "migration":
                relevant.update(dependencies)
                relevant.update(reference["role"] for reference in references)
                relevant.update(entry["role"] for entry in issues)
            elif question != "duplicate":
                expansion_roles = {"wrapper", "helper", "consumer", "external_history"}
                relevant.update(role for role in dependencies if role in expansion_roles)
                relevant.update(reference["role"] for reference in references if reference["role"] in expansion_roles)
                relevant.update(entry["role"] for entry in issues if entry["role"] in expansion_roles)
            found_issues = [entry for entry in issues if entry["role"] in relevant]
            missing = sorted(role for role in relevant if not dependencies.get(role))
            if question == "introduced_by_diff":
                for role in ("base_code", "current_code"):
                    if any(not registry[sid].get("revision") for sid in dependencies.get(role, [])):
                        found_issues.append({"role": role, "status": "version_mismatch",
                                             "reason": "introduced_by_diff requires explicit base/current revisions"})
            if kind == "migration":
                for role in ("old_contract", "new_contract"):
                    if any(not (registry[sid].get("version") or registry[sid].get("revision"))
                           for sid in dependencies.get(role, [])):
                        found_issues.append({"role": role, "status": "version_mismatch",
                                             "reason": "migration contracts require explicit versions or revisions"})
            statuses = {entry["status"] for entry in found_issues}
            status = next((s for s in ("conflicting_evidence", "version_mismatch", "expansion_exhausted", "missing_evidence")
                           if s in statuses), "missing_evidence" if missing else "ready")
            readiness[question] = {"status": status, "missing_roles": missing,
                                   "reasons": found_issues,
                                   "dependencies": sorted({original} | {sid for role in relevant
                                                                        for sid in dependencies.get(role, [])})}
        external_ids = {sid for ids in dependencies.values() for sid in ids
                        if registry[sid]["role"] == "external_history"}
        if registry[original]["role"] == "external_history":
            external_ids.add(original)
        for sid in external_ids:
            imported = imports.get(sid)
            if (imported is None or imported["verification"] != "verified"
                    or not imported["envelope"]["completeness"]["complete"]):
                for question, readiness_row in readiness.items():
                    if sid == original or sid in readiness_row["dependencies"]:
                        if readiness_row["status"] == "ready":
                            readiness_row["status"] = "missing_evidence"
                        readiness_row["reasons"].append({"role": "external_history", "status": "missing_evidence",
                                                         "reason": "external original is unverifiable or incomplete"})
        import_metadata = {sid: {**{key: value for key, value in imported.items() if key not in {"source", "envelope"}},
                                 "envelope": {key: value for key, value in imported["envelope"].items() if key != "content"}}
                           for sid, imported in imports.items()}
        facts["_workflow"] = {"kind": kind, "readiness_version": READINESS_VERSION, "limits": budget,
                               "requirements": requirements, "readiness": readiness,
                               "dependencies": dependencies, "resolution": attempts,
                               "external_evidence": import_metadata,
                               "versions": versions, "grants_authority": False}
        prepared.append({"id": identifier, "source_id": original, "subject": record.get("subject"),
                         "version": record.get("version"), "facts": facts,
                         "required_fields": required_fields, "required_questions": list(requirements),
                         "dependencies": sorted({original} | {sid for ids in dependencies.values() for sid in ids})})
    return evidence_api.prepare_evidence(prepared, fields, root=root,
                                         sources={"items": list(selected.values()), "inventories": inventories})


def prepare_review(records, *, root, sources, resolver=None, limits=None, evidence_api):
    """Prepare every original review comment and freeze question-specific readiness.

    Defaults require triage (current code + contract), introduced_by_diff (explicit
    base + current revisions), and duplicate (resolved related finding). Declare
    required_questions to select a smaller scope; missing evidence is never invalid.
    Structured ``claim``/``facts`` bypass extraction; prose requires a claim field.
    """
    return _prepare(records, kind="review", root=root, sources=sources, resolver=resolver,
                    limits=limits, evidence_api=evidence_api)


def prepare_migration(records, *, root, sources, resolver=None, limits=None, evidence_api):
    """Prepare inventoried call sites, callees, versioned contracts and expansions.

    Logical dependency roles callsite/callee/old_contract/new_contract are required;
    wrapper/helper/consumer references discovered during expansion also block until
    resolved. Supply explicit source inventories for automatic addition/removal drift.
    """
    return _prepare(records, kind="migration", root=root, sources=sources, resolver=resolver,
                    limits=limits, evidence_api=evidence_api)


def prepare_issue(records, *, root, sources, resolver=None, limits=None, evidence_api):
    """Issue/log intake using the same captures, not an executable log interpreter.

    Structured symptom/version/environment/reproduction fields in facts bypass
    extraction. Missing fields stay null and are listed explicitly; reported tool
    success is a report, not an observed project outcome.
    """
    records = _records(records)
    names = ("reported_symptom", "observed_version", "environment", "reproduction_steps")
    for record in records:
        facts = record.setdefault("facts", {})
        facts["missing_information"] = [name for name in names if facts.get(name) is None]
        for name in names:
            facts.setdefault(name, None)
    return _prepare(records, kind="issue", root=root, sources=sources, resolver=resolver,
                    limits=limits, evidence_api=evidence_api)


def _markdown_sections(text):
    """Inventory enclosing Markdown sections, ignoring headings inside fenced code."""
    lines = text.splitlines(keepends=True)
    boundaries, fence = [], None
    for index, line in enumerate(lines):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            run = marker.group(1)
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence):
                fence = None
            continue
        if fence is not None:
            continue
        heading = re.match(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*(?:\r?\n)?$", line)
        if heading:
            boundaries.append((index, heading.group(2).strip()))
        elif (index > 0 and re.match(r"^ {0,3}(?:=+|-+)[ \t]*(?:\r?\n)?$", line)
              and lines[index - 1].strip() and not re.match(r"^\s*[-*+]\s", lines[index - 1])):
            if not boundaries or boundaries[-1][0] != index - 1:
                boundaries.append((index - 1, lines[index - 1].strip()))
    if not boundaries or boundaries[0][0] > 0:
        boundaries.insert(0, (0, "preamble"))
    occurrences = {}
    sections = []
    for pos, (start, heading) in enumerate(boundaries):
        end = boundaries[pos + 1][0] if pos + 1 < len(boundaries) else len(lines)
        passage = "".join(lines[start:end])
        if not passage.strip():
            continue
        occurrences[heading] = occurrences.get(heading, 0) + 1
        sections.append({"key": _digest([heading, occurrences[heading]])[:20], "heading": heading,
                         "line": start + 1, "end_line": end, "passage": passage})
    return sections


def prepare_documentation(documents, *, root, sources, resolver=None, limits=None, evidence_api):
    """Inventory Markdown sections and retain every enclosing conditional passage.

    documents uses the same descriptor keys as prepare_review. Each source is
    parsed deterministically into heading-delimited sections (code fences are not
    headings); each section becomes a stable record. Counts describe inventory,
    never semantic claim recall. A complete section is a candidate claim, not an
    assertion that every natural-language promise was detected.
    """
    root_path = Path(root).resolve()
    registry, _ = _registry(sources, root_path)
    records = []
    for document in _records(documents):
        if document["source_id"] not in registry:
            raise ValueError("documentation source is not registered")
        source = registry[document["source_id"]]
        text = source["text"] if "text" in source else (root_path / source["path"]).read_bytes().decode("utf-8")
        sections = _markdown_sections(text)
        # Empty documents remain visible instead of manufacturing a candidate.
        if not sections:
            records.append({**document, "required_fields": ["claim"],
                            "facts": {**document.get("facts", {}), "inventory_sections": 0,
                                      "semantic_claim_recall": "unknown", "empty_document": True}})
        for section in sections:
            facts = {**document.get("facts", {}), "enclosing_passage": section["passage"],
                     "heading": section["heading"], "inventory_document": document["id"],
                     "inventory_sections": len(sections), "semantic_claim_recall": "unknown",
                     "source_span": {"line": section["line"], "end_line": section["end_line"]}}
            records.append({**document, "id": document["id"] + ":section:" + section["key"],
                            "claim": section["passage"], "required_fields": [], "facts": facts})
    return _prepare(records, kind="documentation", root=root, sources=sources, resolver=resolver,
                    limits=limits, evidence_api=evidence_api)


def _plan(evidence):
    if not isinstance(evidence, dict):
        raise ValueError("evidence must be a versioned artifact")
    if evidence.get("schema") == "jevify-evidence/1":
        return evidence
    if evidence.get("schema") == "jevify-evidence-adoption/1":
        return evidence["plan"]
    raise ValueError("unsupported evidence schema")


def evidence_states(evidence, Q, *, evidence_api):
    """Render source-backed packets and admit only complete question facets.

    ``states`` is safe for run(states, Q): every selected record is ready for
    every supplied question. ``states_by_question`` admits independent facets;
    pass ONLY {qid: Q[qid]} for such a batch. ``admissions`` and ``scope`` retain
    all blocked questions. Readiness is prerequisite evidence, not a verdict.
    """
    result = evidence_api.evidence_states(evidence, Q)
    plan = _plan(evidence)
    if not any("_workflow" in record["facts"] for record in plan["records"].values()):
        return result
    admissions, by_question, complete_states = {}, {qid: {} for qid in Q}, {}
    rendered_bytes = 0
    for identifier, record in plan["records"].items():
        workflow = record["facts"].get("_workflow")
        if workflow is None:
            raise ValueError("workflow rendering requires prepared workflow records")
        if workflow.get("readiness_version") != READINESS_VERSION:
            raise ValueError("unsupported workflow readiness policy")
        observations = evidence.get("records", {}).get(identifier, {}).get("observations", {})
        observations = {name: {key: value for key, value in observation.items() if key != "plan_digest"}
                        for name, observation in observations.items()}
        missing_fields = [name for name in record["required_fields"]
                          if observations.get(name, {}).get("status") != "candidate"]
        facets = {}
        for question in record["required_questions"]:
            readiness = copy.deepcopy(workflow["readiness"][question])
            readiness["missing_fields"] = list(missing_fields)
            if missing_fields and readiness["status"] == "ready":
                readiness["status"] = "missing_evidence"
            if question not in Q:
                readiness["status"] = "missing_evidence"
                readiness["reasons"].append({"role": "rubric", "status": "missing_evidence",
                                             "reason": "required question absent from the supplied rubric"})
            readiness["disposition"] = "pending_judgment" if readiness["status"] == "ready" else "blocked"
            readiness["facts"] = {"readiness": readiness["status"]}
            readiness["model_observations"] = copy.deepcopy(observations)
            readiness["judgment"] = None
            readiness["reviewer_decision"] = None
            facets[question] = readiness
        admissions[identifier] = facets
        base_outcome = result["report"]["outcomes"].get(identifier, {})
        base_oversize = base_outcome.get("truncated", False) or base_outcome.get("status") == "oversize"
        secret = any(not plan["sources"][sid]["transport_admitted"] for sid in record["dependencies"])
        withheld = (base_oversize or secret or base_outcome.get("status") not in {"ready", "unresolved"}
                    or not result["states"].get(identifier)
                    or plan["sources"][record["source_id"]]["byte_length"] == 0)
        if withheld:
            # Base admission is authoritative. Never reconstruct a withheld
            # payload by rereading immutable snapshots through our renderer.
            for readiness in facets.values():
                readiness["status"] = "expansion_exhausted" if base_oversize else "missing_evidence"
                readiness["disposition"] = "blocked"
                readiness["facts"]["readiness"] = readiness["status"]
                readiness["reasons"].append({"role": "packet", "status": readiness["status"],
                                             "reason": "base evidence admission withheld the source packet"})
            continue
        packet = _render_packet(evidence, identifier)
        packet += "\nWorkflow prerequisites (not model verdicts):\n" + json.dumps(facets, ensure_ascii=False, sort_keys=True)
        packet += "\nCandidate claims are attributed, not established facts. Missing evidence is NOT invalid. Grants authority: false."
        packet_bytes = len(packet.encode("utf-8"))
        oversize = (packet_bytes > plan["limits"]["max_state_bytes"]
                    or rendered_bytes + packet_bytes > plan["limits"]["max_total_state_bytes"])
        if oversize:
            for readiness in facets.values():
                readiness["status"] = "expansion_exhausted"
                readiness["disposition"] = "blocked"
                readiness["facts"]["readiness"] = readiness["status"]
                readiness["reasons"].append({"role": "packet", "status": readiness["status"],
                                             "reason": "render budget exhausted"})
        else:
            rendered_bytes += packet_bytes
        for question, readiness in facets.items():
            if question in Q and readiness["status"] == "ready":
                by_question[question][identifier] = packet
        if all(question in facets and facets[question]["status"] == "ready" for question in Q):
            complete_states[identifier] = packet
    scope = {"records": {identifier: {"questions": {qid: facet["disposition"] for qid, facet in facets.items()},
                                      "disposition": "ready" if all(f["status"] == "ready" for f in facets.values()) else "unresolved"}
                          for identifier, facets in admissions.items()},
             "totals": {"records": len(admissions),
                        "required_questions": sum(len(facets) for facets in admissions.values()),
                        "blocked_questions": sum(f["status"] != "ready" for facets in admissions.values() for f in facets.values())},
             "complete": False, "grants_authority": False}
    ledger = copy.deepcopy(result["ledger"])
    for identifier, facets in admissions.items():
        ledger["records"][identifier]["questions"] = {
            question: "pending" if facet["status"] == "ready" else "blocked" for question, facet in facets.items()}
        ledger["records"][identifier]["disposition"] = scope["records"][identifier]["disposition"]
    unresolved = [identifier for identifier, row in scope["records"].items() if row["disposition"] != "ready"]
    ledger["totals"].update({"ready": len(admissions) - len(unresolved), "unresolved": len(unresolved),
                            "blocked_questions": scope["totals"]["blocked_questions"],
                            "pending_questions": scope["totals"]["required_questions"] - scope["totals"]["blocked_questions"]})
    report = {**result["report"], "admissions": admissions, "ledger": ledger,
              "unresolved_ids": unresolved, "ok": result["report"]["ok"] and not unresolved,
              "complete": False}
    return {**result, "states": complete_states, "states_by_question": by_question, "ledger": ledger,
            "admissions": admissions, "scope": scope, "report": report}


def workflow_review(evidence, Q, *, helper, evidence_api):
    """Materialize the existing extraction/inbox interface, never another store.

    Pass returned manifest/items to review_queue(..., evidence=evidence). All
    items initially omit question, so unresolved facets need no fabricated model
    row. batches[qid] supplies a real single-question plan, admitted states,
    extraction manifest, and items with question set only for admitted states.
    Use helper.adopt(items, batch['states'], batch['Q'], extraction=batch['manifest'])
    or await helper.run(..., extraction=batch['manifest']); pass resulting rows
    and the same batch manifest/Q to review_queue. Never merge unlike-qhash rows.
    Original IDs remain unit IDs for dependency-specific stale_records invalidation.
    """
    rendered = evidence_states(evidence, Q, evidence_api=evidence_api)
    plan = _plan(evidence)
    root = Path(plan["root"])
    units, items = {}, {}
    for identifier, record in plan["records"].items():
        source = plan["sources"][record["source_id"]]
        outcome = rendered["report"]["outcomes"].get(identifier, {})
        if (not source["byte_length"] or source["codepoint_length"] > helper.CAP
                or any(not plan["sources"][sid]["transport_admitted"] for sid in record["dependencies"])
                or outcome.get("truncated", False)
                or outcome.get("status") not in {"ready", "unresolved"}):
            # Do not read rejected originals into claims or poison an otherwise
            # usable inbox. Their IDs remain in scope and uncitable_records.
            continue
        snapshot = Path(source["snapshot"])
        raw = snapshot.read_bytes().decode("utf-8")
        lines = raw.splitlines()
        # An empty original has no legal legacy line citation. Keep its scope
        # disposition visible and do not invent a one-line excerpt.
        if not lines or not raw.strip():
            continue
        span = record["facts"].get("source_span", {"line": 1, "end_line": len(lines)})
        units[identifier] = {"file": snapshot.relative_to(root).as_posix(), **span,
                             "read_only": True, "deletion_eligible": False,
                             "role": source["role"], "source_id": source["id"]}
        for question in record["required_questions"]:
            readiness = rendered["admissions"][identifier][question]
            claim = record["facts"].get("claim") or raw
            items[_digest([identifier, question])] = {
                "unit": identifier,
                "claim": f"{question}: {claim}\nPreparation: {readiness['status']}; "
                         f"missing roles: {', '.join(readiness['missing_roles']) or 'none'}. "
                         "This claim grants no action authority."}
    states = {identifier: value for identifier, value in rendered["states"].items() if identifier in units}
    outcomes = {identifier: {"truncated": False,
                            "reason": None if identifier in states else "required evidence question unresolved"}
                for identifier in units}
    manifest = helper.extraction(units, states, root=str(root), outcomes=outcomes, evidence=evidence)
    batches = {}
    for question, question_states in rendered["states_by_question"].items():
        question_states = {identifier: packet for identifier, packet in question_states.items() if identifier in units}
        question_items = {}
        for identifier, record in plan["records"].items():
            item_id = _digest([identifier, question])
            if item_id not in items:
                continue
            question_items[item_id] = copy.deepcopy(items[item_id])
            if identifier in question_states:
                question_items[item_id]["question"] = question
        question_manifest = helper.extraction(units, question_states, root=str(root), outcomes=outcomes,
                                              evidence=evidence)
        question_rubric = {question: Q[question]}
        batches[question] = {"Q": question_rubric, "states": question_states,
                             "manifest": question_manifest, "items": question_items,
                             "plan": helper.plan(question_states, question_rubric) if question_states else None,
                             "evidence": evidence, "scope": rendered["scope"]}
    return {"manifest": manifest, "items": items, "units": units, "states": states, "batches": batches,
            "states_by_question": rendered["states_by_question"], "admissions": rendered["admissions"],
            "scope": rendered["scope"], "evidence": evidence,
            "uncitable_records": sorted(set(plan["records"]) - set(units)), "grants_authority": False}




def review_scope(evidence, Q, items, *, evidence_api):
    """Inbox prerequisite guard, independent of reviewer decisions and authority.

    A ready facet can be confirmed while a sibling lacks evidence, but the unit
    is not approved/settled. Manual items need either a question binding or the
    deterministic workflow_review item key to identify their required facet.
    """
    plan = _plan(evidence)
    rendered = evidence_states(evidence, Q, evidence_api=evidence_api)
    incomplete, blocked = {}, {}
    for identifier, record in plan["records"].items():
        reasons = []
        observations = evidence.get("records", {}).get(identifier, {}).get("observations", {})
        fields_missing = [field for field in record["required_fields"]
                          if observations.get(field, {}).get("status") != "candidate"]
        if fields_missing:
            reasons.append("missing required fields: " + ", ".join(fields_missing))
        for question in record["required_questions"]:
            matching = [item_id for item_id, item in items.items()
                        if item.get("unit") == identifier
                        and (item.get("question") == question if item.get("question") is not None
                             else item_id == _digest([identifier, question]))]
            if not matching:
                reasons.append(f"required question has no review item: {question}")
            facet = rendered.get("admissions", {}).get(identifier, {}).get(question)
            missing = fields_missing or question not in Q or (facet is not None and facet["status"] != "ready")
            if missing:
                reason = f"required question lacks evidence or rubric: {question}"
                reasons.append(reason)
                for item_id in matching:
                    blocked.setdefault(item_id, []).append(reason)
        if not any(item.get("unit") == identifier for item in items.values()):
            reasons.append("original record has no citable review item")
        if reasons:
            incomplete[identifier] = reasons
    return {"incomplete_units": incomplete, "blocked_items": blocked,
            "complete": not incomplete, "grants_authority": False}


def extraction_request(evidence, *, evidence_api):
    """Return the exact installed bridge request, or None for deterministic input.

    The host may call jevify_extract with this request by default unless the user
    opted out, then adopt its result_path. This library never invokes transport.
    Structured facts have no required extraction fields and bypass the bridge.
    """
    check = evidence_api.validate_evidence(evidence)
    if not check["ok"]:
        raise ValueError("extraction requires current, intact evidence")
    plan = _plan(evidence)
    missing = []
    for identifier, record in plan["records"].items():
        observations = evidence.get("records", {}).get(identifier, {}).get("observations", {})
        if any(observations.get(field, {}).get("status") != "candidate" for field in record["required_fields"]):
            if any(not plan["sources"][sid]["transport_admitted"] for sid in record["dependencies"]):
                raise ValueError("extraction source transport is not admitted")
            missing.append(identifier)
    return {"artifact_root": plan["artifact_root"], "plan_digest": plan["plan_digest"]} if missing else None

def _rubric_digest(value):
    def ordered(item):
        if isinstance(item, dict):
            return {"ordered_mapping": [[key, ordered(val)] for key, val in item.items()]}
        if isinstance(item, list):
            return [ordered(val) for val in item]
        return item
    return _digest(ordered(value))


def _render_packet(evidence, identifier):
    plan = _plan(evidence)
    record = plan["records"][identifier]
    sources = []
    for sid in record["dependencies"]:
        source = plan["sources"][sid]
        sources.append({"id": sid, "role": source["role"], "sha256": source["sha256"],
                        "revision": source["revision"], "snapshot_identity": source["snapshot_identity"],
                        "subject": source["subject"], "version": source["version"],
                        "original": sid == record["source_id"],
                        "text": Path(source["snapshot"]).read_bytes().decode("utf-8")})
    observations = evidence.get("records", {}).get(identifier, {}).get("observations", {})
    observations = {name: {key: val for key, val in row.items() if key != "plan_digest"}
                    for name, row in observations.items()}
    return json.dumps({"record": identifier, "facts": record["facts"], "model_observations": observations,
                       "sources": sources, "grants_authority": False}, ensure_ascii=False, sort_keys=True)

def _record_identity(evidence, identifier):
    plan = _plan(evidence)
    record = plan["records"][identifier]
    source_keys = ("id", "role", "sha256", "byte_length", "origin", "revision", "revision_origin", "snapshot_identity",
                   "refresh", "subject", "version", "read_only", "deletion_eligible", "classification", "transport_admitted")
    sources = {sid: {key: plan["sources"][sid][key] for key in source_keys}
               for sid in record["dependencies"]}
    observations = evidence.get("records", {}).get(identifier, {}).get("observations", {})
    # Occurrences bind exact source bytes. The whole-plan digest is deliberately
    # excluded here: unrelated additions must not invalidate this record.
    observations = {name: {key: value for key, value in observation.items() if key != "plan_digest"}
                    for name, observation in observations.items()}
    return _digest({"record": record, "sources": sources,
                    "fields": {name: plan["fields"][name] for name in record["required_fields"]},
                    "limits": plan["limits"], "observations": observations,
                    "readiness_policy": READINESS_VERSION, "null_policy": "missing-never-candidate"})


def refresh_evidence(previous, current, Q, *, previous_Q=None, evidence_api):
    """Compare a freshly re-enumerated artifact with an immutable prior artifact.

    Caller MUST prepare current from its current deterministic inventory; this
    function cannot discover new source records through model output. Only equal
    records, all consulted dependencies, instructions, readiness, rubric and
    rendered state are reusable. The result is an eligibility receipt, not a
    transferred reviewer approval or invented judgment. Old artifacts untouched.
    """
    before, after = _plan(previous), _plan(current)
    prior_check = evidence_api.validate_evidence(previous)
    if not prior_check["integrity_ok"]:
        raise ValueError("previous evidence immutable identity or snapshots are corrupt")
    check = evidence_api.validate_evidence(current)
    if not check["ok"]:
        raise ValueError("current evidence must pass dependency validation before refresh")
    if previous_Q is None:
        previous_Q = Q
    old_ids, new_ids = set(before["records"]), set(after["records"])
    reused, changed = [], {}
    for identifier in sorted(old_ids & new_ids):
        reasons = []
        if _rubric_digest(previous_Q) != _rubric_digest(Q):
            reasons.append("rubric_changed")
        if _record_identity(previous, identifier) != _record_identity(current, identifier):
            reasons.append("record_dependency_field_or_readiness_changed")
        if before["inventories"] != after["inventories"]:
            reasons.append("declared_source_inventory_changed")
        if _render_packet(previous, identifier) != _render_packet(current, identifier):
            reasons.append("rendered_state_changed")
        if reasons:
            changed[identifier] = reasons
        else:
            reused.append(identifier)
    receipt = {"schema": "jevify-evidence-refresh/1", "previous": previous.get("digest", before["plan_digest"]),
               "current": current.get("digest", after["plan_digest"]), "qhash": _rubric_digest(Q),
               "previous_qhash": _rubric_digest(previous_Q), "reusable": reused, "changed": changed,
               "added": sorted(new_ids - old_ids), "removed": sorted(old_ids - new_ids),
               "inventory_reenumerated": True, "reviewer_confirmations_reused": False,
               "grants_authority": False}
    receipt["digest"] = _digest(receipt)
    return receipt
