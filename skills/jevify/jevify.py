"""Deterministic extraction and review helpers for rubric-first Jev batches.

Load into the persistent Python eval kernel with %load skill://jevify/jevify.py.
The kernel supplies judge_batch, completion, and wait when their methods are called.
"""

import base64 as _jv_base64
import collections as _jv_collections
import concurrent.futures as _jv_futures
import copy as _jv_copy
import hashlib as _jv_hashlib
import importlib as _jv_importlib
import json as _jv_json
import math as _jv_math
import os as _jv_os
import pathlib as _jv_pathlib
import random as _jv_random
import re as _jv_re
import shutil as _jv_shutil
import socket as _jv_socket
import subprocess as _jv_subprocess
import sys as _jv_sys
import tempfile as _jv_tempfile
import time as _jv_time
import types as _jv_types


class _JvRows(list):
    """Rows with the receipt from their own run, including runs with no returned rows."""

    def __init__(self, rows=(), *, receipt=None):
        super().__init__(rows)
        self.receipt = receipt


class _JvJournalError(RuntimeError):
    """A durable bundle write failed; the run stops rather than accept an item it could not record."""


class _JvReviewRefused(ValueError):
    """A review operation refused its input. `problems` lists every refusal; nothing was recorded."""

    def __init__(self, problems):
        self.problems = problems
        super().__init__("; ".join(f"{problem['reason']} ({problem['item']}): {problem['detail']}"
                                   for problem in problems))


class _JvNoJournal:
    """Stock runs keep no durable record, so their call sites stay unconditional."""

    @staticmethod
    def append(event):
        return None


class _JvBundle:
    """One run's durable directory: an atomically published plan and an fsynced, append-only journal.

    Ownership is an advisory flock held for the owner's lifetime, so a dead owner releases it. The owner
    record is diagnostic; liveness is decided by the lock alone. Durable bundles require POSIX flock.
    """

    SCHEMA = "jevify-bundle/1"
    LOCK, PLAN, JOURNAL, ROWS, RECEIPT = "lock", "plan.json", "events.jsonl", "rows.jsonl", "receipt.json"

    def __init__(self, path):
        self.path = _jv_pathlib.Path(path)
        self._fd = None

    @staticmethod
    def _flock():
        try:
            import fcntl
        except ImportError as error:
            raise RuntimeError("durable jevify bundles need POSIX flock") from error
        return fcntl

    @staticmethod
    def host():
        """Host identity recorded with each host batch. The boot ID bounds one host session."""
        boot = None
        try:
            with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as source:
                boot = source.read().strip() or None
        except OSError:
            boot = None
        return {"hostname": _jv_socket.gethostname(), "boot_id": boot, "pid": _jv_os.getpid()}

    def has_plan(self):
        return (self.path / self.PLAN).exists()

    def acquire(self, *, wait=0.0):
        """Take the exclusive lock, polling for up to `wait` seconds before refusing a live owner."""
        fcntl = self._flock()
        self.path.mkdir(parents=True, exist_ok=True)
        fd = _jv_os.open(self.path / self.LOCK, _jv_os.O_RDWR | _jv_os.O_CREAT, 0o600)
        deadline = _jv_time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if not isinstance(error, BlockingIOError):
                    _jv_os.close(fd)
                    raise
                if _jv_time.monotonic() >= deadline:
                    _jv_os.close(fd)
                    raise RuntimeError(f"bundle {self.path} is owned by a live run; wait for it to exit "
                                       "or resume after it has exited") from None
                _jv_time.sleep(0.02)
        record = _jv_json.dumps({**self.host(), "acquired": round(_jv_time.time(), 3)}, sort_keys=True)
        _jv_os.ftruncate(fd, 0)
        _jv_os.write(fd, record.encode("utf-8"))
        _jv_os.fsync(fd)
        self._fd = fd

    def release(self):
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl = self._flock()
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            _jv_os.close(fd)

    def publish(self, name, write):
        """Write through write(path), fsync that file, atomically rename it over name, fsync the directory."""
        temporary = self.path / f".{name}.tmp"
        write(str(temporary))
        with open(temporary, "rb+") as handle:
            _jv_os.fsync(handle.fileno())
        _jv_os.replace(temporary, self.path / name)
        try:
            directory = _jv_os.open(self.path, _jv_os.O_RDONLY)
        except OSError:
            return
        try:
            _jv_os.fsync(directory)
        finally:
            _jv_os.close(directory)

    def write_plan(self, body):
        text = _jv_json.dumps(body, ensure_ascii=False, indent=2) + "\n"

        def write(path):
            with open(path, "w", encoding="utf-8") as output:
                output.write(text)

        self.publish(self.PLAN, write)

    def read_plan(self):
        try:
            with open(self.path / self.PLAN, encoding="utf-8") as source:
                return _jv_json.load(source)
        except FileNotFoundError:
            raise ValueError(f"bundle {self.path} has no plan; nothing was submitted, start it with run()") from None

    def append(self, event):
        """Durably record one event; returns only after fsync. Any failure raises _JvJournalError."""
        record = {**event, "at": round(_jv_time.time(), 3)}
        try:
            line = _jv_json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            data = (line + "\n").encode("utf-8", "surrogatepass")
        except (TypeError, ValueError) as error:
            raise _JvJournalError(f"journal event is not serializable: {error}") from error
        try:
            with open(self.path / self.JOURNAL, "ab") as output:
                output.write(data)
                output.flush()
                _jv_os.fsync(output.fileno())
        except OSError as error:
            raise _JvJournalError(f"journal write failed: {error}") from error

    def replay(self):
        """Complete journal records in order. A trailing fragment was never durable, so it is truncated;
        damage inside complete records is an error, never guessed around."""
        target = self.path / self.JOURNAL
        if not target.exists():
            return []
        data = target.read_bytes()
        end = data.rfind(b"\n") + 1
        if end < len(data):
            with open(target, "r+b") as handle:
                handle.truncate(end)
                handle.flush()
                _jv_os.fsync(handle.fileno())
        events = []
        for number, raw in enumerate(data[:end].split(b"\n")[:-1], start=1):
            try:
                event = _jv_json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as error:
                raise ValueError(f"journal record {number} in {target} is corrupt") from error
            if not isinstance(event, dict) or not isinstance(event.get("event"), str):
                raise ValueError(f"journal record {number} in {target} is not an event")
            events.append(event)
        return events


class jv:
    CAP = 12_000
    MAX_JEV = 32_000  # Jev tokens; 32.6k-token states judged, 33.1k failed max_tokens_exceeded.
    SEED = 11
    MAX_LABELS = 255  # Measured on the jev-1.13 route: 255 choice labels judged, more are refused.
    EDGES = (0.0, 0.1, 0.3, 0.5, 0.7, 0.9, 1.01)
    stats = {"states": {}, "runs": []}
    frozen = None

    @classmethod
    def choice(cls, instructions, criteria=None, **labels):
        options = dict(criteria or {})
        for label, description in labels.items():
            if label in options:
                raise ValueError(f"duplicate choice label: {label}")
            options[label] = description
        if len(options) < 2:
            raise ValueError("a choice requires at least two labels")
        if len(options) > cls.MAX_LABELS:
            raise ValueError(f"a choice allows at most {cls.MAX_LABELS} labels (measured jev-1.13 route "
                             f"limit); got {len(options)}: split the question or pre-filter the labels")
        return {"type": "choice", "instructions": instructions, "criteria": options}

    @staticmethod
    def yes(instructions, *, true=None, false=None):
        question = {"type": "bool", "instructions": instructions}
        criteria = {}
        if true is not None:
            criteria["true"] = true
        if false is not None:
            criteria["false"] = false
        if criteria:
            question["criteria"] = criteria
        return question

    @staticmethod
    def scale(instructions, *levels):
        if len(levels) < 2:
            raise ValueError("a scale requires at least two levels")
        return {"type": "score", "instructions": instructions, "criteria": list(levels)}

    @classmethod
    def qhash(cls, Q, *, ordered=True):
        """Ordered identity keeps question and choice order. ordered=False is the order-insensitive
        base rubric identity, used only to compare variants of one rubric."""
        if ordered:
            text = _jv_json.dumps(cls._ordered_identity(Q), ensure_ascii=False, separators=(",", ":"))
        else:
            text = _jv_json.dumps(Q, ensure_ascii=False, sort_keys=True)
        return _jv_hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]

    @classmethod
    def freeze(cls, Q, path="/tmp/jevify-questions.json"):
        digest = cls.qhash(Q)
        with open(path, "w", encoding="utf-8") as output:
            # Insertion order is preserved on disk, so a reloaded file hashes to the same identity.
            _jv_json.dump(Q, output, indent=2, ensure_ascii=False)
            output.write("\n")
        cls.frozen = digest
        return digest

    PLAN_SCHEMA = "jevify-plan/1"
    EXTRACTION_SCHEMA = "jevify-extraction/1"
    REVIEW_SCHEMA = "jevify-review/2"
    REVIEW_LOCK_WAIT = 30.0
    REVIEW_ITEM_FIELDS = frozenset({"unit", "claim", "question"})
    REVIEW_DECISION_REQUIRED = frozenset({"item", "fingerprint", "view", "verdict"})
    REVIEW_DECISION_FIELDS = REVIEW_DECISION_REQUIRED | {"evidence", "note"}
    REVIEW_OPTIONS = (("confirmed", "The cited excerpt supports the claim."),
                      ("overturned", "The cited excerpt contradicts the claim."),
                      ("unresolved", "The cited excerpt cannot decide the claim."))
    ReviewRefused = _JvReviewRefused
    VERDICTS = ("confirmed", "overturned", "unresolved")
    MAX_EVIDENCE_LINES = 400
    _RANK = {"ok": 0, "unmeasured": 1, "over": 2}

    @staticmethod
    def _dumps(value):
        # Sorted JSON with default separators is never more compact than the digest form, so byte
        # counts taken from it stay conservative for the request budget.
        return _jv_json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    @staticmethod
    def _digest(value):
        canonical = _jv_json.dumps(value, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"), default=str)
        return _jv_hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()

    @staticmethod
    def _utf8(text):
        return len(text.encode("utf-8", "surrogatepass"))

    @staticmethod
    def _limits(total_budget, single_budget, headroom, framing):
        for name, value in (("total_budget", total_budget), ("single_budget", single_budget),
                            ("framing", framing)):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if not isinstance(headroom, (int, float)) or not 0 <= headroom < 1:
            raise ValueError("headroom must be a fraction in [0, 1)")
        limits = {"total": int(total_budget * (1 - headroom)) - framing,
                  "single": int(single_budget * (1 - headroom)) - framing}
        if min(limits.values()) <= 0:
            raise ValueError("headroom and framing leave no request budget")
        return limits

    @staticmethod
    def _measure(text, sizer, sized):
        if text not in sized:
            tokens = None if sizer is None else sizer(text)
            if tokens is not None and (type(tokens) is not int or tokens < 0):
                raise ValueError("sizer must return a nonnegative int or None")
            sized[text] = tokens
        return sized[text]

    @classmethod
    def _assess(cls, texts, limit, sizer, sized):
        """Classify one request part against a limit without guessing.

        Jev tokens never exceed UTF-8 bytes, so a byte count within the limit proves ok without
        sizing. Otherwise measured pieces give a proven lower bound and unmeasured pieces count at
        their byte bound: over needs the lower bound beyond the limit, ok needs the upper bound within."""
        upper = sum(cls._utf8(text) for text in texts)
        if upper <= limit:
            return {"status": "ok", "limit": limit, "upper": upper, "lower": 0, "exact": False}
        lower = bound = 0
        exact = True
        for text in texts:
            tokens = cls._measure(text, sizer, sized)
            if tokens is None:
                bound += cls._utf8(text)
                exact = False
            else:
                lower += tokens
                bound += tokens
        if lower > limit:
            status = "over"
        elif bound <= limit:
            status = "ok"
        else:
            status = "unmeasured"
        return {"status": status, "limit": limit, "upper": bound, "lower": lower, "exact": exact}

    @classmethod
    def _request(cls, state, question_texts, limits, sizer, sized):
        """Assess one state with all questions (total) and with each question alone (single)."""
        total = cls._assess([state, *question_texts.values()], limits["total"], sizer, sized)
        singles = {qid: cls._assess([state, text], limits["single"], sizer, sized)
                   for qid, text in question_texts.items()}
        worst = max(singles, key=lambda qid: (cls._RANK[singles[qid]["status"]], singles[qid]["upper"]))
        status = max(total["status"], singles[worst]["status"], key=cls._RANK.__getitem__)
        return {"status": status, "total": total, "single": {"worst": worst, **singles[worst]}}

    @classmethod
    def plan(cls, states, Q, *, grouping=None, total_budget=64_000, single_budget=32_000,
             headroom=0.10, framing=512, sizer=None):
        """Assess every request run() would submit against the documented request budgets.

        Each request carries its state with the questions run() sends alongside it: all of Q for flat
        runs, or the slot-expanded questions (slot_questions at the largest slot count) for grouped runs.
        Two checks apply: state plus all questions within total_budget, and state plus any one question
        within single_budget. Both limits lose `headroom` (a fraction) and `framing` tokens.
        Sizing is local and never calls a judge: a byte count within a limit is ok without measuring;
        otherwise `sizer` (default jev_tokens; any callable returning an int or None) decides.
        Requests that cannot be measured are reported as unmeasured rather than guessed.

        The 32k single_budget default stays below the measured jev-1.13 input limit of about 32.7k
        tokens (route-specific). `labels` counts the labels of every choice question; a choice over
        MAX_LABELS (255, the measured route limit) is listed in `over_labels` and makes the plan
        inadmissible. `pack_suggestion` is None, or a note offered when many tiny flat states leave
        each request nearly empty; grouping is a separate instrument that needs its own pilot, and
        plan never packs for you."""
        if not isinstance(Q, dict) or not Q:
            raise ValueError("plan requires a nonempty question mapping")
        limits = cls._limits(total_budget, single_budget, headroom, framing)
        if grouping is None:
            strategy, width, questions, excluded = "flat", None, Q, {}
        else:
            problems = cls._grouping_problems(states, grouping)
            if problems:
                raise ValueError("invalid grouping: " + "; ".join(problems))
            strategy, excluded = "grouped", dict(grouping["excluded"])
            width = max((len(ids) for ids in grouping["slots"].values()), default=0)
            questions = cls.slot_questions(Q, width) if width else {}
        question_texts = {qid: cls._dumps(question) for qid, question in questions.items()}
        measure = cls.jev_tokens if sizer is None else sizer
        sized, requests = {}, {}
        for identifier, value in states.items():
            state = cls._dumps(value)
            requests[identifier] = cls._request(state, question_texts, limits, measure, sized)
            requests[identifier]["state_bytes"] = cls._utf8(state)
        over = [sid for sid, record in requests.items() if record["status"] == "over"]
        unmeasured = [sid for sid, record in requests.items() if record["status"] == "unmeasured"]
        labels = {qid: len(question["criteria"]) for qid, question in Q.items()
                  if isinstance(question, dict) and question.get("type") == "choice"
                  and isinstance(question.get("criteria"), dict)}
        over_labels = [qid for qid, count in labels.items() if count > cls.MAX_LABELS]
        suggestion = None
        if strategy == "flat" and len(requests) >= 20:
            sizes = sorted(record["single"]["upper"] for record in requests.values())
            middle = len(sizes) // 2
            median = sizes[middle] if len(sizes) % 2 else (sizes[middle - 1] + sizes[middle]) / 2
            if median < 0.10 * single_budget:
                suggestion = (f"{len(requests)} flat states; the median request uses {median:.0f} of "
                              f"{single_budget} single_budget tokens (<10%). Grouping several units per "
                              "request may cut request count, but it is a separate instrument that needs "
                              "its own pilot; plan() never packs for you.")
        result = {"schema": cls.PLAN_SCHEMA, "strategy": strategy, "qhash": cls.qhash(Q),
                  "questions": {"base": len(Q), "expanded": len(questions), "slots": width},
                  "budgets": {"total": total_budget, "single": single_budget, "headroom": headroom,
                              "framing": framing, "limits": limits,
                              "sizer": "jev_tokens" if sizer is None else "custom"},
                  "requests": requests, "over_budget": over, "unmeasured": unmeasured,
                  "excluded": excluded, "admissible": not over and not unmeasured and not over_labels,
                  "inference_calls": 0, "labels": labels, "over_labels": over_labels,
                  "pack_suggestion": suggestion}
        if over_labels:
            print(f"plan: choice questions over {cls.MAX_LABELS} labels (measured route limit): "
                  + ", ".join(f"{qid}={labels[qid]}" for qid in over_labels))
        print(f"plan: {len(requests)} requests · {len(over)} over budget · {len(unmeasured)} unmeasured "
              f"· admissible={result['admissible']}")
        return result

    @staticmethod
    def _grouping_problems(states, grouping):
        if not isinstance(grouping, dict) or set(grouping) != {"original_ids", "slots", "excluded"}:
            return ["grouping requires original_ids, slots, and excluded"]
        if not isinstance(states, dict):
            return ["states must be a mapping"]
        originals, slots, excluded = grouping["original_ids"], grouping["slots"], grouping["excluded"]
        problems = []
        originals_ok = (isinstance(originals, list) and all(isinstance(uid, str) for uid in originals)
                        and len(set(originals)) == len(originals))
        if not originals_ok:
            problems.append("original_ids must be unique string IDs")
        if not isinstance(slots, dict) or set(slots) != set(states):
            problems.append("slots must name every state exactly once")
            slots = slots if isinstance(slots, dict) else {}
        placed = []
        for gid, ids in slots.items():
            if not isinstance(ids, list) or not ids or any(not isinstance(uid, str) for uid in ids):
                problems.append(f"slot {gid} must list string unit IDs")
                continue
            placed.extend(ids)
            state = states.get(gid)
            units = state.get("units") if isinstance(state, dict) else None
            if not isinstance(units, dict) or set(units) != {f"s{i}" for i in range(len(ids))}:
                problems.append(f"slot {gid} does not match its state units")
        if len(set(placed)) != len(placed):
            problems.append("each unit must occupy exactly one slot")
        if (not isinstance(excluded, dict)
                or any(not isinstance(uid, str) or not isinstance(reason, str) or not reason.strip()
                       for uid, reason in excluded.items())):
            problems.append("excluded must map unit IDs to nonempty reasons")
        elif originals_ok and ((set(placed) & set(excluded))
                               or ((set(placed) | set(excluded)) != set(originals))):
            problems.append("grouping must account for every original ID exactly once")
        return problems

    @classmethod
    def pack(cls, units, render, Q, *, by=None, k=8, shared=None, total_budget=64_000,
             single_budget=32_000, headroom=0.10, framing=512, sizer=None):
        """Pack units into grouped states that fit plan()'s request budgets without truncating.

        Units are bucketed by by(unit) (one bucket when by is None) and packed in order into groups of
        at most k slots; a group closes before adding a unit would exceed a budget or the slot count.
        A unit whose one-unit request is over budget or unmeasured is excluded with that reason, and
        empty rendered units are excluded as in group(). Returns (states, grouping) in group()'s
        manifest format, so run(states, Q, grouping=grouping) accepts it unchanged. Every original
        appears exactly once, in a slot or in grouping["excluded"]. Group requests are assessed with
        k expanded slots, an upper bound on what run() sends for any smaller slot count."""
        if type(k) is not int or k < 1:
            raise ValueError("k must be a positive integer")
        if not isinstance(Q, dict) or not Q:
            raise ValueError("pack requires a nonempty question mapping")
        limits = cls._limits(total_budget, single_budget, headroom, framing)
        measure = cls.jev_tokens if sizer is None else sizer
        sized = {}
        question_texts = {qid: cls._dumps(question)
                          for qid, question in cls.slot_questions(Q, k).items()}
        excluded, buckets = {}, {}
        for identifier, unit in units.items():
            value = render(unit) if render else unit
            if cls._nonempty(value):
                buckets.setdefault("all" if by is None else by(unit), []).append((identifier, value))
            else:
                excluded[identifier] = "empty rendered unit"

        def status(key, members):
            state = {"group": key, "units": {f"s{i}": value for i, (_, value) in enumerate(members)}}
            if shared is not None:
                state["shared"] = shared(key)
            return cls._request(cls._dumps(state), question_texts, limits, measure, sized)["status"]

        groups, slots = {}, {}
        for key, members in buckets.items():
            chunks, current = [], []
            for member in members:
                alone = status(key, [member])
                if alone != "ok":
                    excluded[member[0]] = {
                        "over": "one-unit request exceeds the request budget",
                        "unmeasured": "one-unit request size is unmeasured"}[alone]
                    continue
                if current and (len(current) == k or status(key, current + [member]) != "ok"):
                    chunks.append(current)
                    current = []
                current.append(member)
            if current:
                chunks.append(current)
            for index, chunk in enumerate(chunks, start=1):
                identifier = f"{key}#{index}"
                while identifier in groups:
                    identifier += "#2"
                state = {"group": key, "units": {f"s{i}": value for i, (_, value) in enumerate(chunk)}}
                if shared is not None:
                    state["shared"] = shared(key)
                groups[identifier] = state
                slots[identifier] = [uid for uid, _ in chunk]
        return groups, {"original_ids": list(units), "slots": slots, "excluded": excluded}

    @staticmethod
    def _source_path(root_path, rel):
        if not isinstance(rel, str) or not rel or _jv_pathlib.Path(rel).is_absolute():
            raise ValueError(f"unsafe source path: {rel!r}")
        path = root_path / rel
        if not path.resolve().is_relative_to(root_path) or path.is_symlink():
            raise ValueError(f"source path escapes root or is a symlink: {rel}")
        return path

    @classmethod
    def _file_sha256(cls, root_path, rel):
        path = cls._source_path(root_path, rel)
        if not path.is_file():
            return None
        return _jv_hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _hit(hit):
        rel, line = (hit["file"], hit["line"]) if isinstance(hit, dict) else hit
        if not isinstance(rel, str) or type(line) is not int or line < 1:
            raise ValueError(f"invalid source hit: {hit!r}")
        return rel, line

    @classmethod
    def extraction(cls, units, states, *, grouping=None, outcomes=None, root=".", revision=None,
                   hits=None):
        """Bind extracted units, the states run() will receive, and their outcomes to source bytes.

        units: ID -> unit dict with a root-relative "file" and optional "line"/"end_line" (1-based).
        states: flat states, or group states when grouping is the group()/pack() manifest.
        outcomes: the report= dict filled by states() or group(); supplies drop and truncation reasons.
        Hashes: each source file under root (None when missing), each unit, and each state.
        hits: optional independent source sites [{"file", "line"}]; each must fall inside some unit's
        line..end_line span (a unit without end_line covers its line only)."""
        if not isinstance(units, dict) or not isinstance(states, dict):
            raise ValueError("extraction requires unit and state mappings")
        outcomes = {} if outcomes is None else outcomes
        root_path = _jv_pathlib.Path(root).resolve()
        if grouping is None:
            placed = {uid: (uid, None) for uid in states}
            excluded = {}
        else:
            problems = cls._grouping_problems(states, grouping)
            if problems:
                raise ValueError("invalid grouping: " + "; ".join(problems))
            if set(grouping["original_ids"]) != set(units):
                raise ValueError("grouping original_ids must match the extracted units")
            placed = {uid: (gid, index) for gid, ids in grouping["slots"].items()
                      for index, uid in enumerate(ids)}
            excluded = grouping["excluded"]
        stray = sorted(uid for uid in placed if uid not in units)
        if stray:
            raise ValueError(f"submitted IDs are not extracted units: {stray[:10]}")
        hit_rows = [cls._hit(hit) for hit in (hits or ())]
        files, entries = {rel for rel, _ in hit_rows}, {}
        for uid, unit in units.items():
            if not isinstance(unit, dict) or not isinstance(unit.get("file"), str):
                raise ValueError(f"unit {uid} needs a root-relative file")
            files.add(unit["file"])
            outcome = outcomes.get(uid, {})
            state = slot = None
            if uid in placed:
                state, slot = placed[uid]
                status, reason = "submitted", outcome.get("reason")
            elif uid in excluded:
                status, reason = "excluded", excluded[uid]
            else:
                status, reason = "dropped", outcome.get("reason") or "not submitted"
            entries[uid] = {"file": unit["file"], "line": unit.get("line"),
                            "end_line": unit.get("end_line"), "content_sha256": cls._digest(unit),
                            "status": status, "reason": reason, "state": state, "slot": slot,
                            "truncated": outcome.get("truncated")}
        source_files = {rel: {"sha256": cls._file_sha256(root_path, rel)} for rel in sorted(files)}
        spans = _jv_collections.defaultdict(list)
        for entry in entries.values():
            if type(entry["line"]) is int:
                end = entry["end_line"] if type(entry["end_line"]) is int else entry["line"]
                spans[entry["file"]].append((entry["line"], end))
        uncovered = [{"file": rel, "line": line} for rel, line in hit_rows
                     if not any(start <= line <= end for start, end in spans.get(rel, ()))]
        tally = _jv_collections.Counter(entry["status"] for entry in entries.values())
        truncated = sum(bool(entry["truncated"]) for entry in entries.values())
        unknown = sum(entry["truncated"] is None for entry in entries.values())
        coverage = {"originals": len(units), "submitted": tally["submitted"],
                    "dropped": tally["dropped"], "excluded": tally["excluded"],
                    "truncated": truncated, "truncation_unknown": unknown,
                    "hits": len(hit_rows), "uncovered_hits": len(uncovered),
                    "complete": (tally["submitted"] == len(units) and not truncated and not unknown
                                 and not uncovered)}
        body = {"schema": cls.EXTRACTION_SCHEMA,
                "source": {"root": str(root), "revision": revision, "files": source_files},
                "strategy": "flat" if grouping is None else "grouped",
                "original_ids": list(units), "units": entries,
                "states": {sid: cls._digest(value) for sid, value in states.items()},
                "grouping": None if grouping is None else _jv_copy.deepcopy(grouping),
                "hits": {"checked": len(hit_rows), "uncovered": uncovered},
                "coverage": coverage}
        body["digest"] = cls._digest(body)
        return body

    @classmethod
    def validate_extraction(cls, manifest, states=None, *, grouping=None, root=None):
        """Recheck a manifest against current source bytes and, when given, the states and grouping
        run() will receive. Reports explicit problems and never repairs or re-extracts."""
        if not isinstance(manifest, dict) or manifest.get("schema") != cls.EXTRACTION_SCHEMA:
            raise ValueError("not a jevify extraction manifest")
        problems = []
        digest = cls._digest({key: value for key, value in manifest.items() if key != "digest"})
        if manifest.get("digest") != digest:
            problems.append("manifest digest does not match its contents")
        source = manifest["source"]
        root_path = _jv_pathlib.Path(source["root"] if root is None else root).resolve()
        changed, missing = [], []
        for rel, record in source["files"].items():
            try:
                current = cls._file_sha256(root_path, rel)
            except ValueError as error:
                problems.append(str(error))
                continue
            if current == record["sha256"]:
                continue
            if current is None:
                missing.append(rel)
                problems.append(f"source missing: {rel}")
            else:
                changed.append(rel)
                problems.append(f"source changed: {rel}")
        if states is not None or grouping is not None:
            if ((grouping is None) != (manifest["grouping"] is None)
                    or (grouping is not None
                        and cls._digest(grouping) != cls._digest(manifest["grouping"]))):
                problems.append("grouping does not match the manifest")
        if states is not None:
            recorded = manifest["states"]
            if set(states) != set(recorded):
                problems.append("submitted state IDs do not match the manifest")
            problems.extend(f"state changed since extraction: {sid}" for sid, value in states.items()
                            if sid in recorded and cls._digest(value) != recorded[sid])
        coverage = manifest["coverage"]
        return {"ok": not problems, "problems": problems, "digest": digest, "changed": changed,
                "missing": missing, "complete": coverage["complete"], "coverage": coverage,
                "identity": {"digest": digest, "root": source["root"], "revision": source["revision"],
                             "files": {rel: record["sha256"] for rel, record in source["files"].items()}}}

    @staticmethod
    def _review_show(value):
        return "unknown" if value is None else _jv_json.dumps(value, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _review_reviewer(reviewer):
        if not isinstance(reviewer, dict) or set(reviewer) != {"kind", "id"}:
            raise ValueError("reviewer needs exactly kind and id")
        if reviewer["kind"] not in ("human", "agent"):
            raise ValueError("reviewer kind must be human or agent")
        if not isinstance(reviewer["id"], str) or not reviewer["id"].strip():
            raise ValueError("reviewer id must be a nonempty string")
        return {"kind": reviewer["kind"], "id": reviewer["id"]}

    @classmethod
    def _open_review(cls, path, *, create=False):
        """Lock one review directory. Reads hold the lock too: replay repairs a torn tail in place."""
        if isinstance(path, dict):
            raise ValueError("legacy in-memory review dicts are refused; pass the durable review path")
        target = _jv_pathlib.Path(path)
        if not create and not target.is_dir():
            raise ValueError(f"no jevify review at {target}")
        bundle = _JvBundle(target)
        bundle.acquire(wait=cls.REVIEW_LOCK_WAIT)
        if not create and not bundle.has_plan():
            bundle.release()
            raise ValueError(f"no jevify review at {target}")
        return bundle

    @classmethod
    def _read_review_plan(cls, bundle):
        try:
            return cls._verify_review_plan(bundle.read_plan())
        except ValueError as error:
            raise ValueError(f"review at {bundle.path} is corrupt or not a jevify review: {error}") from error

    @classmethod
    def _verify_review_plan(cls, plan):
        """The policy body is checked as drift, not as corruption; every other frozen field must match the digest."""
        if not isinstance(plan, dict) or plan.get("schema") != cls.REVIEW_SCHEMA:
            raise ValueError("not a jevify review plan")
        identity = {key: value for key, value in plan.items() if key not in ("root", "digest", "policy")}
        if plan.get("digest") != cls._digest(identity):
            raise ValueError("review plan digest does not match its contents")
        if (not isinstance(plan.get("files"), dict) or not isinstance(plan.get("items"), dict)
                or not isinstance(plan.get("root"), str) or "policy" not in plan):
            raise ValueError("review plan is missing a frozen section")
        return plan

    @classmethod
    def _review_state(cls, plan, events):
        """Fold the decision journal into per-item verdicts, history, and views. The journal is the only source."""
        items = {item_id: {"verdict": "pending", "decisions": 0, "head": None, "history": []}
                 for item_id in plan["items"]}
        batches = 0
        for event in events:
            if event["event"] != "decisions":
                raise ValueError(f"unknown review journal event {event['event']!r}")
            batches += 1
            decisions = event.get("decisions")
            if event.get("batch") != batches or not isinstance(decisions, list) or not decisions:
                raise ValueError(f"review journal batch {batches} is out of order or empty")
            for record in decisions:
                if (not isinstance(record, dict) or not isinstance(record.get("item"), str)
                        or record["item"] not in items or record.get("verdict") not in cls.VERDICTS):
                    raise ValueError(f"review journal batch {batches} holds an invalid decision")
                entry = items[record["item"]]
                entry["history"].append({"batch": batches, "at": event.get("at"),
                                         "reviewer": event.get("reviewer"), "identity": event.get("identity"),
                                         "verdict": record["verdict"], "evidence": record.get("evidence", []),
                                         "note": record.get("note")})
                entry["verdict"] = record["verdict"]
                entry["decisions"] += 1
                entry["head"] = cls._digest(record)
        for item_id, entry in items.items():
            entry["view"] = cls._digest({"fingerprint": plan["items"][item_id]["fingerprint"],
                                         "decisions": entry["decisions"], "head": entry["head"]})
        return {"items": items, "batches": batches}

    @classmethod
    def _review_drift(cls, plan, state, Q, root_path):
        """Compare frozen identity with the current rubric, policy, and source bytes. Drift is reported, never repaired.

        An item is stale_source when its excerpt's file changed, and stale_evidence when its latest decision cites
        a changed file: that decision no longer counts, but the unchanged excerpt can be decided again."""
        rubric = cls.qhash(Q) != plan["qhash"]
        policy = not (isinstance(plan["policy"], dict) and cls._digest(plan["policy"]) == plan["policy_digest"]
                      and plan["policy"].get("qhash") == plan["qhash"])
        changed = []
        for rel, sha in sorted(plan["files"].items()):
            try:
                current = cls._file_sha256(root_path, rel)
            except ValueError:
                current = None
            if current != sha:
                changed.append(rel)
        items = {}
        for item_id, entry in plan["items"].items():
            history = state["items"][item_id]["history"]
            if rubric:
                items[item_id] = "stale_rubric"
            elif policy:
                items[item_id] = "stale_policy"
            elif entry["excerpt"]["file"] in changed:
                items[item_id] = "stale_source"
            elif history and any(isinstance(record, dict) and record.get("file") in changed
                                 for record in history[-1]["evidence"]):
                items[item_id] = "stale_evidence"
            else:
                items[item_id] = None
        return {"rubric": rubric, "policy": policy, "changed": changed, "items": items}

    @classmethod
    def _review_view(cls, bundle, Q, root):
        """Verified plan, its root, folded state, and drift for one locked review."""
        plan = cls._read_review_plan(bundle)
        root_path = _jv_pathlib.Path(plan["root"] if root is None else root).resolve()
        state = cls._review_state(plan, bundle.replay())
        return plan, root_path, state, cls._review_drift(plan, state, Q, root_path)

    @classmethod
    def _review_summary(cls, plan, state, drift):
        verdicts = {item_id: entry["verdict"] for item_id, entry in state["items"].items()}
        stale = {item_id: reason for item_id, reason in drift["items"].items() if reason}
        by_unit = _jv_collections.defaultdict(list)
        for item_id, entry in plan["items"].items():
            by_unit[entry["unit"]].append(item_id)

        def listed(verdict):
            return sorted(item_id for item_id, value in verdicts.items() if value == verdict)

        pending, unresolved = listed("pending"), listed("unresolved")
        drifted = drift["rubric"] or drift["policy"] or bool(drift["changed"])
        return {"schema": cls.REVIEW_SCHEMA, "items": len(verdicts), "batches": state["batches"],
                "pending": pending, "unresolved": unresolved, "confirmed": listed("confirmed"),
                "overturned": listed("overturned"), "stale": stale,
                "drift": {"rubric": drift["rubric"], "policy": drift["policy"], "source": drift["changed"]},
                "complete": not pending and not unresolved,
                "approved_units": sorted(unit for unit, ids in by_unit.items()
                                         if all(verdicts[item_id] == "confirmed" and not stale.get(item_id)
                                                for item_id in ids)),
                "settled": not pending and not unresolved and not stale and not drifted,
                "grants_authority": False,
                "manifest_digest": plan["manifest_digest"], "policy_digest": plan["policy_digest"],
                "qhash": plan["qhash"]}

    @classmethod
    def _review_rows(cls, rows, manifest, Q):
        """Index one run's rows by unit ID, refusing any run not bound to this rubric and this extraction."""
        if not isinstance(rows, _JvRows) or not isinstance(rows.receipt, dict):
            raise ValueError("review rows must be a run's rows with their receipt")
        if rows.receipt.get("qhash") != cls.qhash(Q):
            raise ValueError("review rows were judged under a different question hash")
        extraction = rows.receipt.get("extraction")
        if not isinstance(extraction, dict) or extraction.get("digest") != manifest["digest"]:
            raise ValueError("review rows were not judged from this extraction manifest")
        indexed = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or row["id"] in indexed:
                raise ValueError("review rows need unique string IDs")
            indexed[row["id"]] = row
        return indexed

    @classmethod
    def _review_model(cls, indexed, unit, question, Q):
        """Verdict, uncertainty, and provenance from one bound run row. No question means unknown."""
        if question is None:
            return None, None, None
        if indexed is None:
            raise ValueError(f"unit {unit!r} names question {question!r} but no run rows are bound")
        if not isinstance(question, str) or question not in Q:
            raise ValueError(f"unit {unit!r} names an unknown question: {question!r}")
        row = indexed.get(unit)
        if row is None:
            raise ValueError(f"run rows have no row for unit {unit!r}")
        if "error" in row:
            raise ValueError(f"run row for unit {unit!r} is an error; it cannot supply a verdict")
        if row.get("qhash") != cls.qhash(Q) or question not in row:
            raise ValueError(f"run row for unit {unit!r} does not answer {question!r} under this question hash")
        try:
            uncertainty = cls.uncertainty(row, question, Q[question])
        except KeyError as error:
            raise ValueError(f"run row for unit {unit!r} lacks uncertainty field {error}") from None
        provenance = {"source": "run_row", "model": row.get("model"), "row_qhash": row["qhash"],
                      "batch_id": row.get("batch_id")}
        return row[question], uncertainty, provenance

    @classmethod
    def review_queue(cls, path, manifest, items, *, Q, policy, root=None, rows=None):
        """Create the durable review at `path`, or reopen it when its identity is unchanged.

        items: item_id -> {"unit": original ID, "claim": text, optional "question": a key of Q}. An item with a
        question takes its model verdict and uncertainty from that answer in `rows`, which must be the run that
        judged this Q and this manifest. rows=None leaves every model field unknown. Caller-supplied model values
        are refused. Each excerpt is its unit's exact manifest span, frozen with its quote and fingerprint.
        policy must come from jv.policy(Q, bounds). Every item starts pending. A different review at `path` is refused."""
        if not isinstance(manifest, dict) or manifest.get("schema") != cls.EXTRACTION_SCHEMA:
            raise ValueError("review requires a jevify extraction manifest")
        cls.policy_rule(policy, Q)
        if not isinstance(items, dict) or not items:
            raise ValueError("review requires items")
        root_path = _jv_pathlib.Path(manifest["source"]["root"] if root is None else root).resolve()
        check = cls.validate_extraction(manifest, root=root_path)
        if not check["ok"]:
            raise ValueError("review requires an extraction that matches current source: "
                             + "; ".join(check["problems"]))
        snapshot = {rel: record["sha256"] for rel, record in manifest["source"]["files"].items()}
        indexed = None if rows is None else cls._review_rows(rows, manifest, Q)
        run_digest = None if rows is None else cls._digest(rows.receipt)
        frozen = {}
        for item_id, spec in items.items():
            if not isinstance(item_id, str) or not item_id.strip():
                raise ValueError("review item IDs must be nonempty strings")
            if (not isinstance(spec, dict) or not {"unit", "claim"} <= set(spec)
                    or set(spec) - cls.REVIEW_ITEM_FIELDS):
                raise ValueError(f"review item {item_id!r} needs unit and claim and may add only question")
            unit, claim, question = spec["unit"], spec["claim"], spec.get("question")
            if not isinstance(unit, str) or unit not in manifest["units"]:
                raise ValueError(f"review item {item_id!r} names an unknown unit: {unit!r}")
            if not isinstance(claim, str) or not claim.strip():
                raise ValueError(f"review item {item_id!r} needs a claim")
            model_verdict, uncertainty, model = cls._review_model(indexed, unit, question, Q)
            record = manifest["units"][unit]
            start = record.get("line")
            end = record.get("end_line") if record.get("end_line") is not None else start
            if type(start) is not int or type(end) is not int:
                raise ValueError(f"review item {item_id!r}: unit {unit!r} has no line span")
            excerpt = cls._cite(root_path, snapshot, {"file": record["file"], "start": start, "end": end})
            if len(excerpt["quote"]) > cls.CAP:
                raise ValueError(f"review item {item_id!r}: excerpt exceeds {cls.CAP} characters")
            entry = {"unit": unit, "claim": claim, "question": question, "model_verdict": model_verdict,
                     "uncertainty": uncertainty, "model": model, "excerpt": excerpt}
            entry["fingerprint"] = cls._digest({"item": item_id, **entry})
            frozen[item_id] = entry
        identity = {"schema": cls.REVIEW_SCHEMA, "manifest_digest": manifest["digest"], "files": snapshot,
                    "qhash": cls.qhash(Q), "policy_digest": cls._digest(policy), "run_digest": run_digest,
                    "items": frozen}
        plan = {**identity, "policy": _jv_copy.deepcopy(policy), "root": str(root_path),
                "digest": cls._digest(identity)}
        bundle = cls._open_review(path, create=True)
        try:
            if bundle.has_plan():
                if cls._read_review_plan(bundle)["digest"] != plan["digest"]:
                    raise ValueError(f"{bundle.path} already holds a different review; reopen it with review_items")
            else:
                bundle.write_plan(plan)
            current, _, state, drift = cls._review_view(bundle, Q, None)
            return cls._review_summary(current, state, drift)
        finally:
            bundle.release()

    @classmethod
    def review_items(cls, path, *, Q, root=None):
        """Every item with its frozen excerpt, model fields, fingerprint, current view, drift reason, and history."""
        bundle = cls._open_review(path)
        try:
            plan, _, state, drift = cls._review_view(bundle, Q, root)
            return [{"item": item_id, "unit": entry["unit"], "claim": entry["claim"],
                     "excerpt": entry["excerpt"], "model_verdict": entry["model_verdict"],
                     "uncertainty": entry["uncertainty"], "model": entry["model"],
                     "fingerprint": entry["fingerprint"], "view": state["items"][item_id]["view"],
                     "verdict": state["items"][item_id]["verdict"], "stale": drift["items"][item_id],
                     "history": state["items"][item_id]["history"]}
                    for item_id, entry in sorted(plan["items"].items())]
        finally:
            bundle.release()

    @classmethod
    def review_status(cls, path, *, Q, root=None):
        bundle = cls._open_review(path)
        try:
            plan, _, state, drift = cls._review_view(bundle, Q, root)
            return cls._review_summary(plan, state, drift)
        finally:
            bundle.release()

    @classmethod
    def _review_model_line(cls, entry):
        model = entry["model"]
        if model is None:
            return "Model verdict: unknown (no run row bound to this item)"
        return (f"Model verdict from run row (model {cls._review_show(model['model'])}, row qhash "
                f"{cls._review_show(model['row_qhash'])}): {cls._review_show(entry['model_verdict'])}")

    @classmethod
    def _review_question(cls, item_id, entry, item_state, stale):
        excerpt = entry["excerpt"]
        quote = excerpt["quote"]
        longest = max((len(run) for run in _jv_re.findall(r"`+", quote)), default=0)
        fence = "`" * max(3, longest + 1)
        return "\n".join([
            f"Claim {item_id} ({entry['unit']}): {entry['claim']}",
            f"Source {excerpt['file']}:{excerpt['start']}-{excerpt['end']} "
            f"(file sha256 {excerpt['file_sha256']}):",
            fence, quote, fence,
            cls._review_model_line(entry),
            f"Model uncertainty: {cls._review_show(entry['uncertainty'])}",
            f"Earlier decisions on this item: {len(item_state['history'])}.",
            *([f"Re-review: the latest decision ({item_state['verdict']}) cites source that changed since "
               "extraction, so it no longer counts."] if stale == "stale_evidence" else []),
            "Answer confirmed if the excerpt supports the claim, overturned if it contradicts the claim, "
            "unresolved if it cannot decide the claim.",
        ])

    @classmethod
    def review_questions(cls, path, *, Q, root=None, limit=5):
        """Up to `limit` open items as ask-compatible questions, each with a binding to echo in its decision.

        Open means pending, unresolved, or invalidated by drift. Questions are ordered: pending items, then decisions
        whose cited evidence changed (stale_evidence), then unresolved items, each by ID, so a bulk loop always
        reaches items not yet asked. Options keep one fixed order with no recommended option. Rubric or policy drift
        refuses the whole batch. stale_source items are never asked; every stale item, whatever its verdict, is listed
        under stale."""
        if type(limit) is not int or limit < 1:
            raise ValueError("review_questions limit must be a positive integer")
        bundle = cls._open_review(path)
        try:
            plan, _, state, drift = cls._review_view(bundle, Q, root)
            if drift["rubric"] or drift["policy"]:
                reason = "stale_rubric" if drift["rubric"] else "stale_policy"
                raise _JvReviewRefused([{"item": None, "reason": reason,
                                         "detail": "review identity drifted; no item is presented"}])
            def priority(item_id):
                if state["items"][item_id]["verdict"] == "pending":
                    return 0
                return 1 if drift["items"][item_id] == "stale_evidence" else 2

            open_items = sorted((item_id for item_id in plan["items"]
                                 if state["items"][item_id]["verdict"] in ("pending", "unresolved")
                                 or drift["items"][item_id]),
                                key=lambda item_id: (priority(item_id), item_id))
            questions, bindings = [], {}
            for item_id in [item_id for item_id in open_items
                            if drift["items"][item_id] in (None, "stale_evidence")][:limit]:
                entry = plan["items"][item_id]
                excerpt = entry["excerpt"]
                questions.append({
                    "id": item_id, "multi": False,
                    "question": cls._review_question(item_id, entry, state["items"][item_id],
                                                     drift["items"][item_id]),
                    "options": [{"label": label, "description": text} for label, text in cls.REVIEW_OPTIONS]})
                bindings[item_id] = {
                    "fingerprint": entry["fingerprint"], "view": state["items"][item_id]["view"],
                    "citation": {"file": excerpt["file"], "start": excerpt["start"], "end": excerpt["end"]}}
            return {"questions": questions, "bindings": bindings, "open": len(open_items),
                    "stale": {item_id: reason for item_id, reason in sorted(drift["items"].items()) if reason}}
        finally:
            bundle.release()

    @classmethod
    def adjudicate(cls, path, decisions, *, Q, reviewer, root=None):
        """Atomically append one reviewer batch. Every decision is checked against the current view first; any
        refusal raises ReviewRefused listing all problems and records nothing.

        decision: {"item", "fingerprint", "view", "verdict"} plus optional "evidence" and "note". confirmed and
        overturned cite [{"file", "start", "end"}] inside the frozen snapshot; unresolved may cite none.
        reviewer: {"kind": "human" | "agent", "id"}, recorded as self-declared, not authenticated."""
        reviewer = cls._review_reviewer(reviewer)
        if not isinstance(decisions, (list, tuple)) or not decisions:
            raise ValueError("adjudicate requires a nonempty list of decisions")
        bundle = cls._open_review(path)
        try:
            plan, root_path, state, drift = cls._review_view(bundle, Q, root)
            problems, accepted, seen = [], [], set()

            def refuse(item, reason, detail):
                problems.append({"item": item, "reason": reason, "detail": detail})

            for entry in decisions:
                item = entry.get("item") if isinstance(entry, dict) else None
                if (not isinstance(entry, dict) or not isinstance(item, str)
                        or not cls.REVIEW_DECISION_REQUIRED <= set(entry)
                        or set(entry) - cls.REVIEW_DECISION_FIELDS):
                    refuse(item, "invalid_decision", "a decision needs item, fingerprint, view, and verdict; "
                           "it may add evidence and note only")
                    continue
                if item in seen:
                    refuse(item, "duplicate_item", "a batch decides each item at most once")
                    continue
                seen.add(item)
                if item not in plan["items"]:
                    refuse(item, "unknown_item", "the item is not in this review")
                    continue
                if drift["items"][item] not in (None, "stale_evidence"):
                    refuse(item, drift["items"][item], "the frozen source, rubric, or policy changed")
                    continue
                current = state["items"][item]
                last = current["history"][-1]["reviewer"] if current["history"] else None
                if reviewer["kind"] == "agent" and isinstance(last, dict) and last.get("kind") == "human":
                    refuse(item, "protected_decision", "an agent cannot replace a human's latest decision")
                    continue
                if entry["fingerprint"] != plan["items"][item]["fingerprint"]:
                    refuse(item, "fingerprint_mismatch", "the decision does not name this item's frozen content")
                    continue
                if entry["view"] != current["view"]:
                    refuse(item, "superseded", "the item changed after this view was issued; re-read its question")
                    continue
                verdict, note, evidence = entry["verdict"], entry.get("note"), entry.get("evidence", [])
                if verdict not in cls.VERDICTS:
                    refuse(item, "invalid_decision", f"verdict must be one of: {', '.join(cls.VERDICTS)}")
                    continue
                if note is not None and not isinstance(note, str):
                    refuse(item, "invalid_decision", "note must be a string")
                    continue
                if not isinstance(evidence, list):
                    refuse(item, "invalid_evidence", "evidence must be a list")
                    continue
                if verdict != "unresolved" and not evidence:
                    refuse(item, "invalid_evidence", f"{verdict} requires cited source evidence")
                    continue
                changed = [citation.get("file") for citation in evidence
                           if isinstance(citation, dict) and citation.get("file") in drift["changed"]]
                if changed:
                    refuse(item, "stale_source", "cited file changed: " + ", ".join(map(str, changed)))
                    continue
                try:
                    records = [cls._cite(root_path, plan["files"], citation) for citation in evidence]
                except ValueError as error:
                    refuse(item, "invalid_evidence", str(error))
                    continue
                accepted.append({"item": item, "fingerprint": entry["fingerprint"], "verdict": verdict,
                                 "evidence": records, "note": note})
            if problems:
                raise _JvReviewRefused(problems)
            batch = state["batches"] + 1
            bundle.append({"event": "decisions", "batch": batch, "reviewer": reviewer,
                           "identity": "self_declared", "decisions": accepted})
            current, _, state, drift = cls._review_view(bundle, Q, root)
            return {"batch": batch,
                    "applied": [{"item": record["item"], "verdict": record["verdict"],
                                 "view": state["items"][record["item"]]["view"]} for record in accepted],
                    "review": cls._review_summary(current, state, drift)}
        finally:
            bundle.release()

    @classmethod
    def _cite(cls, root_path, snapshot, citation):
        if not isinstance(citation, dict) or set(citation) != {"file", "start", "end"}:
            raise ValueError("evidence needs exactly file, start, and end")
        rel, start, end = citation["file"], citation["start"], citation["end"]
        if not isinstance(rel, str) or rel not in snapshot or snapshot[rel] is None:
            raise ValueError(f"evidence is not in a snapshot source file: {rel!r}")
        if type(start) is not int or type(end) is not int or not 1 <= start <= end:
            raise ValueError(f"invalid 1-based evidence span for {rel}: {start}-{end}")
        if end - start + 1 > cls.MAX_EVIDENCE_LINES:
            raise ValueError(f"evidence exceeds {cls.MAX_EVIDENCE_LINES} lines: {rel}:{start}-{end}")
        if cls._file_sha256(root_path, rel) != snapshot[rel]:
            raise ValueError(f"source changed since extraction: {rel}")
        lines = cls._source_path(root_path, rel).read_bytes().decode("utf-8").splitlines()
        if end > len(lines):
            raise ValueError(f"evidence span exceeds file: {rel}:{start}-{end}")
        quote = "\n".join(lines[start - 1:end])
        return {"file": rel, "start": start, "end": end, "file_sha256": snapshot[rel],
                "text_sha256": _jv_hashlib.sha256(quote.encode("utf-8", "surrogatepass")).hexdigest(),
                "quote": quote}

    @classmethod
    def _guard_deletion(cls, units, targets, manifest, bundle, Q, root):
        check = cls.validate_extraction(manifest, root=root)
        if not check["ok"]:
            raise ValueError("refusing delete_spans: source no longer matches the extraction snapshot: "
                             + "; ".join(check["problems"]))
        for uid in targets:
            record = manifest["units"].get(uid)
            if record is None:
                raise ValueError(f"refusing delete_spans: {uid} is not in the extraction manifest")
            if record["content_sha256"] != cls._digest(units[uid]):
                raise ValueError(f"refusing delete_spans: {uid} differs from its extraction record")
        if bundle is None:
            return
        plan, _, state, drift = cls._review_view(bundle, Q, root)
        if plan["manifest_digest"] != manifest["digest"]:
            raise ValueError("refusing delete_spans: review belongs to a different extraction manifest")
        if drift["rubric"] or drift["policy"] or drift["changed"]:
            raise ValueError("refusing delete_spans: review no longer matches its frozen rubric, policy, or source")
        open_items = sorted(item_id for item_id, entry in state["items"].items()
                            if entry["verdict"] in ("pending", "unresolved"))
        if open_items:
            raise ValueError("refusing delete_spans: review is incomplete; pending or unresolved: "
                             + ", ".join(open_items[:10]))
        approved = set(cls._review_summary(plan, state, drift)["approved_units"])
        for uid in targets:
            if uid not in approved:
                raise ValueError(f"refusing delete_spans: {uid} lacks a confirmed review decision")

    @staticmethod
    def _command(argv, *, cwd=".", binary=False):
        result = _jv_subprocess.run(
            argv, cwd=cwd, stdin=_jv_subprocess.DEVNULL, capture_output=True,
            **({} if binary else {"text": True, "encoding": "utf-8", "errors": "surrogateescape"}),
        )
        if result.returncode not in (0, 1):
            stderr = result.stderr.decode("utf-8", "replace") if binary else result.stderr
            raise RuntimeError(f"{argv[0]} failed ({result.returncode}): {stderr.strip()[:1000]}")
        return result

    @staticmethod
    def _unique(units, identifier, unit):
        key = identifier
        suffix = 2
        while key in units:
            key = f"{identifier}#{suffix}"
            suffix += 1
        units[key] = unit

    @staticmethod
    def _git_path(value):
        """Decode Git's C-quoted pathname, including UTF-8 octal escapes."""
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
            raw = bytearray()
            index = 0
            escapes = {"t": 9, "n": 10, "r": 13, "b": 8, "f": 12, "v": 11, "a": 7,
                       '"': 34, "\\": 92}
            while index < len(value):
                char = value[index]
                if char == "\\" and index + 1 < len(value):
                    index += 1
                    octal = _jv_re.match(r"[0-7]{3}", value[index:])
                    if octal:
                        raw.append(int(octal.group(), 8))
                        index += 3
                        continue
                    if value[index] in escapes:
                        raw.append(escapes[value[index]])
                        index += 1
                        continue
                    raw.extend(value[index].encode("utf-8", "surrogateescape"))
                else:
                    raw.extend(char.encode("utf-8", "surrogateescape"))
                index += 1
            value = raw.decode("utf-8", "surrogateescape")
        return value

    @classmethod
    def _diff_paths(cls, header):
        tail = header[len("diff --git "):]
        if tail.startswith('"'):
            quoted = _jv_re.match(r'^"(?:\\.|[^"\\])*"', tail)
            if not quoted:
                raise ValueError(f"malformed Git diff header: {header}")
            old, new = quoted.group(), tail[quoted.end():].strip()
        elif " b/" in tail:
            old, rest = tail.rsplit(" b/", 1)
            new = "b/" + rest
        elif ' "' in tail:
            old, rest = tail.rsplit(' "', 1)
            new = '"' + rest
        else:
            raise ValueError(f"malformed Git diff header: {header}")
        old, new = cls._git_path(old), cls._git_path(new)
        return (old[2:] if old.startswith("a/") else old,
                new[2:] if new.startswith("b/") else new)

    # Explicit OMP read elision/truncation markers seen on this host. A text containing any of them is
    # not the whole source, so unit builders refuse it instead of judging a silently shortened input.
    # "[end of ... bytes]" is a normal end-of-file footer and is deliberately NOT listed.
    ELISION_MARKERS = (
        ("[… N ln elided", _jv_re.compile(r"\[(?:…|\.\.\.)\s*\d+\s*ln elided")),
        ("re-read needed ranges", _jv_re.compile(r"re-read needed ranges")),
        ("… N more", _jv_re.compile(r"(?:…|\.\.\.)\s*\d+ more\b")),
        ("[Some lines truncated to", _jv_re.compile(r"\[Some lines truncated to")),
        ("Use artifact://", _jv_re.compile(r"Use artifact://")),
    )

    @classmethod
    def refuse_elided(cls, text, *, source="input"):
        """Raise ValueError naming the first OMP read elision/truncation marker found in `text`
        (see ELISION_MARKERS); return `text` unchanged otherwise. An elided read yields fewer units
        than the source holds, and the shortfall is otherwise invisible (an elided `agent://` read
        once produced zero units)."""
        if not isinstance(text, str):
            raise ValueError(f"{source} must be text, got {type(text).__name__}")
        for name, pattern in cls.ELISION_MARKERS:
            found = pattern.search(text)
            if found:
                line = text.count("\n", 0, found.start()) + 1
                raise ValueError(f"{source} is elided: marker {name!r} at line {line} "
                                 f"({found.group(0)!r}); re-read the full source before building units")
        return text

    @staticmethod
    def _table_cells(line):
        """Split one Markdown table row on unescaped `|` only; `\\|` becomes a literal `|`."""
        text = line.strip()
        cells, buffer, index, after_separator = [], [], 0, False
        while index < len(text):
            char = text[index]
            if char == "\\" and text[index + 1:index + 2] == "|":
                buffer.append("|")
                index += 2
                after_separator = False
                continue
            if char == "|":
                cells.append("".join(buffer).strip())
                buffer, after_separator = [], True
            else:
                buffer.append(char)
                after_separator = False
            index += 1
        cells.append("".join(buffer).strip())
        if text.startswith("|"):
            cells.pop(0)
        if after_separator and cells:
            cells.pop()
        return cells

    @classmethod
    def units_from_table(cls, text, *, key=None):
        """Parse the first GitHub Markdown table in `text` into {id: {column: value}}.

        Elided input is refused first. Cells split on unescaped `|` only (`\\|` stays one cell, unescaped
        to `|`) and are stripped. A row whose cell count differs from the header is refused, never
        truncated or padded. IDs come from the `key` column (empty or duplicate values are refused) or
        are 1-based row numbers as strings. A second table after the first is refused."""
        cls.refuse_elided(text, source="table")
        lines = [line for line in text.splitlines() if line.strip()]
        start = next((i for i, line in enumerate(lines) if line.strip().startswith("|")), None)
        empty = "no table rows; was the read elided?"
        if start is None or start + 1 >= len(lines):
            raise ValueError(empty)
        header = cls._table_cells(lines[start])
        divider = cls._table_cells(lines[start + 1])
        if not divider or not all(_jv_re.fullmatch(r":?-+:?", cell) for cell in divider):
            raise ValueError(empty)
        if not all(header) or len(set(header)) != len(header):
            raise ValueError(f"table header needs unique nonempty column names: {header!r}")
        if len(divider) != len(header):
            raise ValueError(f"table divider has {len(divider)} cells; header has {len(header)}")
        if key is not None and key not in header:
            raise ValueError(f"key column {key!r} is not in the table header {header!r}")
        rows, ragged, tail = [], [], start + 2
        while tail < len(lines) and lines[tail].strip().startswith("|"):
            cells = cls._table_cells(lines[tail])
            if len(cells) != len(header):
                ragged.append(len(rows) + 1)
            rows.append(cells)
            tail += 1
        if any(line.strip().startswith("|") for line in lines[tail:]):
            raise ValueError("more than one table in the text; pass one table")
        if not rows:
            raise ValueError(empty)
        if ragged:
            raise ValueError(f"table rows {ragged} have a cell count different from the header's "
                             f"{len(header)}; an unescaped `|` or a cut line? refusing to truncate")
        units, empty_keys = {}, []
        for number, cells in enumerate(rows, 1):
            record = dict(zip(header, cells))
            identifier = str(number) if key is None else record[key]
            if not identifier:
                empty_keys.append(number)
                continue
            if identifier in units:
                raise ValueError(f"duplicate table key {identifier!r} (row {number})")
            units[identifier] = record
        if empty_keys:
            raise ValueError(f"empty {key!r} value in table rows {empty_keys}")
        return units

    @classmethod
    def facts(cls, units, checks, *, settle=None, requires=None):
        """Attach deterministic facts to units and settle what a rule already decides, before any judging.

        units: {id: unit}. checks: {name: fn(unit) -> JSON value}. Each unit becomes
        {"unit": unit, "facts": {name: value}}; a dict unit instead gains a "facts" key (an existing
        "facts" key is refused, never overwritten). A check that raises, or returns a non-JSON value, is
        recorded in report["check_errors"][id][name]; the unit stays in the states without that fact and
        is never settled by rule.
        settle: fn(facts) -> None (undecided: judge it) or {column: decided_value}. Return a mapping only
        when the rule decides every question for that unit. The unit leaves the states and becomes a row
        {"id", "settled_by": "rule", column..., "qhash": None}; columns are the question ids exactly as
        judged rows name them (dotted columns such as `qid.conf` are never fabricated). A rule that
        raises is recorded in report["settle_errors"] and the unit stays to be judged.
        requires: {qid: [fact or field names]}. A judged unit lacking one (absent, None or "" in its facts
        or, for dict units, its fields) is listed in report["missing"][qid][id] and a warning is printed:
        the question asks about something the unit does not contain. Settled units are not asked.
        Returns (states, settled_rows, report); nothing here calls a judge."""
        if not isinstance(units, dict):
            raise ValueError("units must be a mapping of id to unit")
        if not isinstance(checks, dict) or not all(isinstance(name, str) and name and callable(fn)
                                                   for name, fn in checks.items()):
            raise ValueError("checks must map nonempty fact names to callables")
        if settle is not None and not callable(settle):
            raise ValueError("settle must be callable")
        requires = {} if requires is None else requires
        if not isinstance(requires, dict) or not all(
                isinstance(qid, str) and isinstance(names, (list, tuple)) and names
                and all(isinstance(name, str) for name in names) for qid, names in requires.items()):
            raise ValueError("requires must map question ids to nonempty lists of names")
        clobbered = [identifier for identifier, unit in units.items()
                     if isinstance(unit, dict) and "facts" in unit]
        if clobbered:
            raise ValueError(f"units already have a 'facts' key and would be clobbered: {clobbered[:10]}")
        states, settled, check_errors, settle_errors = {}, [], {}, {}
        missing = {qid: {} for qid in requires}
        reserved = {"id", "settled_by", "qhash"}
        for identifier, unit in units.items():
            values, errors = {}, {}
            for name, fn in checks.items():
                try:
                    value = fn(unit)
                    _jv_json.dumps(value, ensure_ascii=False)
                except Exception as error:
                    errors[name] = f"{type(error).__name__}: {error}"
                    continue
                values[name] = value
            if errors:
                check_errors[identifier] = errors
            if settle is not None and not errors:
                try:
                    decided = settle(values)
                    if decided is not None and (not isinstance(decided, dict) or not decided
                                                or not all(isinstance(column, str) and column
                                                           and column not in reserved
                                                           for column in decided)):
                        raise ValueError("settle must return None or a nonempty mapping of "
                                         f"question columns (not {sorted(reserved)}) to values")
                except Exception as error:
                    settle_errors[identifier] = f"{type(error).__name__}: {error}"
                    decided = None
                if decided is not None:
                    settled.append({"id": identifier, "settled_by": "rule", **decided, "qhash": None})
                    continue
            wrapped = {**unit, "facts": values} if isinstance(unit, dict) else {"unit": unit, "facts": values}
            states[identifier] = wrapped
            for qid, names in requires.items():
                absent = [name for name in names
                          if values.get(name) in (None, "")
                          and (not isinstance(unit, dict) or unit.get(name) in (None, ""))]
                if absent:
                    missing[qid][identifier] = absent
        missing = {qid: found for qid, found in missing.items() if found}
        report = {"check_errors": check_errors, "settle_errors": settle_errors, "missing": missing,
                  "counts": {"units": len(units), "judged": len(states), "settled": len(settled),
                             "check_errors": len(check_errors), "settle_errors": len(settle_errors),
                             "missing": {qid: len(found) for qid, found in missing.items()}}}
        print(f"facts: {len(units)} units · {len(settled)} settled by rule · {len(states)} to judge · "
              f"{len(check_errors)} check errors · {len(settle_errors)} settle errors")
        for qid, found in missing.items():
            print(f"warning: {len(found)} units lack names question {qid!r} depends on "
                  f"({', '.join(sorted({n for names in found.values() for n in names}))}); the judge "
                  "would answer about something the unit does not contain: " + ", ".join(map(str, list(found)[:10])))
        return states, settled, report

    @classmethod
    def git_units(cls, rev, *, by="file", cwd=".", paths=None, context=3):
        if by not in ("file", "hunk") or context < 0:
            raise ValueError("by must be 'file' or 'hunk' and context must be nonnegative")
        options = ["--no-color", "--no-ext-diff", f"-U{context}", "-M"]
        if rev == "WORKTREE":
            argv = ["git", "diff", *options]
        elif rev == "STAGED":
            argv = ["git", "diff", *options, "--cached"]
        elif ".." in rev:
            argv = ["git", "diff", *options, rev]
        else:
            argv = ["git", "show", "--diff-merges=first-parent", "--format=", *options, rev]
        if paths is not None:
            argv.extend(["--", *([paths] if isinstance(paths, str) else paths)])
        result = cls._command(argv, cwd=cwd)
        if result.returncode:
            raise RuntimeError("git diff/show failed")
        chunks = []
        for line in result.stdout.splitlines(keepends=True):
            if line.startswith("diff --git "):
                chunks.append([line])
            elif chunks:
                chunks[-1].append(line)
        units = {}
        hunk_header = _jv_re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
        for chunk in chunks:
            old, new = cls._diff_paths(chunk[0].rstrip("\r\n"))
            path = new
            status = "modified"
            for line in chunk[1:]:
                if line.startswith("rename to "):
                    path, status = cls._git_path(line[10:].rstrip("\r\n")), "renamed"
                elif line.startswith("new file mode "):
                    status = "added"
                elif line.startswith("deleted file mode "):
                    path, status = old, "deleted"
                elif line.startswith(("Binary files ", "GIT binary patch")):
                    status = "binary"
            if status == "modified" and old != new:
                status = "renamed"
            diff = "".join(chunk)
            if by == "file":
                cls._unique(units, path, {"file": path, "status": status, "diff": diff})
                continue
            starts = [index for index, line in enumerate(chunk) if hunk_header.match(line)]
            if not starts:
                cls._unique(units, f"{path}:meta",
                            {"file": path, "status": status, "header": "", "hunk": diff,
                             "line": None, "old_line": None})
                continue
            for index, start in enumerate(starts):
                heading = chunk[start].rstrip("\r\n")
                match = hunk_header.match(heading)
                old_line, old_size = int(match[1]), int(match[2] or 1)
                new_line, new_size = int(match[3]), int(match[4] or 1)
                anchor = f"+{new_line}" if new_size else f"-{old_line}"
                cls._unique(units, f"{path}:{anchor}",
                            {"file": path, "status": status, "header": heading,
                             "hunk": "".join(chunk[start:starts[index + 1] if index + 1 < len(starts) else len(chunk)]),
                             "line": new_line, "old_line": old_line})
        return units

    @staticmethod
    def _rg_text(field):
        if "text" in field:
            return field["text"]
        return _jv_base64.b64decode(field["bytes"]).decode("utf-8", "replace")

    @classmethod
    def rg_units(cls, pattern, *, cwd=".", globs=None, context=3, fixed=False,
                 case=True, max_units=50_000):
        if context < 0 or max_units < 0:
            raise ValueError("context and max_units must be nonnegative")
        argv = ["rg", "--json", "-n", "-C", str(context)]
        if fixed:
            argv.append("-F")
        argv.append("-s" if case else "-i")
        for glob in globs or ():
            argv.extend(["-g", glob])
        argv.extend(["-e", pattern, "."])
        result = cls._command(argv, cwd=cwd)
        if result.returncode == 1:
            return {}
        lines = _jv_collections.defaultdict(dict)
        hits = []
        for raw in result.stdout.splitlines():
            event = _jv_json.loads(raw)
            if event["type"] not in ("match", "context"):
                continue
            data = event["data"]
            path = cls._rg_text(data["path"])
            if path.startswith("./"):
                path = path[2:]
            number = data["line_number"]
            text = cls._rg_text(data["lines"]).rstrip("\r\n")
            for offset, part in enumerate(text.splitlines() or [""]):
                lines[path][number + offset] = part
            if event["type"] == "match":
                hits.append((path, number, text))
                if len(hits) > max_units:
                    raise ValueError(f"rg matches exceed max_units={max_units}: {len(hits)}")
        units = {}
        for path, number, text in hits:
            surrounding = [
                f"{'>' if pos == number else ' '}{pos:>4}| {lines[path][pos]}"
                for pos in range(number - context, number + context + 1)
                if pos in lines[path]
            ]
            cls._unique(units, f"{path}:{number}",
                        {"file": path, "line": number, "match": text,
                         "context": "\n".join(surrounding)})
        return units

    @staticmethod
    def dep(dist, module=None):
        """Import `module`, installing `dist` into a jevify-owned target dir when missing.
        `%pip` needs pip inside the kernel interpreter; a system Python often lacks it."""
        name = module or dist.replace("-", "_")
        base = _jv_os.environ.get("XDG_CACHE_HOME") or str(_jv_pathlib.Path.home() / ".cache")
        info = _jv_sys.version_info
        target = _jv_pathlib.Path(base) / "jevify" / f"py{info[0]}.{info[1]}"
        if str(target) not in _jv_sys.path:
            _jv_sys.path.append(str(target))
        try:
            return _jv_importlib.import_module(name)
        except ImportError:
            pass
        target.mkdir(parents=True, exist_ok=True)
        if _jv_shutil.which("uv"):
            argv = ["uv", "pip", "install", "--quiet", "--python", _jv_sys.executable, "--target", str(target), dist]
        else:
            argv = [_jv_sys.executable, "-m", "pip", "install", "--quiet", "--target", str(target), dist]
        print(f"jevify: installing {dist} into {target}")
        result = _jv_subprocess.run(argv, stdin=_jv_subprocess.DEVNULL, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(f"installing {dist} failed ({result.returncode}): {result.stderr.strip()[:1000]}")
        _jv_importlib.invalidate_caches()
        return _jv_importlib.import_module(name)

    @classmethod
    def ast_units(cls, pattern=None, *, lang, rule=None, cwd=".", globs=None):
        """Match `pattern`, or an ast-grep `rule` dict (kind/regex/has/inside/any/not) for spans a
        pattern cannot express, such as a definition together with its decorators."""
        if (pattern is None) == (rule is None):
            raise ValueError("pass exactly one of pattern or rule")
        matcher = {"pattern": pattern} if rule is None else rule
        SgRoot = cls.dep("ast-grep-py", "ast_grep_py").SgRoot
        extensions = {
            "typescript": ("*.ts", "*.tsx"), "tsx": ("*.tsx",),
            "javascript": ("*.js", "*.jsx"), "jsx": ("*.jsx",),
            "python": ("*.py",), "rust": ("*.rs",), "go": ("*.go",),
            "java": ("*.java",), "c": ("*.c", "*.h"),
            "cpp": ("*.cc", "*.cpp", "*.cxx", "*.hpp", "*.h"),
            "json": ("*.json",), "html": ("*.html",), "css": ("*.css",),
        }
        selected = globs if globs is not None else extensions.get(lang)
        if selected is None:
            raise ValueError(f"no default file globs for {lang!r}; supply globs")
        argv = ["rg", "--files", "-0"]
        for glob in selected:
            argv.extend(["-g", glob])
        argv.append(".")
        result = cls._command(argv, cwd=cwd, binary=True)
        if result.returncode == 1:
            return {}
        units = {}
        skipped = damaged = 0
        for raw in sorted(filter(None, result.stdout.split(b"\0"))):
            path = raw.decode("utf-8", "surrogateescape")
            if path.startswith("./"):
                path = path[2:]
            file_lang = ("tsx" if lang == "typescript" and path.endswith(".tsx") else
                         "jsx" if lang == "javascript" and path.endswith(".jsx") else lang)
            try:
                source = (_jv_pathlib.Path(cwd) / path).read_text(encoding="utf-8", errors="replace")
                root = SgRoot(source, file_lang).root()
                # Files with parse errors are still searched; skipping them would drop real hits.
                damaged += bool(root.find_all(kind="ERROR"))
            except (OSError, UnicodeError, ValueError, RuntimeError):
                skipped += 1
                continue
            # Invalid patterns or rules must surface, not masquerade as file parse failures.
            for node in root.find_all(**matcher):
                span = node.range()
                start = span.start.line + 1
                end = span.end.line + (0 if span.end.line > span.start.line and span.end.column == 0 else 1)
                cls._unique(units, f"{path}:{start}",
                            {"file": path, "line": start, "end_line": end, "text": node.text()})
        print(f"ast_units: {len(units)} matches · {damaged} files with parse errors (searched anyway) "
              f"· {skipped} unreadable files skipped")
        return units

    @classmethod
    def line_units(cls, source, *, keep=None, drop=None, normalize=True, examples=3):
        if examples < 0:
            raise ValueError("examples must be nonnegative")
        if isinstance(source, _jv_os.PathLike):
            lines = _jv_pathlib.Path(source).read_text(encoding="utf-8").splitlines()
        elif isinstance(source, str):
            try:
                is_path = "\n" not in source and _jv_pathlib.Path(source).is_file()
            except OSError:
                is_path = False
            lines = (_jv_pathlib.Path(source).read_text(encoding="utf-8") if is_path else source).splitlines()
        else:
            lines = list(source)
        keep_re = _jv_re.compile(keep) if isinstance(keep, str) else keep
        drop_re = _jv_re.compile(drop) if isinstance(drop, str) else drop
        uuid = _jv_re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", _jv_re.I)
        hex_id = _jv_re.compile(r"(?<![\w])(?:0x)?[\da-f]{8,}(?![\w])", _jv_re.I)
        timestamp = _jv_re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[Tt ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:?\d{2})?)?\b")
        number = _jv_re.compile(r"(?<![\w])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?(?![\w])")
        grouped = {}
        for raw in lines:
            raw = raw.rstrip("\r\n")
            if (keep_re and not keep_re.search(raw)) or (drop_re and drop_re.search(raw)):
                continue
            signature = raw.strip()
            if not signature:
                continue
            if normalize:
                for expression, replacement in ((uuid, "<uuid>"), (timestamp, "<ts>"),
                                                (hex_id, "<hex>"), (number, "<n>")):
                    signature = expression.sub(replacement, signature)
            entry = grouped.setdefault(signature, {"signature": signature, "count": 0, "examples": []})
            entry["count"] += 1
            if len(entry["examples"]) < examples:
                entry["examples"].append(raw)
        units = {}
        for signature, entry in sorted(grouped.items(), key=lambda item: (-item[1]["count"], item[0])):
            cls._unique(units, _jv_hashlib.sha1(signature.encode("utf-8")).hexdigest()[:10], entry)
        return units

    @classmethod
    def _cap(cls, value, cap):
        if isinstance(value, str) and len(value) > cap:
            kept = cap
            while True:
                marker = f"\n…[truncated {len(value) - kept} chars]"
                next_kept = max(0, cap - len(marker))
                if next_kept == kept:
                    return value[:kept] + marker, 1
                kept = next_kept
                if kept == 0 and len(marker) > cap:
                    raise ValueError("cap is too small to include the truncation marker")
        if isinstance(value, dict):
            entries = [((key, *cls._cap(item, cap))) for key, item in value.items()]
            return {key: item for key, item, _ in entries}, sum(count for _, _, count in entries)
        if isinstance(value, (list, tuple)):
            entries = [cls._cap(item, cap) for item in value]
            return [item for item, _ in entries], sum(count for _, count in entries)
        return value, 0

    @classmethod
    def _nonempty(cls, value):
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, dict):
            return any(cls._nonempty(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(cls._nonempty(item) for item in value)
        return True  # Zero and false are meaningful evidence, not empty states.

    @staticmethod
    def jev_tokens(text, *, timeout=20):
        """Jev token count of `text` via `omp toks <file> --json` (omp >= 18.3.0), or None when
        omp is missing, times out, or prints nothing parsable. Always pass a timeout: long runs
        of one repeated character tokenize pathologically slowly."""
        binary = _jv_shutil.which("omp")
        if binary is None:
            return None
        handle, path = _jv_tempfile.mkstemp(prefix="jevify-toks-", suffix=".txt")
        try:
            with _jv_os.fdopen(handle, "w", encoding="utf-8", errors="replace") as output:
                output.write(text)
            result = _jv_subprocess.run(
                [binary, "toks", path, "--json"], stdin=_jv_subprocess.DEVNULL,
                capture_output=True, text=True, timeout=timeout)
            if result.returncode:
                return None
            for entry in _jv_json.loads(result.stdout)["encodings"]:
                if entry.get("encoding") == "Jev":
                    tokens = entry.get("tokens")
                    return tokens if type(tokens) is int and tokens >= 0 else None
            return None
        except (OSError, _jv_subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError):
            return None
        finally:
            try:
                _jv_os.unlink(path)
            except OSError:
                pass

    @classmethod
    def states(cls, units, render=None, *, cap=None, report=None):
        ceiling = cls.CAP if cap is None else cap
        if ceiling <= 0:
            raise ValueError("cap must be positive")
        built, dropped, truncated, candidates = {}, 0, 0, {}
        for identifier, unit in units.items():
            value, count = cls._cap(render(unit) if render else unit, ceiling)
            truncated += count
            cap_reason = f"string fields capped at {ceiling} chars" if count else None
            if cls._nonempty(value):
                built[identifier] = value
                outcome = {"status": "submitted", "reason": cap_reason, "truncated": count,
                           "oversize": False, "unmeasured": False}
                serialized = _jv_json.dumps(value, ensure_ascii=False, default=str)
                # Byte-level BPE never yields more tokens than UTF-8 bytes (measured: dense ASCII
                # 0.76 tok/byte, CJK 0.64, emoji 0.56), so a state within MAX_JEV bytes cannot
                # exceed MAX_JEV tokens; only larger ones cost an `omp toks` call.
                if len(serialized.encode("utf-8", "surrogatepass")) > cls.MAX_JEV:
                    candidates[identifier] = value if isinstance(value, str) else serialized
            else:
                dropped += 1
                outcome = {"status": "dropped", "reason": "empty rendered unit", "truncated": count,
                           "oversize": False, "unmeasured": False}
            if report is not None:
                report[identifier] = outcome
        sizes = {}
        if candidates:
            with _jv_futures.ThreadPoolExecutor(max_workers=min(8, len(candidates))) as pool:
                sizes = dict(zip(candidates, pool.map(cls.jev_tokens, candidates.values())))
        oversize = [identifier for identifier, tokens in sizes.items()
                    if tokens is not None and tokens > cls.MAX_JEV]
        unmeasured = [identifier for identifier, tokens in sizes.items() if tokens is None]
        if report is not None:
            for identifier in oversize:
                report[identifier]["oversize"] = True
            for identifier in unmeasured:
                report[identifier]["unmeasured"] = True
        cls.stats["states"] = {"input": len(units), "built": len(built), "dropped": dropped,
                               "truncated": truncated, "cap": ceiling, "measured": len(sizes),
                               "oversize": oversize, "unmeasured": unmeasured}
        print(f"states: {len(built)} built · {dropped} empty dropped · {truncated} truncated "
              f"(cap {ceiling} chars) · {len(sizes)} Jev-measured")
        if oversize:
            print(f"warning: {len(oversize)} states exceed MAX_JEV={cls.MAX_JEV} Jev tokens; Jev rejects "
                  "these with max_tokens_exceeded; lower the cap or split: " + ", ".join(map(str, oversize)))
        if unmeasured:
            print(f"warning: {len(unmeasured)} states over {cls.MAX_JEV} bytes could not be Jev-measured "
                  "(`omp toks` missing, timed out or unparsable); check their size: "
                  + ", ".join(map(str, unmeasured)))
        return built

    _DEFINES = _jv_re.compile(
        r"^\+([ \t]*)(?:export\s+)?(?:default\s+)?(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?"
        r"(?:def|class|function|fn|struct|enum|trait|interface|type|func|const|let|var)\s+([A-Za-z_]\w*)",
        _jv_re.M)

    @classmethod
    def hunk_states(cls, units, *, siblings=6, users=3, width=700, cap=None):
        """States for git_units(by="hunk"): each hunk with its nearest same-file hunks (by line
        distance, not file order) and hunks whose added lines use a name this hunk defines at its
        outermost indentation (a helper serves the purpose of its users; methods and dunders are noise)."""
        def anchor(unit):
            return unit.get("line") or unit.get("old_line") or 0

        def changed(unit, marks):
            return "\n".join(line for line in unit["hunk"].splitlines()[1:] if line[:1] in marks)

        by_file, uses = _jv_collections.defaultdict(list), _jv_collections.defaultdict(list)
        for identifier, unit in units.items():
            if "hunk" not in unit:
                raise ValueError(f"{identifier} is not a git_units(by='hunk') unit")
            by_file[unit["file"]].append(identifier)
            for name in set(_jv_re.findall(r"[A-Za-z_]\w*", changed(unit, "+"))):
                uses[name].append(identifier)

        def brief(identifier):
            unit = units[identifier]
            return f"{unit['file']} {unit['header'][:140]}\n{changed(unit, '+-')[:width]}"

        rendered = {}
        for identifier, unit in units.items():
            near = sorted((other for other in by_file[unit["file"]] if other != identifier),
                          key=lambda other: abs(anchor(units[other]) - anchor(unit)))[:siblings]
            callers = []
            defined = [(len(indent.expandtabs(4)), name) for indent, name in cls._DEFINES.findall(unit["hunk"])
                       if not name.startswith("__")]
            outer = min((depth for depth, _ in defined), default=0)
            for name in dict.fromkeys(name for depth, name in defined if depth == outer):
                for other in uses.get(name, ()):
                    if len(callers) < users and other != identifier and other not in near and other not in callers:
                        callers.append(other)
            rendered[identifier] = {
                "file": unit["file"], "status": unit["status"], "hunk": unit["hunk"],
                "nearest_sibling_hunks": [brief(other) for other in near],
                "hunks_using_names_defined_here": [brief(other) for other in callers],
            }
        return cls.states(rendered, cap=cap)

    @classmethod
    def group(cls, units, *, by, k=8, render=None, shared=None, cap=None, report=None):
        """Return bounded group states and an explicit original/slot/exclusion manifest."""
        if k < 1:
            raise ValueError("k must be positive")
        ceiling = cls.CAP if cap is None else cap
        if ceiling <= 0:
            raise ValueError("cap must be positive")
        buckets, excluded = {}, {}
        for identifier, unit in units.items():
            value = render(unit) if render else unit
            if cls._nonempty(value):
                buckets.setdefault(by(unit), []).append((identifier, value))
            else:
                excluded[identifier] = "empty rendered unit"
        groups, slots = {}, {}
        for key, members in buckets.items():
            for start in range(0, len(members), k):
                part = members[start:start + k]
                identifier = f"{key}#{start // k + 1}"
                while identifier in groups:
                    identifier += "#2"
                state = {"group": key, "units": {f"s{i}": value for i, (_, value) in enumerate(part)}}
                if shared is not None:
                    state["shared"] = shared(key)
                groups[identifier] = state
                slots[identifier] = [uid for uid, _ in part]
        group_report = {}
        bounded = cls.states(groups, cap=ceiling, report=group_report)
        for gid in groups.keys() - bounded.keys():
            excluded.update({uid: "empty group state" for uid in slots[gid]})
        if report is not None:
            owner = {uid: gid for gid, ids in slots.items() for uid in ids}
            for uid in units:
                if uid not in owner:
                    report[uid] = {"status": "excluded", "reason": excluded[uid], "truncated": 0,
                                   "oversize": False, "unmeasured": False, "state": None, "slot": None}
                    continue
                gid = owner[uid]
                kept = gid in bounded
                report[uid] = {"status": "submitted" if kept else "excluded",
                               "reason": group_report[gid]["reason"] if kept else "empty group state",
                               "truncated": group_report[gid]["truncated"],
                               "oversize": group_report[gid]["oversize"],
                               "unmeasured": group_report[gid]["unmeasured"],
                               "state": gid, "slot": slots[gid].index(uid)}
        return bounded, {"original_ids": list(units),
                         "slots": {gid: slots[gid] for gid in bounded}, "excluded": excluded}

    @classmethod
    def sample(cls, states, n=60, *, seed=None):
        if n < 0:
            raise ValueError("n must be nonnegative")
        ids = _jv_random.Random(cls.SEED if seed is None else seed).sample(sorted(states), min(n, len(states)))
        return {identifier: states[identifier] for identifier in ids}

    @staticmethod
    def slot_questions(Q, k):
        if k < 1:
            raise ValueError("k must be positive")
        output = {}
        for i in range(k):
            for qid, question in Q.items():
                copy = _jv_copy.deepcopy(question)
                copy["instructions"] = (
                    f"State holds up to {k} units under state.units keyed s0..s{k - 1}; "
                    f"judge ONLY units['s{i}'] (the rest is context). If units['s{i}'] "
                    "is absent, answer anything — the answer is discarded. " + copy["instructions"]
                )
                output[f"{qid}@s{i}"] = copy
        return output

    @classmethod
    def _item_good(cls, item, questions, required):
        answers = getattr(item, "answers", None)
        if not getattr(item, "ok", False) or not isinstance(answers, dict):
            return False
        try:
            for qid in required:
                cls._answer({}, qid, questions[qid], answers[qid])
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
        return True

    @classmethod
    async def _collect(cls, payload, questions, required, *, intent, concurrency, retries, deadline,
                       attempt=1, label="first", journal=None, attach_id=None, skip=()):
        """Submit payload, or attach to attach_id, and drain it. Each delivered item is journaled before it
        is accepted. A crash after acceptance resumes from that record; a crash before journaling leaves the
        item only in the host batch, where recovery can reach it without its provenance.

        Returns (found, outcome). outcome carries the status (drained, partial, submit_failed, attach_failed),
        the host batch ID as soon as the host returned it, the cost estimate, and coverage errors."""
        journal = _JvNoJournal() if journal is None else journal
        found, coverage, problem, cost, cost_known = {}, [], "no result delivered", 0.0, False
        host_done = host_total = None  # the host's own count, observed only from a batch this owner submitted
        batch, batch_id = None, attach_id
        try:
            if attach_id is None:
                # Durable before submission: a crash after this record leaves an attempt with no batch ID.
                journal.append({"event": "attempt", "attempt": attempt, "label": label,
                                "intent": intent, "keys": list(payload)})
                batch = globals()["judge_batch"](payload, questions, concurrency=concurrency,
                                                   retries=retries, intent=intent)
            else:
                batch = globals()["judge_batch"].attach(attach_id)
            batch_id = getattr(batch, "id", None) or attach_id
            # The host batch ID is durable as soon as the host returns it.
            journal.append({"event": "batch", "attempt": attempt, "batch_id": batch_id,
                            "attached": attach_id is not None, "host": _JvBundle.host()})
            while True:
                remaining = deadline - _jv_time.monotonic()
                if remaining <= 0:
                    problem = "judge drain timed out"
                    break
                try:
                    async for identifier, item in batch.drain_iter(min(60, remaining)):
                        cls._accept(payload, found, coverage, journal, attempt, batch_id,
                                    identifier, item, source="drain")
                except _JvJournalError:
                    raise
                except Exception as error:
                    problem = f"judge drain failed: {error}"
                    break
                snapshot = batch.status()
                raw_cost = snapshot.get("cost", 0)
                if isinstance(raw_cost, (int, float)):
                    cost = float(raw_cost)
                print(f"jevify {intent}: {snapshot.get('done', len(found))}/{snapshot.get('total', len(payload))} "
                      f"done · {snapshot.get('failed', 0)} failed · batch cost estimate={cost:.6g} "
                      f"· elapsed={snapshot.get('elapsedS', 0):.1f}s")
                # A fresh batch's done count is its own. An attached batch's count includes items an earlier
                # owner already drained, so only local acceptance can finish an attachment.
                done = attach_id is None and snapshot.get("done", 0) >= len(payload)
                if len(found) >= len(payload) or done or not snapshot.get("running", False):
                    break
            final = batch.status()
            if attach_id is None:
                # An attached batch's counts include items an earlier owner drained, so they are not ours.
                done_count, total_count = final.get("done"), final.get("total")
                if type(done_count) is int and type(total_count) is int:
                    host_done, host_total = done_count, total_count
            raw_cost = final.get("cost", 0)
            if isinstance(raw_cost, (int, float)):
                cost, cost_known = float(raw_cost), True
            if attach_id is not None:
                cls._recover_from_host(batch, payload, skip, found, coverage, journal, attempt, batch_id)
        except _JvJournalError:
            raise
        except Exception as error:
            problem = f"judge submission failed: {error}"
            if batch is None and attach_id is not None:
                journal.append({"event": "attach", "attempt": attempt, "batch_id": attach_id,
                                "error": str(error)[:500]})
            elif batch is None:
                journal.append({"event": "batch", "attempt": attempt, "batch_id": None, "attached": False,
                                "error": str(error)[:500], "host": _JvBundle.host()})
        finally:
            if batch is not None:
                try:
                    if len(found) < len(payload):
                        batch.cancel()
                finally:
                    batch.close()
        if batch is not None and not batch_id:
            message = "host batch ID missing"
            coverage.append(message)
            journal.append({"event": "coverage", "attempt": attempt, "message": message})
        if batch is None:
            status = "attach_failed" if attach_id is not None else "submit_failed"
        elif all(key in found for key in payload):
            status, problem = "drained", None
        else:
            status = "partial"
        outcome = {"batch_id": batch_id, "status": status, "problem": problem, "cost": cost,
                   "cost_known": cost_known, "coverage_errors": coverage,
                   "host_done": host_done, "host_total": host_total}
        # A failed attachment stays open, so a later resume on the same host session can try again.
        if status != "attach_failed":
            journal.append({"event": "end", "attempt": attempt, "status": status, "problem": problem,
                            "cost": cost, "cost_known": cost_known, "batch_id": batch_id})
        return found, outcome

    # Journal records keep the display model so a replayed row is rebuilt exactly; receipt attempt items
    # carry route and cache provenance only, since each row already shows its display model.
    _RECEIPT_ITEM_FIELDS = ("selected_model", "primary_model", "response_model", "fallback",
                            "cache", "questions", "cached_questions")
    _ITEM_FIELDS = ("model", *_RECEIPT_ITEM_FIELDS)

    @classmethod
    def _item_event(cls, attempt, batch_id, identifier, item, source):
        error = getattr(item, "error", None)
        return {"event": "item", "attempt": attempt, "batch_id": batch_id, "key": identifier,
                "source": source, "ok": bool(getattr(item, "ok", False)),
                "error": None if error is None else str(error),
                "answers": getattr(item, "answers", None),
                "meta": {field: getattr(item, field, None) for field in cls._ITEM_FIELDS}}

    @classmethod
    def _item_from_event(cls, event):
        """The judge item a journal record stands for. Provenance absent from the record stays None."""
        meta = event.get("meta") if isinstance(event.get("meta"), dict) else {}
        fields = {field: meta.get(field) for field in cls._ITEM_FIELDS}
        return _jv_types.SimpleNamespace(ok=bool(event.get("ok")), answers=event.get("answers"),
                                         error=event.get("error"), **fields)

    @classmethod
    def _accept(cls, payload, found, coverage, journal, attempt, batch_id, identifier, item, *, source):
        """Persist a delivered item, then accept it. Unplanned or duplicate host keys are coverage errors."""
        if identifier not in payload:
            message = f"unexpected host key: {identifier}"
        elif identifier in found:
            message = f"duplicate host key: {identifier}"
        else:
            message = None
        if message is not None:
            coverage.append(message)
            journal.append({"event": "coverage", "attempt": attempt, "message": message})
            return
        journal.append(cls._item_event(attempt, batch_id, identifier, item, source))
        found[identifier] = item

    @classmethod
    def _recover_from_host(cls, batch, payload, skip, found, coverage, journal, attempt, batch_id):
        """Take answers and failures the host still holds for planned units this owner never accepted.
        Their drain-time provenance died with the earlier owner, so recovered items stay provenance-unknown."""
        try:
            answers, failures = dict(batch.results()), dict(batch.failed())
        except Exception as error:
            message = f"host result recovery failed: {error}"
            coverage.append(message)
            journal.append({"event": "coverage", "attempt": attempt, "message": message})
            return
        for identifier in payload:
            if identifier in found or identifier in skip:
                continue
            if identifier in answers:
                item = _jv_types.SimpleNamespace(ok=True, answers=answers[identifier], error=None,
                                                 **dict.fromkeys(cls._ITEM_FIELDS))
            elif identifier in failures:
                item = _jv_types.SimpleNamespace(ok=False, answers=None, error=str(failures[identifier]),
                                                 **dict.fromkeys(cls._ITEM_FIELDS))
            else:
                continue
            cls._accept(payload, found, coverage, journal, attempt, batch_id, identifier, item,
                        source="results")

    @classmethod
    def _evidence(cls, ctx, *, attempt, label, planned, outcome, found, resumed=False):
        """One attempt's record: what it planned, what the host delivered, and what stayed undelivered."""
        questions, required = ctx["questions"], ctx["required"]
        delivered = [key for key in planned if key in found]
        answered = [key for key in delivered if cls._item_good(found[key], questions, required[key])]
        return {"attempt": attempt, "label": label, "status": outcome["status"],
                "batch_id": outcome["batch_id"], "planned_ids": list(planned),
                "delivered_ids": delivered, "answered_ids": answered,
                "error_ids": [key for key in planned if key not in answered],
                "failure": outcome["problem"] or "no result delivered",
                "coverage_errors": list(outcome["coverage_errors"]),
                "cost_estimate": outcome["cost"], "cost_known": outcome["cost_known"],
                "host_done": outcome.get("host_done"), "host_total": outcome.get("host_total"),
                "resumed": resumed,
                "items": [{"id": key, "batch_id": outcome["batch_id"],
                           **{field: getattr(found[key], field, None) for field in cls._RECEIPT_ITEM_FIELDS}}
                          for key in delivered]}

    @classmethod
    def _outcomes(cls, ctx, attempts):
        """Each unit's outcome comes from the latest attempt that planned it, as run() retries work: that
        attempt's delivered item, or else that attempt's failure. Never-planned units are reported as such."""
        owner = {}
        for evidence, found in attempts:
            for key in evidence["planned_ids"]:
                owner[key] = (evidence, found)
        outcomes = {}
        for key in ctx["states"]:
            if key not in owner:
                outcomes[key] = (None, None, "not submitted before the interruption")
                continue
            evidence, found = owner[key]
            if key in found:
                outcomes[key] = (found[key], evidence, None)
            else:
                outcomes[key] = (None, evidence, evidence["failure"])
        return outcomes

    @staticmethod
    def _units(ctx, keys):
        slots = ctx["slots"]
        return [uid for key in keys for uid in ([key] if slots is None else slots[key])]

    @staticmethod
    def _uncertainty(evidences):
        """Reasons the batch cost estimate may understate host spend. Empty means every attempt drained
        and reported its cost."""
        reasons = []
        for evidence in evidences:
            label = f"attempt {evidence['attempt']} ({evidence['label']})"
            undelivered = len(evidence["planned_ids"]) - len(evidence["delivered_ids"])
            if evidence["status"] in ("submit_failed", "submit_unknown", "no_batch_id", "stale", "attach_failed"):
                reasons.append(f"{label}: {evidence['status']}; host acceptance or in-flight work may be unobserved")
            elif undelivered:
                reasons.append(f"{label}: {undelivered} planned unit(s) undelivered; cancelled or abandoned "
                               "work may still bill")
            elif not evidence["cost_known"]:
                reasons.append(f"{label}: batch cost not observed")
        return reasons

    @staticmethod
    def _same_host_session(recorded, current):
        """A host batch can be attached only from the host and boot session that submitted it."""
        return (isinstance(recorded, dict) and bool(current.get("boot_id"))
                and recorded.get("hostname") == current.get("hostname")
                and recorded.get("boot_id") == current.get("boot_id"))

    @staticmethod
    def _oversize(item):
        error = str(getattr(item, "error", None) or "")
        return "max_tokens_exceeded" in error

    @staticmethod
    def _fraction(value):
        probability = float(value)
        if not _jv_math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError(f"invalid probability: {value!r}")
        return probability

    @classmethod
    def _answer(cls, row, qid, question, answer):
        if not isinstance(answer, dict):
            raise ValueError(f"missing {qid} answer")
        kind = question["type"]
        if kind == "choice":
            labels = question["criteria"]
            choice = answer.get("choice")
            probabilities = answer.get("probabilities")
            if choice not in labels or not isinstance(probabilities, dict):
                raise ValueError(f"invalid {qid} choice")
            row[qid] = choice
            for label in labels:
                row[f"{qid}.{label}"] = cls._fraction(probabilities.get(label, 0.0))
            row[f"{qid}.conf"] = cls._fraction(answer["confidence"])
        elif kind == "bool":
            row[qid] = cls._fraction(answer["bool"])
        elif kind == "score":
            levels = len(question["criteria"])
            score = float(answer["score"])
            if not _jv_math.isfinite(score) or not 0 <= score <= levels - 1:
                raise ValueError(f"invalid {qid} weighted level index")
            confidence = cls._fraction(answer["confidence"])
            distribution = cls._distribution(qid, answer.get("probabilities"), levels)
            legend = cls._legend(qid, answer.get("legend"), levels)
            # Every field is validated before any write, so a rejected answer leaves no partial columns.
            row[qid] = score
            row[f"{qid}.conf"] = confidence
            row[f"{qid}.dist"] = distribution
            row[f"{qid}.legend"] = legend
        else:
            raise ValueError(f"unknown question type: {kind}")

    @staticmethod
    def _provenance_unknown(row):
        selected, primary = row.get("selected_model"), row.get("primary_model")
        fallback = row.get("fallback")
        cache, total, cached = row.get("cache"), row.get("questions"), row.get("cached_questions")
        if (row.get("provenance_unknown") or not isinstance(selected, str) or not selected
                or not isinstance(primary, str) or not primary or type(fallback) is not bool
                or fallback != (selected != primary)):
            return True
        if (cache not in ("hit", "partial", "miss") or type(total) is not int or total < 1
                or type(cached) is not int or not 0 <= cached <= total):
            return True
        return not ((cache == "hit" and cached == total)
                    or (cache == "miss" and cached == 0)
                    or (cache == "partial" and 0 < cached < total))

    @classmethod
    def _flat_row(cls, identifier, Q, item, digest, *, slot=None, group=None, failure=None,
                  batch_id=None, state=None, judged_at=None):
        row = {"id": identifier, "model": getattr(item, "model", None), "qhash": digest,
               "batch_id": batch_id,
               "state_sha256": None if state is None else cls.state_digest(state),
               "judged_at": judged_at, "assembly": "single"}
        for field in ("selected_model", "primary_model", "fallback", "response_model", "cache",
                      "questions", "cached_questions"):
            row[field] = getattr(item, field, None)
        row["provenance_unknown"] = cls._provenance_unknown(row)
        if group is not None:
            row["group"] = group
            row["slot"] = f"s{slot}"
        if failure is not None:
            row["error"] = failure
            return row
        error = getattr(item, "error", None) if item is not None else None
        if error or not getattr(item, "ok", False):
            row["error"] = str(error or "judge returned no answer")
            return row
        try:
            for qid, question in Q.items():
                key = qid if slot is None else f"{qid}@s{slot}"
                cls._answer(row, qid, question, item.answers[key])
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            # Keep route/cache evidence, but never expose a partially decoded verdict as success.
            for qid, question in Q.items():
                for name in cls._answer_columns(qid, question):
                    row.pop(name, None)
            row["error"] = f"invalid judge answer: {error}"
        return row

    @staticmethod
    def _validate_columns(Q):
        # Answers stay flat, so every emitted column must have a single owner.
        columns = {field: "helper metadata" for field in (
            "id", "model", "qhash", "batch_id", "selected_model", "primary_model",
            "fallback", "response_model", "cache", "questions", "cached_questions",
            "provenance_unknown", "group", "slot", "error", "why",
            "coverage_complete", "coverage_errors", "state_sha256", "judged_at", "assembly")}
        for qid, question in Q.items():
            for name in jv._answer_columns(qid, question):
                if name in columns:
                    raise ValueError(f"answer column {name!r} for question {qid!r} "
                                     f"collides with {columns[name]}")
                columns[name] = f"question {qid!r}"

    @classmethod
    async def run(cls, states, Q, *, intent, concurrency=32, retries=1, timeout=1800, grouping=None,
                  receipt_path=None, bundle_path=None, extraction=None, preflight=False):
        """Judge states and return list-compatible rows bound to their own rows.receipt.

        Grouped runs require the full manifest returned by group() or pack(). The receipt is also retained in
        stats['runs'] and optionally written to receipt_path. Its cost is a batch estimate; the host
        model-usage ledger accounts for all attempts.

        bundle_path: write a durable run bundle (plan, journal, rows, receipt) that jv.resume can continue.
        extraction: an extraction manifest from jv.extraction, validated against states before any submission.
        preflight: opt-in. Run jv.plan first and refuse, before any submission, unless every request is admissible.
        """
        if not isinstance(intent, str) or not intent.strip():
            raise ValueError("run requires a non-empty intent")
        if timeout <= 0 or concurrency < 1:
            raise ValueError("timeout and concurrency must be positive")
        ctx = cls._context(states, Q, grouping)
        if cls.frozen is not None and cls.frozen != ctx["digest"]:
            print(f"WARNING: question hash {ctx['digest']} differs from last frozen {cls.frozen}: in the same "
                  "sweep the wording drifted, so re-judge every unit; a new sweep should freeze its own Q")
        extraction_check = cls._check_extraction(extraction, ctx)
        preflight_report = cls._preflight(ctx, preflight)
        start = _jv_time.monotonic()
        deadline = start + timeout
        bundle = None
        if bundle_path is not None:
            bundle = _JvBundle(bundle_path)
            bundle.acquire()
        try:
            bundle_info = None
            if bundle is not None:
                if bundle.has_plan():
                    raise ValueError(f"bundle {bundle.path} already has a plan; continue it with jv.resume")
                body = cls._plan_body(ctx, intent=intent, extraction=extraction,
                                      extraction_check=extraction_check, preflight=preflight_report)
                bundle.write_plan(body)
                bundle_info = {"path": str(bundle.path), "digest": body["digest"], "resumed": False,
                               "host": body["host"]}
            attempts = await cls._fresh_passes(ctx, intent=intent, concurrency=concurrency, retries=retries,
                                               deadline=deadline, journal=bundle)
            return cls._conclude(ctx, attempts, intent=intent, start=start, receipt_path=receipt_path,
                                 bundle=bundle, bundle_info=bundle_info, resumed=False,
                                 extraction=extraction_check, preflight=preflight_report, resubmit=None)
        finally:
            if bundle is not None:
                bundle.release()

    @classmethod
    async def resume(cls, bundle_path, *, intent, concurrency=32, retries=1, timeout=1800,
                     resubmit_missing=False, receipt_path=None):
        """Continue a durable run bundle without re-judging what was already delivered.

        Delivered items come from the journal. An open attempt reattaches to its host batch only from the same
        host and boot session that submitted it; otherwise its undelivered units stay unresolved and the receipt
        is incomplete. Missing units are resubmitted only with resubmit_missing=True, which may repeat host-side
        work whose acceptance or cost is uncertain; the receipt records every such attempt. This is not
        exactly-once: a crash between host submission and its journal record leaves acceptance unknown.
        """
        if not isinstance(intent, str) or not intent.strip():
            raise ValueError("resume requires a non-empty intent")
        if timeout <= 0 or concurrency < 1:
            raise ValueError("timeout and concurrency must be positive")
        bundle = _JvBundle(bundle_path)
        bundle.acquire()
        try:
            plan = cls._verify_plan(bundle.read_plan())
            ctx = cls._context(plan["states"], plan["Q"], plan["grouping"])
            extraction = None
            if plan["extraction"] is not None:
                check = cls.validate_extraction(plan["extraction"], ctx["states"], grouping=ctx["grouping"])
                extraction = {**plan["extraction_check"],
                              "current": {"ok": check["ok"], "problems": check["problems"]}}
            records = cls._replay(bundle.replay(), ctx)
            host = _JvBundle.host()
            bundle.append({"event": "resume", "host": host, "resubmit_missing": bool(resubmit_missing)})
            start = _jv_time.monotonic()
            deadline = start + timeout
            attempts = []
            for record in records:
                attempts.append(await cls._replayed(ctx, record, intent=intent, host=host, journal=bundle,
                                                    concurrency=concurrency, retries=retries, deadline=deadline))
            resubmit = {"requested": bool(resubmit_missing), "submitted_ids": [], "skipped": None}
            outcomes = cls._outcomes(ctx, attempts)
            missing = [key for key in ctx["states"] if outcomes[key][0] is None]
            if missing and not resubmit_missing:
                resubmit["skipped"] = "not requested; pass resubmit_missing=True to resubmit missing units"
            elif missing and _jv_time.monotonic() >= deadline:
                resubmit["skipped"] = "deadline reached before resubmission"
            elif missing:
                number = max((record["attempt"] for record in records), default=0) + 1
                found, outcome = await cls._collect(
                    {key: ctx["states"][key] for key in missing}, ctx["questions"], ctx["required"],
                    intent=intent + " (resubmit)", concurrency=concurrency, retries=retries,
                    deadline=deadline, attempt=number, label="resubmit", journal=bundle)
                attempts.append((cls._evidence(ctx, attempt=number, label="resubmit", planned=missing,
                                               outcome=outcome, found=found), found))
                resubmit["submitted_ids"] = cls._units(ctx, missing)
            bundle_info = {"path": str(bundle.path), "digest": plan["digest"], "resumed": True,
                           "host": host}
            return cls._conclude(ctx, attempts, intent=intent, start=start, receipt_path=receipt_path,
                                 bundle=bundle, bundle_info=bundle_info, resumed=True, extraction=extraction,
                                 preflight=plan["preflight"], resubmit=resubmit)
        finally:
            bundle.release()

    @classmethod
    def _context(cls, states, Q, grouping):
        """Validate one submission once; run() and resume() share this exact contract."""
        Q = _jv_copy.deepcopy(Q)
        cls._validate_columns(Q)
        states = dict(states)
        # Receipts, saved rows, and bundle plans all key units by string; refuse before any spend or write.
        bad = [identifier for identifier in states if not isinstance(identifier, str)]
        if bad:
            raise ValueError(f"state IDs must be strings, got {', '.join(map(repr, bad[:5]))}")
        digest = cls.qhash(Q)
        slots, exclusions = None, {}
        original_ids = unit_ids = list(states)
        if grouping is not None:
            if (not isinstance(grouping, dict)
                    or set(grouping) != {"original_ids", "slots", "excluded"}):
                raise ValueError("grouping requires original_ids, slots, and excluded")
            grouping = _jv_copy.deepcopy(grouping)
            original_ids, slots, exclusions = (grouping["original_ids"], grouping["slots"],
                                               grouping["excluded"])
            if (not isinstance(original_ids, list)
                    or any(not isinstance(uid, str) for uid in original_ids)
                    or len(set(original_ids)) != len(original_ids)):
                raise ValueError("grouping original_ids must be unique string IDs")
            if (not isinstance(slots, dict) or set(states) != set(slots)
                    or any(not isinstance(ids, list) or not ids for ids in slots.values())):
                raise ValueError("slots must name every nonempty group state exactly once")
            unit_ids = [uid for ids in slots.values() for uid in ids]
            if any(not isinstance(uid, str) for uid in unit_ids) or len(set(unit_ids)) != len(unit_ids):
                raise ValueError("slots must map each original unit ID exactly once")
            if (not isinstance(exclusions, dict)
                    or any(not isinstance(uid, str) or not isinstance(reason, str) or not reason.strip()
                           for uid, reason in exclusions.items())
                    or set(unit_ids) & set(exclusions)
                    or set(unit_ids) | set(exclusions) != set(original_ids)):
                raise ValueError("grouping must account for every original ID once in slots or excluded")
            for gid, ids in slots.items():
                state = states[gid]
                units = state.get("units") if isinstance(state, dict) else None
                if not isinstance(units, dict) or set(units) != {f"s{i}" for i in range(len(ids))}:
                    raise ValueError(f"slots do not match actual state.units for group {gid}")
            questions = cls.slot_questions(Q, max(map(len, slots.values()))) if slots else {}
            required = {gid: [f"{qid}@s{i}" for i in range(len(ids)) for qid in Q]
                        for gid, ids in slots.items()}
        else:
            questions = Q
            required = {identifier: list(Q) for identifier in states}
        if states and not questions:
            raise ValueError("run requires questions")
        return {"Q": Q, "states": states, "digest": digest, "grouping": grouping, "slots": slots,
                "exclusions": exclusions, "original_ids": original_ids, "unit_ids": unit_ids,
                "questions": questions, "required": required}

    @classmethod
    def _check_extraction(cls, manifest, ctx):
        if manifest is None:
            return None
        check = cls.validate_extraction(manifest, ctx["states"], grouping=ctx["grouping"])
        if not check["ok"]:
            raise ValueError("extraction does not match the submitted states or source: "
                             + "; ".join(check["problems"]))
        return {"digest": check["digest"], "identity": check["identity"], "complete": check["complete"]}

    @classmethod
    def _preflight(cls, ctx, enabled):
        if not enabled:
            return None
        report = cls.plan(ctx["states"], ctx["Q"], grouping=ctx["grouping"])
        if not report["admissible"]:
            refused = report["over_budget"] + report["unmeasured"]
            over_labels = report.get("over_labels") or []
            raise ValueError(f"preflight refuses to submit: {len(report['over_budget'])} over budget, "
                             f"{len(report['unmeasured'])} unmeasured ({', '.join(refused)[:400]}), "
                             f"{len(over_labels)} question(s) over {cls.MAX_LABELS} labels "
                             f"({', '.join(map(str, over_labels))}); split or shrink these units or labels")
        return {key: value for key, value in report.items() if key != "requests"}

    @classmethod
    def _plan_body(cls, ctx, *, intent, extraction, extraction_check, preflight):
        body = {"schema": _JvBundle.SCHEMA, "intent": intent, "qhash": ctx["digest"],
                "questions": [[qid, question] for qid, question in ctx["Q"].items()],
                "states": ctx["states"], "grouping": ctx["grouping"],
                "extraction": extraction, "extraction_check": extraction_check, "preflight": preflight,
                "created": round(_jv_time.time(), 3), "host": _JvBundle.host()}
        return {**body, "digest": cls._digest(body)}

    @classmethod
    def _verify_plan(cls, plan):
        """Refuse a plan that was edited, truncated, or written by another schema, before any batch is touched."""
        if not isinstance(plan, dict) or plan.get("schema") != _JvBundle.SCHEMA:
            raise ValueError("not a jevify run bundle plan")
        body = {key: value for key, value in plan.items() if key != "digest"}
        if plan.get("digest") != cls._digest(body):
            raise ValueError("bundle plan digest does not match its contents")
        pairs = plan.get("questions")
        if (not isinstance(pairs, list)
                or any(not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str)
                       for pair in pairs)):
            raise ValueError("bundle questions must be ordered [id, question] pairs")
        Q = {qid: question for qid, question in pairs}
        if len(Q) != len(pairs):
            raise ValueError("bundle question IDs must be unique")
        if not isinstance(plan.get("states"), dict):
            raise ValueError("bundle states must be a mapping")
        if cls.qhash(Q) != plan.get("qhash"):
            raise ValueError("bundle question hash does not match its questions")
        return {**plan, "Q": Q}

    @classmethod
    def _replay(cls, events, ctx):
        """Fold the journal into attempt records, validated against the plan. Nothing is repaired silently."""
        records, by_number = [], {}
        for event in events:
            kind = event["event"]
            if kind in ("resume", "finish"):
                continue
            if kind == "attempt":
                number, keys = event.get("attempt"), event.get("keys")
                if (type(number) is not int or number in by_number or not isinstance(keys, list)
                        or len(set(keys)) != len(keys) or any(key not in ctx["states"] for key in keys)):
                    raise ValueError(f"journal attempt {number!r} does not match the bundle plan")
                record = {"attempt": number, "label": event.get("label"), "intent": event.get("intent"),
                          "keys": list(keys), "batch_id": None, "batch_recorded": False, "batch_host": None,
                          "delivered": {}, "coverage_errors": [], "end": None}
                by_number[number] = record
                records.append(record)
                continue
            record = by_number.get(event.get("attempt"))
            if record is None:
                raise ValueError(f"journal {kind!r} event refers to no recorded attempt")
            if kind == "batch":
                record["batch_recorded"] = True
                if record["batch_id"] is None and event.get("batch_id") is not None:
                    record["batch_id"], record["batch_host"] = event["batch_id"], event.get("host")
            elif kind == "attach":
                continue
            elif kind == "item":
                if event.get("key") not in record["keys"]:
                    raise ValueError(f"journal item for unplanned unit {event.get('key')!r}")
                record["delivered"][event["key"]] = event
            elif kind == "coverage":
                record["coverage_errors"].append(str(event.get("message")))
            elif kind == "end":
                record["end"] = event
            else:
                raise ValueError(f"unknown journal event {kind!r}")
        return records

    @classmethod
    async def _replayed(cls, ctx, record, *, intent, host, journal, concurrency, retries, deadline):
        """Rebuild one journaled attempt. An open attempt reattaches only from the host session that submitted
        it; otherwise its undelivered units stay undelivered and reported, never invented."""
        number, label, planned = record["attempt"], record["label"], record["keys"]
        found = {key: cls._item_from_event(event) for key, event in record["delivered"].items()}
        coverage = list(record["coverage_errors"])

        def closed(status, problem, batch_id, cost=0.0, cost_known=False):
            outcome = {"batch_id": batch_id, "status": status, "problem": problem, "cost": cost,
                       "cost_known": cost_known, "coverage_errors": coverage}
            return cls._evidence(ctx, attempt=number, label=label, planned=planned, outcome=outcome,
                                 found=found, resumed=True), found

        if record["end"] is not None:
            end = record["end"]
            return closed(end.get("status"), end.get("problem"), record["batch_id"],
                          float(end.get("cost") or 0.0), bool(end.get("cost_known")))
        if record["batch_id"] is None:
            if record["batch_recorded"]:
                return closed("no_batch_id", "the host returned no batch ID, so this work cannot be reattached", None)
            return closed("submit_unknown",
                          "submission was not recorded before the interruption; host acceptance is unknown", None)
        if not cls._same_host_session(record["batch_host"], host):
            recorded = record["batch_host"] if isinstance(record["batch_host"], dict) else {}
            return closed("stale",
                          f"batch {record['batch_id']} was submitted from host {recorded.get('hostname')!r} "
                          f"(boot {recorded.get('boot_id')!r}); this host session cannot attach to it",
                          record["batch_id"])
        remaining = {key: ctx["states"][key] for key in planned if key not in found}
        fresh, outcome = await cls._collect(
            remaining, ctx["questions"], ctx["required"], intent=intent + " (resumed)",
            concurrency=concurrency, retries=retries, deadline=deadline, attempt=number, label=label,
            journal=journal, attach_id=record["batch_id"], skip=set(found))
        outcome["coverage_errors"] = coverage + outcome["coverage_errors"]
        merged = {**found, **fresh}
        return cls._evidence(ctx, attempt=number, label=label, planned=planned, outcome=outcome,
                             found=merged, resumed=True), merged

    @classmethod
    async def _fresh_passes(cls, ctx, *, intent, concurrency, retries, deadline, journal):
        """run()'s protocol: submit every state once, then retry once the states whose first answer was neither
        good nor oversize. An oversize state fails identically on every attempt, so it is never retried."""
        attempts = []
        if not ctx["states"]:
            return attempts
        questions, required = ctx["questions"], ctx["required"]
        found, outcome = await cls._collect(ctx["states"], questions, required, intent=intent,
                                            concurrency=concurrency, retries=retries, deadline=deadline,
                                            attempt=1, label="first", journal=journal)
        attempts.append((cls._evidence(ctx, attempt=1, label="first", planned=list(ctx["states"]),
                                       outcome=outcome, found=found), found))
        pending = {key: state for key, state in ctx["states"].items()
                   if not (key in found and (cls._item_good(found[key], questions, required[key])
                                             or cls._oversize(found[key])))}
        if pending and _jv_time.monotonic() < deadline:
            found, outcome = await cls._collect(pending, questions, required, intent=intent + " (retry)",
                                                concurrency=concurrency, retries=retries, deadline=deadline,
                                                attempt=2, label="retry", journal=journal)
            attempts.append((cls._evidence(ctx, attempt=2, label="retry", planned=list(pending),
                                           outcome=outcome, found=found), found))
        return attempts

    @classmethod
    def _conclude(cls, ctx, attempts, *, intent, start, receipt_path, bundle, bundle_info, resumed,
                  extraction, preflight, resubmit):
        """Assemble rows and the receipt from every attempt, then publish them where the caller asked."""
        evidences = [evidence for evidence, _ in attempts]
        outcomes = cls._outcomes(ctx, attempts)
        slots, exclusions = ctx["slots"], ctx["exclusions"]
        Q, digest, questions, required = ctx["Q"], ctx["digest"], ctx["questions"], ctx["required"]
        rows = _JvRows()
        cache_counts = {"hit": 0, "partial": 0, "miss": 0, "unknown": 0}
        question_count, cached_count, counts_unknown = 0, 0, 0
        unresolved = []
        # The host reports no per-item completion time, so judged_at is when this collection concluded.
        judged_at = round(_jv_time.time(), 3)
        for key in ctx["states"]:
            item, evidence, issue = outcomes[key]
            cache = getattr(item, "cache", None)
            cache_counts[cache if cache in ("hit", "partial", "miss") else "unknown"] += 1
            total, cached = getattr(item, "questions", None), getattr(item, "cached_questions", None)
            if type(total) is int and type(cached) is int and 0 <= cached <= total:
                question_count += total
                cached_count += cached
            else:
                counts_unknown += 1
            failure = None if item is not None else (issue or "no result delivered")
            if item is not None and not cls._item_good(item, questions, required[key]):
                failure = str(getattr(item, "error", None) or "invalid judge answer")
                if cls._oversize(item):
                    failure = f"oversize: Jev max_tokens_exceeded; shrink or split this state ({failure[:400]})"
            batch_id = evidence["batch_id"] if evidence is not None else None
            if item is None:
                unresolved.extend(cls._units(ctx, [key]))
            if slots is None:
                rows.append(cls._flat_row(key, Q, item, digest, failure=failure, batch_id=batch_id,
                                          state=ctx["states"][key], judged_at=judged_at))
            else:
                for index, identifier in enumerate(slots[key]):
                    rows.append(cls._flat_row(identifier, Q, item, digest, slot=index, group=key,
                                              failure=failure, batch_id=batch_id,
                                              state=ctx["states"][key], judged_at=judged_at))
        models = _jv_collections.Counter(str(row["model"]) for row in rows if row["model"] is not None)
        errors = sum("error" in row for row in rows)
        elapsed = _jv_time.monotonic() - start
        cost = sum(evidence["cost_estimate"] for evidence in evidences if evidence["cost_known"])
        coverage_errors = [error for evidence in evidences for error in evidence["coverage_errors"]]
        uncertainty = cls._uncertainty(evidences)
        served, served_unknown = cls._served(rows)
        host_counts = cls._host_counts(evidences)
        summary = {"schema": 2, "strategy": "jev-grouped" if slots is not None else "jev-flat",
                   "intent": intent, "units": len(rows), "ok": len(rows) - errors, "errors": errors,
                   "models": dict(models), "cost": cost, "cost_kind": "batch_estimate",
                   "billing": "estimate_unverified", "assembly": "single",
                   "served_models": served, "served_unknown": served_unknown,
                   "comparable": len(served) == 1 and not served_unknown,
                   "host_counts": host_counts,
                   "cost_known": all(evidence["cost_known"] for evidence in evidences),
                   "cost_uncertain": bool(uncertainty), "cost_uncertainty": uncertainty,
                   "elapsed": elapsed, "qhash": digest, "question_hash": digest,
                   "frozen_question_hash": cls.frozen,
                   "batch_ids": [evidence["batch_id"] for evidence in evidences if evidence["batch_id"]],
                   "batches": evidences,
                   "unit_ids": ctx["unit_ids"], "original_ids": ctx["original_ids"],
                   "excluded_ids": [uid for uid in ctx["original_ids"] if uid in exclusions],
                   "exclusions": exclusions,
                   "answered_ids": [row["id"] for row in rows if "error" not in row],
                   "error_ids": [row["id"] for row in rows if "error" in row],
                   "error_details": {row["id"]: row["error"] for row in rows if "error" in row},
                   "unresolved_ids": unresolved,
                   "group_sizes": [] if slots is None else [len(ids) for ids in slots.values()],
                   "group_slots": {} if slots is None else slots,
                   "coverage_errors": coverage_errors,
                   "complete": not errors and not coverage_errors and not exclusions,
                   "provenance_complete": all(not cls._provenance_unknown(row) for row in rows),
                   "cache_counts": cache_counts, "questions": question_count,
                   "cached_questions": cached_count, "question_counts_unknown": counts_unknown,
                   "selected_models": dict(_jv_collections.Counter(
                       row["selected_model"] for row in rows if row["selected_model"])),
                   "primary_models": dict(_jv_collections.Counter(
                       row["primary_model"] for row in rows if row["primary_model"])),
                   "resumed": resumed, "resubmit": resubmit, "bundle": bundle_info,
                   "extraction": extraction, "preflight": preflight}
        rows.receipt = summary
        for row in rows:
            row["coverage_complete"] = summary["complete"]
            row["coverage_errors"] = list(coverage_errors)
        cls.stats["runs"].append(summary)
        print(f"jevify: {len(rows)} units · {len(rows) - errors} ok · {errors} errors · "
              f"served={served}{' (+%d unknown)' % served_unknown if served_unknown else ''} · "
              f"batch cost estimate (unverified, priced by host)={cost:.6g} · "
              f"elapsed={elapsed:.1f}s · qhash={digest}")
        print(_jv_json.dumps({"jevify_receipt": summary}, ensure_ascii=False))
        if receipt_path is not None:
            cls._write_json(receipt_path, summary)
        if bundle is not None:
            bundle.publish(_JvBundle.ROWS, lambda path: cls.save(rows, path))
            bundle.publish(_JvBundle.RECEIPT, lambda path: cls._write_json(path, summary))
            bundle.append({"event": "finish", "complete": summary["complete"],
                           "unresolved": len(unresolved)})
        return rows

    @staticmethod
    def _served(rows):
        """({model: answered rows}, answered rows whose served model is unknown)."""
        counts, unknown = _jv_collections.Counter(), 0
        for row in rows:
            if "error" in row:
                continue
            if row.get("model") is None:
                unknown += 1
            else:
                counts[str(row["model"])] += 1
        return dict(counts), unknown

    @staticmethod
    def _host_counts(evidences):
        """Answered units against the host's own done/total for the batches this run submitted. Counts the
        host did not report, or that an attachment shares with an earlier owner, stay None and never reconcile."""
        answered = sum(len(evidence["answered_ids"]) for evidence in evidences)
        planned = sum(len(evidence["planned_ids"]) for evidence in evidences)
        done = [evidence.get("host_done") for evidence in evidences]
        total = [evidence.get("host_total") for evidence in evidences]
        observed = bool(evidences) and all(type(value) is int for value in done + total)
        host_done = sum(done) if observed else None
        host_total = sum(total) if observed else None
        return {"answered": answered, "planned": planned, "host_done": host_done, "host_total": host_total,
                "reconciled": observed and host_done == answered and host_total == planned}

    @staticmethod
    def _write_json(path, value):
        with open(path, "w", encoding="utf-8") as output:
            _jv_json.dump(value, output, indent=2, ensure_ascii=False)
            output.write("\n")

    @staticmethod
    def _answer_columns(qid, question):
        kind = question["type"]
        names = [qid]
        if kind == "choice":
            names.extend(f"{qid}.{label}" for label in question["criteria"])
        if kind in ("choice", "score"):
            names.append(f"{qid}.conf")
        if kind == "score":
            names.extend((f"{qid}.dist", f"{qid}.legend"))
        return names

    @staticmethod
    def _ordered_identity(Q):
        identity = []
        for qid, question in Q.items():
            record = []
            for field in sorted(question):
                value = question[field]
                if field == "criteria" and isinstance(value, dict):
                    value = [[label, description] for label, description in value.items()]
                record.append([field, value])
            identity.append([qid, record])
        return identity

    @staticmethod
    def _ordered_digest(value):
        text = _jv_json.dumps(value, ensure_ascii=False, default=str)
        return _jv_hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def _distribution(cls, qid, value, levels):
        """Normalized, nonnegative level probabilities in level order; None when the host omitted them."""
        if value is None:
            return None  # absent is explicitly unavailable; never rebuilt from the weighted index
        if isinstance(value, dict):
            entries = {}
            for key, probability in value.items():
                name = str(key)
                if name in entries:
                    raise ValueError(f"duplicate {qid} probability level: {name}")
                entries[name] = probability
            if set(entries) != {str(index) for index in range(levels)}:
                raise ValueError(f"{qid} probabilities must name every level exactly once")
            ordered = [entries[str(index)] for index in range(levels)]
        elif isinstance(value, list) and len(value) == levels:
            ordered = value
        else:
            raise ValueError(f"invalid {qid} probabilities")
        distribution = [cls._fraction(probability) for probability in ordered]
        if abs(sum(distribution) - 1) > 1e-3:
            raise ValueError(f"{qid} probabilities must sum to 1")
        return distribution

    @classmethod
    def _legend(cls, qid, value, levels):
        """Host-supplied level legend, retained as supplied; None when absent."""
        if value is None:
            return None
        if isinstance(value, list) and len(value) == levels:
            legend = list(value)
            texts = legend
        elif (isinstance(value, dict) and len(value) == levels
              and {str(key) for key in value} == {str(index) for index in range(levels)}):
            legend = {str(key): text for key, text in value.items()}
            texts = legend.values()
        else:
            raise ValueError(f"invalid {qid} legend")
        if not all(isinstance(text, str) for text in texts):
            raise ValueError(f"{qid} legend entries must be strings")
        return legend

    @classmethod
    def _adopt_items(cls, source):
        """(items {key: item}, host status or None, source kind). A host batch is read from its results(),
        failed() and status() snapshots and is never drained, so adoption stays synchronous. Its results()
        carry answers only: the served model of those items is unknown, never taken from the batch status."""
        def blank(**fields):
            return _jv_types.SimpleNamespace(**{**dict.fromkeys(cls._ITEM_FIELDS), **fields})

        if all(callable(getattr(source, name, None)) for name in ("results", "failed", "status")):
            answers, failures = dict(source.results()), dict(source.failed())
            try:
                status = source.status()
                status = dict(status) if isinstance(status, dict) else {"error": "host status was not a mapping"}
            except Exception as error:
                status = {"error": f"host status unavailable: {error}"}
            items = {key: blank(ok=True, answers=value, error=None) for key, value in answers.items()}
            for key, error in failures.items():
                if key in items:
                    items[key] = blank(ok=False, answers=None,
                                       error="host reported both an answer and a failure for this key")
                else:
                    items[key] = blank(ok=False, answers=None, error=str(error))
            return items, status, "batch"
        if isinstance(source, dict):
            pairs = list(source.items())
        elif isinstance(source, (list, tuple)):
            pairs = []
            for entry in source:
                if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                    raise ValueError("adopt list sources must be (key, item) pairs, as drain() returns them")
                pairs.append(tuple(entry))
        else:
            raise ValueError("adopt source must be a host judge batch, a {key: item or answers} mapping, "
                             "or drain()'s [(key, item)] pairs")
        items = {}
        for key, value in pairs:
            if key in items:
                raise ValueError(f"adopt source delivers key {key!r} more than once")
            if isinstance(value, dict):
                items[key] = blank(ok=True, answers=value, error=None)
            elif hasattr(value, "answers") and hasattr(value, "ok"):
                own = getattr(value, "key", key)
                if own != key:
                    raise ValueError(f"host item keyed {own!r} was delivered under key {key!r}")
                items[key] = value
            else:
                raise ValueError(f"result {key!r} is neither a host judge item nor an answers mapping")
        return items, None, "items"

    @classmethod
    def adopt(cls, source, states, Q, *, intent=None, grouping=None, batch=None):
        """Bind built-in judge_batch() output to its submitted states as receipt-bound rows, so flag(), policy
        rules, review and reports apply to work the host notice ran directly.

        source: a host JudgmentBatch (read from results()/failed()/status(), never drained), a mapping
        {key: host item or answers mapping}, or drain()'s [(key, item)] pairs. Every state needs a result:
        a missing one becomes an error row; a result for a key outside `states` is refused because it cannot be
        bound to a submitted state. grouping is the manifest from group()/pack() for slot-expanded questions.
        Answers-only results (batch results(), answers mappings) have an unknown served model.

        batch: with drained items, the host batch (or its status() dict) they came from, so the receipt keeps the
        batch ID, cost estimate and host done/total. Drained items keep their served model; batch snapshots do not."""
        ctx = cls._context(states, Q, grouping)
        items, status, kind = cls._adopt_items(source)
        if batch is not None:
            if kind == "batch":
                raise ValueError("adopt takes batch= only with drained items; the batch source already has a status")
            try:
                snapshot = batch.status() if callable(getattr(batch, "status", None)) else batch
                status = dict(snapshot) if isinstance(snapshot, dict) else {"error": "host status was not a mapping"}
            except Exception as error:
                status = {"error": f"host status unavailable: {error}"}
            kind = "items+status"
        unknown = [key for key in items if key not in ctx["states"]]
        if unknown:
            raise ValueError("results for keys outside the submitted states cannot be bound: "
                             + ", ".join(map(repr, unknown[:10]))
                             + (f" (+{len(unknown) - 10} more)" if len(unknown) > 10 else ""))
        if cls.frozen is not None and cls.frozen != ctx["digest"]:
            print(f"WARNING: question hash {ctx['digest']} differs from last frozen {cls.frozen}: the adopted "
                  "answers were judged under different wording than the frozen rubric")
        slots, exclusions = ctx["slots"], ctx["exclusions"]
        Q, digest, questions, required = ctx["Q"], ctx["digest"], ctx["questions"], ctx["required"]
        status = {} if status is None else status
        if intent is None:
            intent = status.get("intent")
        batch_id = status.get("id") if isinstance(status.get("id"), str) and status.get("id") else None
        # The host reports no per-item completion time, so judged_at is when the answers were adopted.
        judged_at = round(_jv_time.time(), 3)
        rows = _JvRows()
        cache_counts = {"hit": 0, "partial": 0, "miss": 0, "unknown": 0}
        question_count, cached_count, counts_unknown = 0, 0, 0
        unresolved, answered_keys = [], []
        for key in ctx["states"]:
            item = items.get(key)
            cache = getattr(item, "cache", None)
            cache_counts[cache if cache in ("hit", "partial", "miss") else "unknown"] += 1
            total, cached = getattr(item, "questions", None), getattr(item, "cached_questions", None)
            if type(total) is int and type(cached) is int and 0 <= cached <= total:
                question_count += total
                cached_count += cached
            else:
                counts_unknown += 1
            failure = None if item is not None else "no result delivered"
            if item is None:
                unresolved.extend(cls._units(ctx, [key]))
            elif not cls._item_good(item, questions, required[key]):
                failure = str(getattr(item, "error", None) or "invalid judge answer")
                if cls._oversize(item):
                    failure = f"oversize: Jev max_tokens_exceeded; shrink or split this state ({failure[:400]})"
            else:
                answered_keys.append(key)
            if slots is None:
                rows.append(cls._flat_row(key, Q, item, digest, failure=failure, batch_id=batch_id,
                                          state=ctx["states"][key], judged_at=judged_at))
            else:
                for index, identifier in enumerate(slots[key]):
                    rows.append(cls._flat_row(identifier, Q, item, digest, slot=index, group=key,
                                              failure=failure, batch_id=batch_id,
                                              state=ctx["states"][key], judged_at=judged_at))
        coverage_errors, uncertainty = [], []
        if status.get("error"):
            coverage_errors.append(str(status["error"]))
        if status.get("running") is True:
            coverage_errors.append("host batch was still running: adopted a partial snapshot")
            uncertainty.append("host batch still running; its cost estimate may still grow")
        done, host_total = status.get("done"), status.get("total")
        observed = type(done) is int and type(host_total) is int
        delivered = sum(key in items for key in ctx["states"])
        if observed and (done != delivered or host_total != len(ctx["states"])):
            coverage_errors.append(f"host reported {done} of {host_total} done, but {delivered} of "
                                   f"{len(ctx['states'])} submitted states delivered a result")
        host_counts = {"answered": len(answered_keys), "planned": len(ctx["states"]),
                       "host_done": done if observed else None, "host_total": host_total if observed else None,
                       "reconciled": observed and done == len(answered_keys) and host_total == len(ctx["states"])}
        raw_cost = status.get("cost")
        cost_known = (isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool)
                      and _jv_math.isfinite(raw_cost) and status.get("running") is not True)
        cost = float(raw_cost) if cost_known else 0.0
        if kind == "items":
            uncertainty.append("adopted from drained items without batch=: no host batch status, so cost was not observed")
        elif not cost_known and status.get("running") is not True:
            uncertainty.append("batch cost not observed")
        elapsed = status.get("elapsedS")
        elapsed = float(elapsed) if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool) else 0.0
        models = _jv_collections.Counter(str(row["model"]) for row in rows if row["model"] is not None)
        errors = sum("error" in row for row in rows)
        served, served_unknown = cls._served(rows)
        summary = {"schema": 2, "strategy": "host-judge-batch-grouped" if slots is not None else "host-judge-batch",
                   "source": kind, "intent": intent, "units": len(rows), "ok": len(rows) - errors,
                   "errors": errors, "models": dict(models), "status_model": status.get("model"),
                   "cost": cost, "cost_kind": "batch_estimate", "billing": "estimate_unverified",
                   "assembly": "single", "served_models": served, "served_unknown": served_unknown,
                   "comparable": len(served) == 1 and not served_unknown, "host_counts": host_counts,
                   "cost_known": cost_known, "cost_uncertain": bool(uncertainty), "cost_uncertainty": uncertainty,
                   "elapsed": elapsed, "qhash": digest, "question_hash": digest,
                   "frozen_question_hash": cls.frozen, "batch_ids": [batch_id] if batch_id else [],
                   "batches": [], "unit_ids": ctx["unit_ids"], "original_ids": ctx["original_ids"],
                   "excluded_ids": [uid for uid in ctx["original_ids"] if uid in exclusions],
                   "exclusions": exclusions,
                   "answered_ids": [row["id"] for row in rows if "error" not in row],
                   "error_ids": [row["id"] for row in rows if "error" in row],
                   "error_details": {row["id"]: row["error"] for row in rows if "error" in row},
                   "unresolved_ids": unresolved,
                   "group_sizes": [] if slots is None else [len(ids) for ids in slots.values()],
                   "group_slots": {} if slots is None else slots,
                   "coverage_errors": coverage_errors,
                   "complete": not errors and not coverage_errors and not exclusions,
                   "provenance_complete": all(not cls._provenance_unknown(row) for row in rows),
                   "cache_counts": cache_counts, "questions": question_count,
                   "cached_questions": cached_count, "question_counts_unknown": counts_unknown,
                   "selected_models": dict(_jv_collections.Counter(
                       row["selected_model"] for row in rows if row["selected_model"])),
                   "primary_models": dict(_jv_collections.Counter(
                       row["primary_model"] for row in rows if row["primary_model"])),
                   "resumed": False, "resubmit": False, "bundle": None, "extraction": None, "preflight": None}
        rows.receipt = summary
        for row in rows:
            row["coverage_complete"] = summary["complete"]
            row["coverage_errors"] = list(coverage_errors)
        cls.stats["runs"].append(summary)
        print(f"jevify adopt: {len(rows)} units · {len(rows) - errors} ok · {errors} errors · "
              f"served={served}{' (+%d unknown)' % served_unknown if served_unknown else ''} · "
              f"complete={summary['complete']} · reconciled={host_counts['reconciled']} · qhash={digest}")
        print(_jv_json.dumps({"jevify_receipt": summary}, ensure_ascii=False))
        return rows

    @classmethod
    def scalar(cls, value, qid=None):
        """One answer as a plain value: choice -> label, bool -> P(yes), score -> weighted level index.

        value: a jv row with qid (returns row[qid]); a host answer mapping ({"type": ...}); or a host answers
        mapping or host item with qid. Anything it cannot reduce raises ValueError naming the problem."""
        def reduce(answer, name):
            if not isinstance(answer, dict):
                raise ValueError(f"{name} answer is {type(answer).__name__}, not a host answer mapping")
            kind = answer.get("type")
            try:
                if kind == "choice":
                    choice = answer["choice"]
                    if not isinstance(choice, str):
                        raise ValueError(f"{name} choice is not a label: {choice!r}")
                    return choice
                if kind == "bool":
                    return cls._fraction(answer["bool"])
                if kind == "score":
                    score = float(answer["score"])
                    if not _jv_math.isfinite(score):
                        raise ValueError(f"{name} score is not finite: {score!r}")
                    return score
            except KeyError as missing:
                raise ValueError(f"{name} {kind} answer lacks {missing.args[0]!r}") from None
            except TypeError as error:
                raise ValueError(f"{name} {kind} answer is malformed: {error}") from None
            raise ValueError(f"{name} answer has unknown type {kind!r}; expected choice, bool or score")

        if isinstance(value, dict) and "id" in value and "qhash" in value:
            if qid is None:
                raise ValueError("scalar of a row requires qid")
            if "error" in value:
                raise ValueError(f"row {value['id']!r} has no {qid!r} answer: {value['error']}")
            if qid not in value:
                raise ValueError(f"row {value['id']!r} has no column {qid!r}")
            return value[qid]
        if isinstance(value, dict) and "type" in value:
            return reduce(value, "host" if qid is None else repr(qid))
        if not isinstance(value, dict) and hasattr(value, "answers"):
            if qid is None:
                raise ValueError("scalar of a host item requires qid")
            answers = getattr(value, "answers", None)
            if not isinstance(answers, dict) or qid not in answers:
                raise ValueError(f"host item has no {qid!r} answer: "
                                 f"{getattr(value, 'error', None) or 'answers missing'}")
            return reduce(answers[qid], repr(qid))
        if not isinstance(value, dict):
            raise ValueError(f"scalar accepts a host answer, host item or jv row, not {type(value).__name__}")
        if qid is None:
            raise ValueError("scalar of an answers mapping requires qid")
        if qid in value:
            return reduce(value[qid], repr(qid))
        raise ValueError(f"mapping has no {qid!r} answer")

    @classmethod
    def uncertainty(cls, row, qid, question):
        """Primitive-specific measures for one successful row. Host confidence is reported, never redefined."""
        kind = question["type"]
        if kind == "choice":
            ranked = sorted((row[f"{qid}.{label}"] for label in question["criteria"]), reverse=True)
            return {"top_probability": ranked[0], "margin": ranked[0] - ranked[1],
                    "host_confidence": row[f"{qid}.conf"]}
        if kind == "bool":
            probability = row[qid]
            return {"p_yes": probability, "ambiguity": 1 - abs(2 * probability - 1)}
        if kind == "score":
            distribution = row.get(f"{qid}.dist")
            measures = {"weighted_index": row[qid], "host_confidence": row[f"{qid}.conf"],
                        "distribution_available": distribution is not None}
            if distribution is None:
                return {**measures, "spread": None}
            mean = sum(index * probability for index, probability in enumerate(distribution))
            variance = sum(probability * (index - mean) ** 2 for index, probability in enumerate(distribution))
            return {**measures, "spread": _jv_math.sqrt(variance)}
        raise ValueError(f"unknown question type: {kind}")

    @classmethod
    def policy(cls, Q, bounds):
        """Freeze primitive-specific escalation bounds bound to this exact ordered question identity.

        choice: {"top": p, "margin": p} escalates a low top probability or a narrow margin.
        bool: {"band": [lo, hi]} escalates P(yes) inside the band.
        score: {"spread": s} escalates wider distributions and rows whose distribution is unavailable.
        Thresholds are domain policy, not guarantees.
        """
        if not isinstance(bounds, dict) or not bounds:
            raise ValueError("policy requires at least one question bound")
        allowed = {"choice": {"top", "margin"}, "bool": {"band"}, "score": {"spread"}}
        questions = {}
        for qid, entry in bounds.items():
            if qid not in Q:
                raise ValueError(f"policy names unknown question {qid!r}")
            kind = Q[qid]["type"]
            if not isinstance(entry, dict) or not entry or not set(entry) <= allowed[kind]:
                raise ValueError(f"policy for {qid!r} accepts {sorted(allowed[kind])} for {kind} questions")
            clean = {}
            if kind == "choice":
                for name in ("top", "margin"):
                    if name in entry:
                        clean[name] = cls._fraction(entry[name])
            elif kind == "bool":
                band = entry["band"]
                if not isinstance(band, (list, tuple)) or len(band) != 2:
                    raise ValueError(f"band for {qid!r} must be [lo, hi]")
                lo, hi = cls._fraction(band[0]), cls._fraction(band[1])
                if lo > hi:
                    raise ValueError(f"band for {qid!r} must satisfy lo <= hi")
                clean["band"] = [lo, hi]
            else:
                spread = float(entry["spread"])
                if not _jv_math.isfinite(spread) or spread < 0:
                    raise ValueError(f"spread for {qid!r} must be a nonnegative finite number")
                clean["spread"] = spread
            questions[qid] = clean
        return {"schema": 1, "qhash": cls.qhash(Q), "questions": questions}

    @classmethod
    def policy_rule(cls, spec, Q):
        """Compile a frozen policy into a flag() rule; refuses a policy bound to other question identity."""
        if not isinstance(spec, dict) or spec.get("schema") != 1:
            raise ValueError("unsupported policy schema")
        if spec.get("qhash") != cls.qhash(Q):
            raise ValueError("policy is bound to a different question hash")
        questions = spec["questions"]

        def rule(row):
            if "error" in row:
                return None
            reasons = []
            for qid, bound in questions.items():
                question = Q[qid]
                measures = cls.uncertainty(row, qid, question)
                if question["type"] == "choice":
                    if "top" in bound and measures["top_probability"] < bound["top"]:
                        reasons.append(f"{qid}:low_top_probability")
                    if "margin" in bound and measures["margin"] < bound["margin"]:
                        reasons.append(f"{qid}:low_margin")
                elif question["type"] == "bool":
                    lo, hi = bound["band"]
                    if lo <= measures["p_yes"] <= hi:
                        reasons.append(f"{qid}:ambiguous_bool")
                elif measures["spread"] is None:
                    reasons.append(f"{qid}:score_distribution_unavailable")
                elif measures["spread"] > bound["spread"]:
                    reasons.append(f"{qid}:wide_score")
            return "; ".join(reasons) or None

        return rule

    @staticmethod
    def _calibration_labels(question, label):
        """Return (target, allowed verdict labels, label descriptions) for one primitive label."""
        kind = question["type"]
        if kind == "choice":
            if label not in question["criteria"]:
                raise ValueError("calibrate requires a choice question and one of its labels")
            return label, list(question["criteria"]), dict(question["criteria"])
        if kind == "bool":
            if label not in ("yes", "no"):
                raise ValueError('calibrate requires a bool question and label "yes" or "no"')
            criteria = question.get("criteria", {})
            return label, ["yes", "no"], {"yes": criteria.get("true"), "no": criteria.get("false")}
        if kind == "score":
            levels = [str(index) for index in range(len(question["criteria"]))]
            if isinstance(label, bool) or not isinstance(label, (int, str)) or str(label) not in levels:
                raise ValueError("calibrate requires a score question and one of its level indices")
            return str(label), levels, {str(index): text for index, text in enumerate(question["criteria"])}
        raise ValueError(f"unknown question type: {kind}")

    @staticmethod
    def _decision(row, qid, question):
        """Primitive decision compared across variants: choice label, bool side at 0.5, nearest score level."""
        value = row.get(qid)
        if "error" in row or value is None:
            return None
        kind = question["type"]
        if kind == "choice":
            return value
        if kind == "bool":
            return "yes" if value >= 0.5 else "no"
        return _jv_math.floor(value + 0.5)

    BAND = (0.3, 0.7)
    _POWER_Z = {0.9: 1.6448536269514722, 0.95: 1.959963984540054, 0.99: 2.5758293035489004}

    @classmethod
    def _bound_rows(cls, rows, Q, name):
        """Refuse rows carrying a rubric hash other than this Q's; rows without one are taken as given."""
        digest = cls.qhash(Q)
        foreign = sorted({str(row.get("qhash")) for row in rows
                          if row.get("qhash") is not None and row.get("qhash") != digest})
        if foreign:
            raise ValueError(f"{name} were judged under another rubric ({', '.join(foreign)}), not qhash {digest}")

    @classmethod
    def _in_band(cls, row, Q):
        """True when any choice top probability lies in [0.3, 0.7) or any bool P(yes) in [0.3, 0.7]."""
        lo, hi = cls.BAND
        for qid, question in Q.items():
            if question["type"] not in ("choice", "bool"):
                continue
            try:
                measures = cls.uncertainty(row, qid, question)
            except KeyError as error:
                raise ValueError(f"row {row.get('id')!r} lacks {qid} answer columns ({error})") from None
            if question["type"] == "choice" and lo <= measures["top_probability"] < hi:
                return True
            if question["type"] == "bool" and lo <= measures["p_yes"] <= hi:
                return True
        return False

    @classmethod
    def pilot_check(cls, rows, Q, *, rule=None, max_flag_rate=0.4, max_band_rate=0.4, catchall=(),
                    max_catchall_rate=0.4, min_units=10):
        """Decide from a pilot whether a full Jev sweep is worth running, before paying for it.

        Uses the frozen escalation rule: `rule` from jv.policy_rule(), else flag()'s defaults (errors,
        incomplete coverage, fallback). "provenance_unknown" is excluded from the flag rate: stock hosts never
        report selected/primary model, fallback or cache fields, so it is true on every row and says nothing
        about this pilot. A row flagged only for provenance is counted in `provenance_only`, not `flagged`.

        Rates: flag_rate = flagged units / all units (errors are flagged); band_rate = answered rows with any
        choice top probability in [0.3, 0.7) or bool P(yes) in [0.3, 0.7] (score questions have no band);
        catchall = {qid: share of answered rows whose label is in catchall[qid]}, e.g.
        {"verdict": ["other", "unrelated"]}. Any rate strictly above its max -> "use_direct". Fewer than
        `min_units` answered rows -> "too_small", whatever the rates. Otherwise "proceed".
        Returns {verdict, units, answered, flagged, provenance_only, flag_rate, band, band_rate, catchall,
        errors, reasons}. A "proceed" verdict is not a quality claim; flagged rows still need source reads.
        """
        for name, value in (("max_flag_rate", max_flag_rate), ("max_band_rate", max_band_rate),
                            ("max_catchall_rate", max_catchall_rate)):
            cls._fraction(value)
        if type(min_units) is not int or min_units < 1:
            raise ValueError("min_units must be a positive integer")
        catch = dict(catchall) if catchall else {}
        for qid, labels in catch.items():
            if qid not in Q or Q[qid]["type"] != "choice":
                raise ValueError(f"catchall names {qid!r}, which is not a choice question in Q")
            if isinstance(labels, str) or not labels:
                raise ValueError(f"catchall for {qid!r} must be a nonempty list of labels")
            unknown = [label for label in labels if label not in Q[qid]["criteria"]]
            if unknown:
                raise ValueError(f"catchall for {qid!r} names unknown labels {unknown}")
        cls._bound_rows(rows, Q, "pilot rows")
        units = len(rows)
        answered = [row for row in rows if "error" not in row]
        errors = units - len(answered)
        flagged_rows = cls.flag(rows, rule) if rows else []
        counted = [row for row in flagged_rows if any(why != "provenance_unknown" for why in row["why"])]
        band = sum(cls._in_band(row, Q) for row in answered)
        flag_rate = len(counted) / units if units else 0.0
        band_rate = band / len(answered) if answered else 0.0
        rates = {qid: (sum(row.get(qid) in labels for row in answered) / len(answered) if answered else 0.0)
                 for qid, labels in catch.items()}
        result = {"verdict": "proceed", "units": units, "answered": len(answered), "flagged": len(counted),
                  "provenance_only": len(flagged_rows) - len(counted), "flag_rate": flag_rate, "band": band,
                  "band_rate": band_rate, "catchall": rates, "errors": errors, "reasons": []}
        if len(answered) < min_units:
            result["verdict"] = "too_small"
            result["reasons"].append(f"{len(answered)} answered units (errors {errors}) < min_units {min_units}; "
                                     "pilot cannot decide; judge more units or read them directly")
            return result
        advice = "; read these units directly or write a deterministic script instead of a Jev sweep"
        if flag_rate > max_flag_rate:
            result["reasons"].append(f"flag_rate {flag_rate:.2f} > {max_flag_rate}: {len(counted)}/{units} units "
                                     "flagged by errors, coverage or the rule (provenance_unknown excluded)" + advice)
        if band_rate > max_band_rate:
            result["reasons"].append(f"band_rate {band_rate:.2f} > {max_band_rate}: {band}/{len(answered)} answers "
                                     "sit in the uncertain band [0.3, 0.7]" + advice)
        for qid, rate in rates.items():
            if rate > max_catchall_rate:
                result["reasons"].append(f"catchall {qid} rate {rate:.2f} > {max_catchall_rate}: labels "
                                         f"{list(catch[qid])} absorb the pilot, so the rubric does not fit" + advice)
        if result["reasons"]:
            result["verdict"] = "use_direct"
        return result

    @classmethod
    def remaining(cls, states, prior_rows, Q):
        """Split states into (todo_states, reused_rows) so pilot verdicts are not paid for twice.

        A prior row is reused only when it has no error, is not a grouped row (grouped verdicts depend on
        their group mates), carries qhash == jv.qhash(Q), and its state_sha256 equals
        jv.state_digest(states[id]). Anything else, including rows without a digest, is judged again.
        Reused rows are copies; combining them with a later run is a splice and must be reported as such.
        """
        if not isinstance(states, dict):
            raise ValueError("states must be a dict of unit id -> state")
        digest = cls.qhash(Q)
        reusable = {}
        for row in prior_rows:
            identifier = row.get("id")
            if (identifier not in states or identifier in reusable or "error" in row or "group" in row
                    or row.get("qhash") != digest or row.get("state_sha256") is None):
                continue
            if row["state_sha256"] == cls.state_digest(states[identifier]):
                reusable[identifier] = dict(row)
        todo = {identifier: state for identifier, state in states.items() if identifier not in reusable}
        reused = [reusable[identifier] for identifier in states if identifier in reusable]
        print(f"remaining: {len(todo)} to judge · {len(reused)} reused (same rubric and state digest)")
        return todo, reused

    @classmethod
    def _moved(cls, before, after, question, margin):
        """True when one primitive answer differs: another choice label, or bool/score moved by more than
        `margin` (score on the 0..1 scale of its level range)."""
        if question["type"] == "choice":
            return before != after
        scale = 1.0 if question["type"] == "bool" else max(len(question["criteria"]) - 1, 1)
        return abs(float(after) - float(before)) / scale > margin

    @classmethod
    def gate(cls, baseline_rows, current_rows, Q, *, qid, margin=0.15, rejudge_rows=None):
        """Regression gate between two sweeps of one rubric, separating real regressions from judge noise.

        Per unit in either set: missing from a set, an error row, or a missing qid answer -> unknown. Same
        verdict -> unchanged (same or missing digests) or source_changed (digests differ). Changed verdict
        (choice label differs, bool/score moved by more than `margin`): missing state_sha256 -> unknown;
        identical digests -> noise (the input did not change, so the judge did); differing digests -> regression
        only when a rejudge row of the current state repeats the current verdict, noise when the rejudge
        disagrees, unconfirmed when there is no usable rejudge row. Rows judged under another rubric than Q
        (any qhash differing) raise ValueError: a different rubric is not comparable. qhash_mismatch is
        therefore always False in a returned result.
        """
        if qid not in Q:
            raise ValueError(f"gate names unknown question {qid!r}")
        question = Q[qid]
        margin = cls._fraction(margin)
        rejudge_rows = list(rejudge_rows or [])
        for name, rows in (("baseline rows", baseline_rows), ("current rows", current_rows),
                           ("rejudge rows", rejudge_rows)):
            cls._bound_rows(rows, Q, name)

        def index(rows):
            return {row.get("id"): row for row in rows}

        baseline, current, rejudge = index(baseline_rows), index(current_rows), index(rejudge_rows)
        result = {"regressions": [], "noise": [], "unconfirmed": [], "source_changed": [], "unchanged": [],
                  "unknown": [], "qhash_mismatch": False}
        for identifier in list(dict.fromkeys([*baseline, *current])):
            before, after = baseline.get(identifier), current.get(identifier)
            if (before is None or after is None or "error" in before or "error" in after
                    or before.get(qid) is None or after.get(qid) is None):
                result["unknown"].append(identifier)
                continue
            old_digest, new_digest = before.get("state_sha256"), after.get("state_sha256")
            if not cls._moved(before[qid], after[qid], question, margin):
                changed_source = old_digest is not None and new_digest is not None and old_digest != new_digest
                result["source_changed" if changed_source else "unchanged"].append(identifier)
                continue
            if old_digest is None or new_digest is None:
                result["unknown"].append(identifier)
            elif old_digest == new_digest:
                result["noise"].append(identifier)
            else:
                again = rejudge.get(identifier)
                if (again is None or "error" in again or again.get(qid) is None
                        or again.get("state_sha256") != new_digest):
                    result["unconfirmed"].append(identifier)
                elif cls._moved(after[qid], again[qid], question, margin):
                    result["noise"].append(identifier)
                else:
                    result["regressions"].append(identifier)
        print("gate: " + " · ".join(f"{key}={len(value)}" for key, value in result.items()
                                    if isinstance(value, list)))
        return result

    @classmethod
    def power(cls, *, rate=0.5, margin=0.1, confidence=0.95):
        """Units needed to estimate a binomial proportion near `rate` within ±`margin` at `confidence`
        (normal approximation, rounded up). confidence is one of 0.9, 0.95, 0.99. rate=0.5 is the worst case."""
        if confidence not in cls._POWER_Z:
            raise ValueError(f"confidence must be one of {sorted(cls._POWER_Z)}")
        rate, margin = float(rate), float(margin)
        if not 0 < rate < 1 or not 0 < margin < 1:
            raise ValueError("rate and margin must lie strictly between 0 and 1")
        z = cls._POWER_Z[confidence]
        return _jv_math.ceil(z * z * rate * (1 - rate) / (margin * margin))

    class Ledger:
        """Append-only JSONL ledger of which unit ids were used for what, and who labeled gold.

        record(ids, purpose=..., labeler=None, labeler_kind=None) appends one fsynced line under an exclusive
        flock. purpose: "tune" | "heldout" | "gold"; labeler_kind: "human" | "agent" | "script" | "external"
        (required for gold). Recording an id already used for "tune" as "heldout" is refused with ValueError
        and records nothing: tuned units cannot serve as held-out evidence. independent_gold() splits gold ids
        into independent (some human or external label) and not_independent (agent or script labels only).
        """

        PURPOSES = ("tune", "heldout", "gold")
        KINDS = ("human", "agent", "script", "external")

        def __init__(self, path):
            self.path = _jv_pathlib.Path(path)

        def _read(self, handle):
            """Parse the ledger; a torn final line (no newline) is cut, any other bad line is refused."""
            handle.seek(0)
            data = handle.read()
            end = data.rfind(b"\n") + 1
            if end < len(data):
                handle.truncate(end)
                handle.flush()
                _jv_os.fsync(handle.fileno())
            entries = []
            for number, raw in enumerate(data[:end].split(b"\n")[:-1], start=1):
                try:
                    entry = _jv_json.loads(raw)
                except ValueError:
                    raise ValueError(f"ledger {self.path} line {number} is corrupt") from None
                if not isinstance(entry, dict) or entry.get("purpose") not in self.PURPOSES:
                    raise ValueError(f"ledger {self.path} line {number} is not a ledger entry")
                entries.append(entry)
            return entries

        def _locked(self, act):
            fcntl = _JvBundle._flock()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    return act(handle, self._read(handle))
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

        def entries(self):
            return self._locked(lambda handle, entries: entries)

        def record(self, ids, *, purpose, labeler=None, labeler_kind=None):
            ids = list(ids) if not isinstance(ids, str) else None
            if not ids or not all(isinstance(identifier, str) and identifier for identifier in ids):
                raise ValueError("ids must be a nonempty list of string unit ids")
            if purpose not in self.PURPOSES:
                raise ValueError(f"purpose must be one of {self.PURPOSES}")
            if labeler_kind is not None and labeler_kind not in self.KINDS:
                raise ValueError(f"labeler_kind must be one of {self.KINDS}")
            if purpose == "gold" and labeler_kind is None:
                raise ValueError("gold needs labeler_kind, or its independence cannot be established")

            def act(handle, entries):
                if purpose == "heldout":
                    tuned = {identifier for entry in entries if entry["purpose"] == "tune"
                             for identifier in entry["ids"]}
                    reused = sorted(set(ids) & tuned)
                    if reused:
                        raise ValueError(f"held-out reuse refused: {reused} were already used for tuning")
                entry = {"ids": ids, "purpose": purpose, "labeler": labeler, "labeler_kind": labeler_kind,
                         "at": round(_jv_time.time(), 3)}
                handle.seek(0, _jv_os.SEEK_END)
                handle.write((_jv_json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
                             .encode("utf-8"))
                handle.flush()
                _jv_os.fsync(handle.fileno())
                return entry

            return self._locked(act)

        def independent_gold(self):
            kinds = _jv_collections.defaultdict(set)
            for entry in self.entries():
                if entry["purpose"] == "gold":
                    for identifier in entry["ids"]:
                        kinds[identifier].add(entry["labeler_kind"])
            independent = sorted(identifier for identifier, found in kinds.items()
                                 if found & {"human", "external"})
            return {"independent": independent,
                    "not_independent": sorted(set(kinds) - set(independent)),
                    "labeler_kinds": {identifier: sorted(found) for identifier, found in sorted(kinds.items())}}

    @classmethod
    def ledger(cls, path):
        """Open the held-out/labeler ledger at `path` (created on first record). See jv.Ledger."""
        return cls.Ledger(path)

    @classmethod
    async def sensitivity(cls, states, Q, *, intent, max_variants, permutations=None, contexts=None,
                          grouping=None, concurrency=32, retries=1, timeout=1800):
        """Judge explicit bounded variants and report per-unit stability; never choose a consensus.

        permutations: {choice qid: [full label order, ...]} reorders one question at a time.
        contexts: {name: complete replacement states with the base unit IDs}; irrelevant or
        adversarial material is supplied by the caller. Variants are not crossed: each changes one
        thing from base. Each variant is one run() call, so timeout applies per variant.
        """
        if not isinstance(intent, str) or not intent.strip():
            raise ValueError("sensitivity requires a non-empty intent")
        if type(max_variants) is not int or not 2 <= max_variants <= 16:
            raise ValueError("max_variants must be an integer from 2 to 16")
        if not permutations and not contexts:
            raise ValueError("sensitivity requires explicit permutations or contexts")
        base = _jv_copy.deepcopy(Q)
        cls._validate_columns(base)
        variants = [("base", base, states)]
        for qid, orders in (permutations or {}).items():
            question = base.get(qid)
            if question is None or question["type"] != "choice":
                raise ValueError(f"permutation names an unknown or non-choice question: {qid!r}")
            labels = list(question["criteria"])
            for order in orders:
                order = list(order)
                if sorted(order) != sorted(labels) or len(set(order)) != len(order):
                    raise ValueError(f"permutation for {qid!r} must reorder every label exactly once")
                if order == labels:
                    raise ValueError(f"permutation for {qid!r} repeats the base order")
                variant = _jv_copy.deepcopy(base)
                variant[qid]["criteria"] = {label: question["criteria"][label] for label in order}
                variants.append((f"perm:{qid}:{_jv_json.dumps(order, ensure_ascii=False)}", variant, states))
        for name, variant_states in (contexts or {}).items():
            if not isinstance(variant_states, dict) or set(variant_states) != set(states):
                raise ValueError(f"context {name!r} must replace exactly the base unit IDs")
            variants.append((f"context:{name}", base, variant_states))
        if len(variants) > max_variants:
            raise ValueError(f"{len(variants)} variants exceed max_variants={max_variants}")
        identifiers = [identifier for identifier, _, _ in variants]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("sensitivity variant identifiers must be unique")
        strategy = "flat" if grouping is None else "grouped"
        grouping_digest = None if grouping is None else cls._ordered_digest(grouping)
        reports, answers = [], {}
        for identifier, variant_Q, variant_states in variants:
            rows = await cls.run(variant_states, variant_Q, intent=f"{intent} [{identifier}]",
                                 concurrency=concurrency, retries=retries, timeout=timeout,
                                 grouping=grouping)
            receipt = rows.receipt
            states_digest = cls._ordered_digest(variant_states)
            variant_qhash = cls.qhash(variant_Q)
            reports.append({
                "id": identifier, "qhash": variant_qhash, "states_digest": states_digest,
                "identity": cls._ordered_digest({"strategy": strategy, "qhash": variant_qhash,
                                                  "states": states_digest, "grouping": grouping_digest}),
                "batch_ids": receipt["batch_ids"], "cache_counts": receipt["cache_counts"],
                "errors": receipt["errors"], "complete": receipt["complete"]})
            for row in rows:
                for qid, question in base.items():
                    answers.setdefault(row["id"], {}).setdefault(qid, {})[identifier] = cls._decision(
                        row, qid, question)
        units, counts = [], {"stable": 0, "disagree": 0, "unresolved": 0}
        for unit_id, per_question in answers.items():
            for qid in base:
                decisions = per_question.get(qid, {})
                values = [decisions.get(identifier) for identifier in identifiers]
                if len({value for value in values if value is not None}) > 1:
                    status = "disagree"
                elif any(value is None for value in values):
                    status = "unresolved"
                else:
                    status = "stable"
                counts[status] += 1
                units.append({"id": unit_id, "question": qid, "status": status,
                              "answers": {identifier: "ERR" if decisions.get(identifier) is None
                                          else decisions[identifier] for identifier in identifiers}})
        warnings = ["host effective criteria order and provider/judge cache behaviour are unverified: "
                    "variant identities record what was submitted, not what the host evaluated"]
        if any(report["cache_counts"]["hit"] or report["cache_counts"]["partial"] for report in reports):
            warnings.append("cache hits were reported: some variant answers may reuse earlier verdicts "
                            "rather than a distinct judgment")
        for message in warnings:
            print(f"WARNING: {message}")
        print(f"sensitivity: {len(units)} unit-questions · stable={counts['stable']} · "
              f"disagree={counts['disagree']} · unresolved={counts['unresolved']} · variants={len(reports)}")
        return {"schema": 1, "claim": "stability across supplied variants; not correctness and not consensus",
                "strategy": strategy, "base_rubric_qhash": cls.qhash(base, ordered=False),
                "base_qhash": cls.qhash(base), "variants": reports, "units": units,
                "counts": counts, "warnings": warnings}

    @classmethod
    def flag(cls, rows, rule=None, *, report=None):
        """Escalate incomplete coverage independently of answer errors and provenance.

        `report` is the dict jv.states()/jv.group() filled. A row whose id (or whose `group` state id)
        was truncated, found oversize, or left unmeasured there is also flagged "truncated",
        "oversize" or "unmeasured": the judge saw less than the unit, so its answer needs a source read.
        Without a report nothing about truncation is inferred."""
        receipt = getattr(rows, "receipt", None)
        incomplete = receipt is not None and receipt.get("complete") is not True
        if not rows and (receipt is None or incomplete):
            raise ValueError("cannot establish coverage for empty rows without a complete run receipt")
        result, counts = [], _jv_collections.Counter()
        for row in rows:
            why = []
            if "error" in row:
                why.append("error")
            else:
                if incomplete or row.get("coverage_complete") is not True:
                    why.append("coverage_incomplete")
                if cls._provenance_unknown(row):
                    why.append("provenance_unknown")
                if row.get("fallback") is True:
                    why.append(f"fallback:{row.get('selected_model')}")
                if rule is not None:
                    reason = rule(row)
                    if reason:
                        why.append(reason if isinstance(reason, str) else "rule")
            if report is not None:
                outcome = report.get(row.get("id"))
                if outcome is None and row.get("group") is not None:
                    outcome = report.get(row["group"])
                if isinstance(outcome, dict):
                    if (outcome.get("truncated") or 0) > 0:
                        why.append("truncated")
                    if outcome.get("oversize"):
                        why.append("oversize")
                    if outcome.get("unmeasured"):
                        why.append("unmeasured")
            if why:
                copy = dict(row)
                copy["why"] = why
                result.append(copy)
                counts.update(why)
        print(f"flags: {len(result)} rows · reasons={dict(sorted(counts.items()))}")
        return result

    @staticmethod
    def show(rows_or_ids, states, *, fields=None, width=1200, limit=30):
        if limit < 0 or width < 1:
            raise ValueError("limit must be nonnegative and width positive")
        rows = list(rows_or_ids)
        metadata = {"id", "model", "qhash", "group", "slot", "why", "batch_id",
                    "selected_model", "primary_model", "fallback", "response_model", "cache",
                    "questions", "cached_questions", "provenance_unknown",
                    "coverage_complete", "coverage_errors", "state_sha256", "judged_at", "assembly"}
        for entry in rows[:limit]:
            row = entry if isinstance(entry, dict) else {"id": entry}
            identifier = row["id"]
            names = fields if fields is not None else [key for key in row if key not in metadata]
            verdict = {key: round(row[key], 2) if isinstance(row.get(key), float) else row.get(key)
                       for key in names if key in row}
            for key in ("model", "error", "why", "group"):
                if key in row:
                    verdict[key] = row[key]
            state = states.get(identifier, states.get(row.get("group")))
            serialized = _jv_json.dumps(state, indent=2, ensure_ascii=False, default=str)
            if len(serialized) > width:
                serialized = serialized[:max(0, width - 1)] + "…"
            print(f"{identifier} {_jv_json.dumps(verdict, ensure_ascii=False, default=str)}\n{serialized}")
        if len(rows) > limit:
            print(f"… {len(rows) - limit} more rows")

    @classmethod
    def tally(cls, rows, key, *, edges=None, levels=None):
        """Histogram a column. Score columns pass levels: bins are centred on whole level indices."""
        values = [row[key] for row in rows if key in row and "error" not in row]
        if values and all(isinstance(value, (int, float)) for value in values):
            if levels is not None:
                if edges is not None:
                    raise ValueError("pass edges or levels, not both")
                if type(levels) is not int or levels < 2:
                    raise ValueError("levels must be an integer of at least two")
                boundaries = tuple(index - 0.5 for index in range(levels + 1))
                names = [f"level {index}" for index in range(levels)]
            else:
                boundaries = tuple(cls.EDGES if edges is None else edges)
                if len(boundaries) < 2 or any(a >= b for a, b in zip(boundaries, boundaries[1:])):
                    raise ValueError("histogram edges must be strictly increasing")
                if edges is None and any(not 0 <= value <= 1 for value in values):
                    raise ValueError(f"{key}: values outside [0, 1] need explicit edges or levels; "
                                     "score answers are weighted level indices")
                names = [f"[{lo:g}, {hi:g})" for lo, hi in zip(boundaries, boundaries[1:])]
            spans = list(zip(boundaries, boundaries[1:]))
            result = {name: 0 for name in names}
            for value in values:
                for name, (lo, hi) in zip(names, spans):
                    if lo <= value < hi:
                        result[name] += 1
                        break
                else:
                    result["outside bins"] = result.get("outside bins", 0) + 1
        else:
            result = dict(_jv_collections.Counter(str(value) for value in values))
        if len(values) < len(rows):
            result["missing/error"] = len(rows) - len(values)
        print(f"{key}: " + " · ".join(f"{label}={count}" for label, count in result.items()))
        return result

    @classmethod
    def calibrate(cls, rows, states, Q, *, qid, label, agree=None, per_bin=20,
                  edges=None, model="slow", rubric="", seed=None):
        """Sample second-model opinions on one primitive label; agreement is not correctness.

        choice: `label` is a criterion; the stratum is its primary probability.
        bool: `label` is "yes" or "no"; the stratum is the primary P(label).
        score: `label` is a level index; the stratum is the primary probability of that level.
          Rows without a supplied distribution are counted as unsupported and never sampled.
        Agreement means the second model's verdict is in `agree` (default: `label` only).
        Grouped rows select their group/slot while retaining all shared and sibling context.
        Missing state, slot, or question-hash pairings raise before any opinions are requested.
        """
        question = Q[qid]
        kind = question["type"]
        target, allowed, label_map = cls._calibration_labels(question, label)
        if per_bin < 0:
            raise ValueError("per_bin must be nonnegative")
        boundaries = tuple(cls.EDGES if edges is None else edges)
        if len(boundaries) < 2 or any(a >= b for a, b in zip(boundaries, boundaries[1:])):
            raise ValueError("calibration edges must be strictly increasing")
        if agree is None:
            agreeing = {target}
        elif isinstance(agree, (str, int)):
            agreeing = {agree}
        else:
            agreeing = set(agree)
        agreeing = {str(name) for name in agreeing}
        if not agreeing <= set(allowed):
            raise ValueError("calibration agree names a verdict outside the question's labels")
        digest = cls.qhash(Q)
        seed_value = cls.SEED if seed is None else seed
        rng = _jv_random.Random(seed_value)
        criteria = "Labels and criteria: " + _jv_json.dumps(label_map, ensure_ascii=False)
        schema = {"type": "object", "properties": {
            "verdict": {"enum": allowed}, "reason": {"type": "string"}},
            "required": ["verdict", "reason"]}

        def primary(row):
            if kind == "choice":
                return row.get(f"{qid}.{target}")
            if kind == "bool":
                probability = row.get(qid)
                if probability is None:
                    return None
                return probability if target == "yes" else 1 - probability
            distribution = row.get(f"{qid}.dist")
            return None if distribution is None else distribution[int(target)]

        pools = [[] for _ in range(len(boundaries) - 1)]
        seen, prepared, unsupported = set(), {}, 0
        # Validate the entire pairing before the first slow call, not only sampled rows.
        for row in rows:
            uid = row["id"]
            if "error" in row or uid in seen:
                continue
            seen.add(uid)
            if "qhash" in row and row["qhash"] != digest:
                raise ValueError(f"calibration row {uid!r} was judged under question hash "
                                 f"{row['qhash']}, not {digest}")
            state_id = row.get("group", uid)
            if state_id not in states:
                raise ValueError(f"calibration state missing for row {uid!r}: {state_id!r}")
            state, instructions = states[state_id], question["instructions"]
            if "group" in row or "slot" in row:
                units = state.get("units") if isinstance(state, dict) else None
                slot = row.get("slot")
                if ("group" not in row or not isinstance(units, dict) or not units
                        or set(units) != {f"s{i}" for i in range(len(units))}
                        or not isinstance(slot, str) or slot not in units):
                    raise ValueError(f"invalid grouped calibration state/slot for row {uid!r}")
                instructions = cls.slot_questions({qid: question}, len(units))[f"{qid}@{slot}"]["instructions"]
            value = primary(row)
            prepared[uid] = {"state": state, "instructions": instructions,
                             "group": row.get("group"), "slot": row.get("slot"),
                             "initial": {"answer": row.get(qid), "p": value}}
            if value is None:
                unsupported += 1
                continue
            for index, (lo, hi) in enumerate(zip(boundaries, boundaries[1:])):
                if lo <= value < hi:
                    pools[index].append((uid, value))
                    break
        chosen = [(index, uid, value) for index, pool in enumerate(pools)
                  for uid, value in rng.sample(pool, min(per_bin, len(pool)))]
        outcome, contexts, slow_calls, waves = {}, {}, 0, 0
        for start in range(0, len(chosen), 16):
            wave = chosen[start:start + 16]
            waves += 1
            handles, submitted = [], []
            for _, uid, _ in wave:
                entry = prepared[uid]
                system = "\n\n".join(part for part in (rubric, entry["instructions"], criteria) if part)
                prompt = _jv_json.dumps(entry["state"], ensure_ascii=False, default=str)
                contexts[uid] = _jv_hashlib.sha256((system + "\n" + prompt).encode("utf-8")).hexdigest()[:12]
                try:
                    handles.append(globals()["completion"](prompt, model=model, system=system, schema=schema))
                    submitted.append(uid)
                    slow_calls += 1
                except Exception as error:
                    outcome[uid] = ("ERR", str(error)[:200])
            if not handles:
                continue
            try:
                outputs = globals()["wait"](handles, raise_errors=False)
            except Exception as error:
                outputs = [error] * len(handles)
            for uid, answer in zip(submitted, outputs):
                if isinstance(answer, dict) and answer.get("verdict") in allowed:
                    outcome[uid] = (answer["verdict"], str(answer.get("reason", "")))
                else:
                    outcome[uid] = ("ERR", str(answer)[:200])
            for uid in submitted[len(outputs):]:
                outcome[uid] = ("ERR", "no slow result")
        verdicts, sampled = {}, []
        for index, uid, value in chosen:
            slow, reason = outcome.get(uid, ("ERR", "not submitted"))
            if slow == "ERR":
                status = "unresolved"
            else:
                status = "agree" if slow in agreeing else "disagree"
            entry = prepared[uid]
            verdicts[uid] = {"slow": slow, "reason": reason, "p": value}
            sampled.append({"id": uid, "stratum": index, "initial": entry["initial"],
                            "final": {"status": status, "slow": slow},
                            "context": {"digest": contexts.get(uid), "group": entry["group"],
                                        "slot": entry["slot"]}})
        bins = []
        estimates = []
        prior = 0.0
        for index, (lo, hi) in enumerate(zip(boundaries, boundaries[1:])):
            observed = [verdicts[uid] for uid, _ in pools[index] if uid in verdicts]
            valid = [item for item in observed if item["slow"] != "ERR"]
            hits = sum(item["slow"] in agreeing for item in valid)
            agreement = hits / len(valid) if valid else None
            if agreement is not None:
                prior = agreement
            estimates.append(prior)
            bins.append({"lo": lo, "hi": hi, "n": len(pools[index]),
                         "calibrated": len(valid), "agreed": hits,
                         "errors": len(observed) - len(valid), "agreement": agreement})
            display = "n/a (estimate forward-filled)" if agreement is None else f"{agreement:.0%}"
            print(f"[{lo:g}, {hi:g}): n={len(pools[index])} slow={len(valid)} "
                  f"errors={len(observed) - len(valid)} agreement={display}")
        thresholds = []
        for threshold in (0.9, 0.8, 0.7, 0.6, 0.5):
            flagged = [index for index, pool in enumerate(pools) for _, p in pool if p >= threshold]
            expected_true = sum(estimates[index] for index in flagged)
            thresholds.append({"threshold": threshold, "flagged": len(flagged),
                               "expected_true": expected_true,
                               "expected_false": len(flagged) - expected_true})
            print(f"p ≥ {threshold:.1f}: flagged={len(flagged)} "
                  f"expected-true≈{expected_true:.1f} "
                  f"expected-false≈{len(flagged) - expected_true:.1f}")
        unresolved = sum(record["final"]["status"] == "unresolved" for record in sampled)
        evidence = {
            "claim": "second-model agreement under the stated criteria; not correctness or ground truth",
            "qid": qid, "label": target, "kind": kind, "qhash": digest,
            "criteria": {"agree": sorted(agreeing), "verdicts": allowed,
                         "strata": [[lo, hi] for lo, hi in zip(boundaries, boundaries[1:])],
                         "per_bin": per_bin,
                         "seed": seed_value},
            "population": {"rows": len(prepared), "supported": len(prepared) - unsupported,
                           "unsupported": unsupported},
            "sampled_ids": sorted(record["id"] for record in sampled),
            "sampled": sampled,
            "overhead": {"slow_calls": slow_calls, "waves": waves, "unresolved": unresolved,
                         "cost": "unknown: the helper does not read completion cost"},
        }
        print(f"calibration: rows={len(prepared)} unsupported={unsupported} slow_calls={slow_calls} "
              f"unresolved={unresolved}")
        return {"bins": bins, "thresholds": thresholds, "verdicts": verdicts, "evidence": evidence}

    @staticmethod
    def state_digest(state):
        """sha256 hex of the canonical JSON of the state a row was judged on; binds a row to its input."""
        return jv._digest(state)

    @staticmethod
    def _part(rows):
        """Receipt summary of one set of rows: what was judged, how much was answered, and by which batches."""
        receipt = getattr(rows, "receipt", None) or {}
        answered = receipt.get("answered_ids")
        if not isinstance(answered, list):
            answered = [row.get("id") for row in rows if "error" not in row]
        if receipt.get("assembly") == "spliced":
            return list(receipt.get("parts") or [])
        return [{"intent": receipt.get("intent"), "qhash": receipt.get("qhash"),
                 "units": len(rows), "answered": len(answered),
                 "batch_ids": list(receipt.get("batch_ids") or [])}]

    @classmethod
    def splice(cls, base_rows, patch_rows):
        """Replace base rows by id with re-judged patch rows. The result is never a single full sweep: its
        receipt says spliced, lists both parts and the replaced ids, and patch rows are marked spliced-patch.
        It is complete only when every id is answered and neither part reported coverage errors or exclusions."""
        parts_in = []
        for label, rows in (("base", base_rows), ("patch", patch_rows)):
            receipt = getattr(rows, "receipt", None)
            if not isinstance(receipt, dict):
                raise ValueError(f"splice needs {label} rows that carry their run receipt")
            ids = [row.get("id") for row in rows]
            if len(set(ids)) != len(ids):
                raise ValueError(f"splice refuses duplicate ids in {label} rows")
            parts_in.append((receipt, ids))
        (base_receipt, base_ids), (patch_receipt, patch_ids) = parts_in
        digests = {base_receipt.get("qhash"), patch_receipt.get("qhash"),
                   *(row.get("qhash") for row in base_rows), *(row.get("qhash") for row in patch_rows)}
        if len(digests) != 1 or None in digests:
            raise ValueError(f"splice refuses different question hashes: {sorted(map(str, digests))}")
        digest = digests.pop()
        base_set = set(base_ids)
        unknown = [uid for uid in patch_ids if uid not in base_set]
        if unknown:
            raise ValueError(f"splice refuses patch ids absent from the base rows: {unknown[:10]}")
        patch = {row["id"]: row for row in patch_rows}
        rows = _JvRows()
        for row in base_rows:
            if row["id"] in patch:
                replacement = dict(patch[row["id"]])
                replacement["assembly"] = "spliced-patch"
                rows.append(replacement)
            else:
                rows.append(dict(row))
        errors = [row["id"] for row in rows if "error" in row]
        coverage_errors = [*(base_receipt.get("coverage_errors") or []), *(patch_receipt.get("coverage_errors") or [])]
        exclusions = {**(base_receipt.get("exclusions") or {}), **(patch_receipt.get("exclusions") or {})}
        complete = not errors and not coverage_errors and not exclusions
        served, served_unknown = cls._served(rows)
        for row in rows:
            row["coverage_complete"] = complete
            row["coverage_errors"] = list(coverage_errors)
        parts = [*cls._part(base_rows), *cls._part(patch_rows)]
        ids = [row["id"] for row in rows]
        batch_ids = [batch for part in parts for batch in part["batch_ids"]]
        receipt = {
            "schema": 2, "strategy": "spliced", "assembly": "spliced", "parts": parts,
            "intent": f"spliced: {len(patch)} of {len(rows)} re-judged",
            "replaced_ids": [uid for uid in base_ids if uid in patch],
            "units": len(rows), "ok": len(rows) - len(errors), "errors": len(errors),
            "qhash": digest, "question_hash": digest, "unit_ids": ids,
            "original_ids": list(base_receipt.get("original_ids") or ids),
            "answered_ids": [uid for uid in ids if uid not in errors], "error_ids": errors,
            "error_details": {row["id"]: row["error"] for row in rows if "error" in row},
            "excluded_ids": [], "exclusions": exclusions, "unresolved_ids": [],
            "coverage_errors": coverage_errors, "complete": complete,
            "batch_ids": batch_ids,
            "models": dict(_jv_collections.Counter(str(row["model"]) for row in rows if row.get("model"))),
            "served_models": served, "served_unknown": served_unknown,
            "comparable": len(served) == 1 and not served_unknown,
            "host_counts": {"answered": len(ids) - len(errors), "planned": None, "host_done": None,
                            "host_total": None, "reconciled": False},
            "cost": sum((part.get("cost") or 0.0) for part in (base_receipt, patch_receipt)),
            "cost_kind": "batch_estimate", "billing": "estimate_unverified",
            "cost_known": all(part.get("cost_known") is True for part in (base_receipt, patch_receipt)),
            "provenance_complete": all(not cls._provenance_unknown(row) for row in rows if "error" not in row),
        }
        rows.receipt = receipt
        print(f"spliced: {len(patch)} of {len(rows)} re-judged · {len(errors)} errors · "
              f"complete={complete} · qhash={digest}")
        return rows

    @classmethod
    def comparable(cls, rows_a, rows_b):
        """Whether two row sets may be compared as one measurement: the same question hash and the same single,
        known served model. A row with no served model makes the pair not comparable."""
        reasons, served = [], []
        for label, rows in (("A", rows_a), ("B", rows_b)):
            if not len(rows):
                reasons.append(f"{label}: no rows")
            receipt = getattr(rows, "receipt", None) or {}
            digests = {row.get("qhash") for row in rows} | ({receipt["qhash"]} if receipt.get("qhash") else set())
            if len(digests) > 1:
                reasons.append(f"{label}: mixed question hashes {sorted(map(str, digests))}")
            served.append((digests, cls._served(rows)))
        (digests_a, (models_a, unknown_a)), (digests_b, (models_b, unknown_b)) = served
        if len(digests_a) == 1 and len(digests_b) == 1 and digests_a != digests_b:
            reasons.append(f"question hashes differ: {sorted(digests_a)} vs {sorted(digests_b)}")
        for label, models, unknown in (("A", models_a, unknown_a), ("B", models_b, unknown_b)):
            if unknown:
                reasons.append(f"{label}: {unknown} answered row(s) have an unknown served model")
            if len(models) > 1:
                reasons.append(f"{label}: several served models {sorted(models)}")
        if models_a and models_b and set(models_a) != set(models_b):
            reasons.append(f"served models differ: {sorted(models_a)} vs {sorted(models_b)}")
        return {"comparable": not reasons, "reasons": reasons}

    @staticmethod
    def _cell(value):
        if isinstance(value, float):
            value = round(value, 3)
        elif isinstance(value, (list, tuple)):
            value = "; ".join(str(item) for item in value)
        text = "" if value is None else str(value)
        return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")

    @classmethod
    def export(cls, rows, path=None, *, Q, review=None, rule_flags=None, format="md"):
        """One table or JSONL record per row. judge_flag comes from jv.flag; rule_flags is the caller's own
        regex/script findings in a separate column and is never merged. review_status is unreviewed unless a
        durable review path is given. Returns the text and writes it when path is given."""
        if format not in ("md", "jsonl"):
            raise ValueError("format must be 'md' or 'jsonl'")
        rule_flags = {} if rule_flags is None else rule_flags
        if (not isinstance(rule_flags, dict)
                or any(not isinstance(key, str) or not isinstance(value, (list, tuple, str))
                       for key, value in rule_flags.items())):
            raise ValueError("rule_flags must map string ids to a reason or list of reasons")
        known = {row["id"] for row in rows}
        absent = sorted(set(rule_flags) - known)
        if absent:
            raise ValueError(f"rule_flags name ids that are not rows: {absent[:10]}")
        judged = {row["id"]: row["why"] for row in cls.flag(rows)}
        reviewed = {}
        if review is not None:
            for entry in cls.review_items(review, Q=Q):
                reviewed.setdefault(entry["unit"], []).append(entry)

        def status(identifier):
            entries = reviewed.get(identifier)
            if not entries:
                return "unreviewed"
            if any(entry["stale"] for entry in entries):
                return "stale"
            verdicts = {entry["verdict"] or "pending" for entry in entries}
            return verdicts.pop() if len(verdicts) == 1 else "mixed"

        answers = [name for qid, question in Q.items() for name in cls._answer_columns(qid, question)
                   if not name.endswith((".dist", ".legend"))]
        records = []
        for row in rows:
            identifier = row["id"]
            reasons = rule_flags.get(identifier, [])
            digest = row.get("state_sha256")
            records.append({
                "id": identifier, **{name: row.get(name) for name in answers}, "error": row.get("error"),
                "judge_flag": judged.get(identifier, []),
                "rule_flags": [reasons] if isinstance(reasons, str) else list(reasons),
                "review_status": status(identifier), "assembly": row.get("assembly"),
                "judged_at": row.get("judged_at"), "state_sha256": digest[:12] if digest else None})
        if format == "jsonl":
            text = "".join(_jv_json.dumps(record, ensure_ascii=False) + "\n" for record in records)
        else:
            columns = list(records[0]) if records else ["id"]
            lines = ["| " + " | ".join(cls._cell(column) for column in columns) + " |",
                     "| " + " | ".join("---" for _ in columns) + " |"]
            lines += ["| " + " | ".join(cls._cell(record[column]) for column in columns) + " |"
                      for record in records]
            text = "\n".join(lines) + "\n"
        if path is not None:
            with open(path, "w", encoding="utf-8") as output:
                output.write(text)
        return text

    @classmethod
    def receipt_md(cls, rows):
        """Markdown block stating what the receipt supports and what it does not."""
        receipt = getattr(rows, "receipt", None)
        if not isinstance(receipt, dict):
            return "- No run receipt: coverage, served models and cost are unknown.\n- Judge estimates, not source-confirmed.\n"
        answered = len(receipt.get("answered_ids") or [])
        out = [f"- Units: {receipt.get('units', len(rows))}; answered: {answered}; "
               f"errors: {receipt.get('errors', sum('error' in row for row in rows))}."]
        if receipt.get("assembly") == "spliced":
            replaced = receipt.get("replaced_ids") or []
            out.append(f"- Assembly: spliced: {len(replaced)} of {receipt.get('units', len(rows))} re-judged; "
                       "not a single full sweep.")
            for part in receipt.get("parts") or []:
                out.append(f"  - part {part.get('intent')!r}: {part.get('units')} units, "
                           f"{part.get('answered')} answered, batches {part.get('batch_ids')}.")
        else:
            out.append(f"- Assembly: {receipt.get('assembly', 'single')} run, intent {receipt.get('intent')!r}.")
        out.append(f"- Complete coverage: {receipt.get('complete') is True}.")
        served = receipt.get("served_models") or {}
        unknown = receipt.get("served_unknown") or 0
        out.append(f"- Served models: {served or 'none'}"
                   + (f" (+{unknown} answered row(s) with unknown served model)" if unknown else "")
                   + f"; comparable across rows: {receipt.get('comparable') is True}.")
        cost = receipt.get("cost")
        out.append("- Cost: " + (f"{cost:.6g} USD batch estimate, unverified (priced by host)"
                                  if isinstance(cost, (int, float)) else "unknown")
                   + (f"; cost observed for every batch: {receipt.get('cost_known') is True}."))
        counts = receipt.get("host_counts")
        if isinstance(counts, dict) and counts.get("host_done") is not None:
            out.append(f"- Host counts: answered {counts.get('answered')}, host reported "
                       f"{counts.get('host_done')} of {counts.get('host_total')} done; "
                       f"reconciled: {counts.get('reconciled') is True}.")
        else:
            out.append("- Host counts: not observed, so answered units are not reconciled with the host.")
        unrouted = sum(1 for row in rows if "error" not in row and cls._provenance_unknown(row))
        out.append("- Provenance: " + ("route, fallback and cache reported for every answered row."
                                       if not unrouted else
                                       f"route, fallback or cache unknown for {unrouted} answered row(s) "
                                       "(stock hosts report only the served model)."))
        out.append("- These are judge estimates, not source-confirmed.")
        return "\n".join(out) + "\n"

    @staticmethod
    def save(rows, path="/tmp/jevify-rows.jsonl"):
        """Write a receipt header followed by rows, retaining empty-run coverage evidence."""
        with open(path, "w", encoding="utf-8") as output:
            output.write(_jv_json.dumps({"jevify_receipt": getattr(rows, "receipt", None)},
                                       ensure_ascii=False) + "\n")
            for row in rows:
                output.write(_jv_json.dumps(row, ensure_ascii=False) + "\n")
        return path

    @staticmethod
    def load(path):
        """Restore rows only if their exact inventory and result partition match the receipt."""
        with open(path, encoding="utf-8") as source:
            records = (_jv_json.loads(line) for line in source if line.strip())
            header = next(records, None)
            if not isinstance(header, dict) or set(header) != {"jevify_receipt"}:
                raise ValueError("saved rows require a jevify_receipt header")
            receipt = header["jevify_receipt"]
            if receipt is not None and not isinstance(receipt, dict):
                raise ValueError("saved run receipt must be an object or null")
            rows = _JvRows(records, receipt=receipt)
        if receipt is not None:
            expected = {}
            for field in ("unit_ids", "answered_ids", "error_ids"):
                ids = receipt.get(field)
                if (not isinstance(ids, list) or any(not isinstance(uid, str) for uid in ids)
                        or len(set(ids)) != len(ids)):
                    raise ValueError(f"saved receipt {field} must contain unique string IDs")
                expected[field] = set(ids)
            if (expected["answered_ids"] & expected["error_ids"]
                    or expected["answered_ids"] | expected["error_ids"] != expected["unit_ids"]):
                raise ValueError("saved receipt success/error IDs do not partition its unit inventory")
            seen, answered, errors = set(), set(), set()
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                    raise ValueError("saved rows must have string IDs")
                uid = row["id"]
                if uid in seen:
                    raise ValueError(f"duplicate saved row ID: {uid!r}")
                seen.add(uid)
                (errors if "error" in row else answered).add(uid)
            if seen != expected["unit_ids"]:
                raise ValueError("saved row inventory does not match bound receipt")
            if answered != expected["answered_ids"] or errors != expected["error_ids"]:
                raise ValueError("saved row success/error partition does not match bound receipt")
        return rows

    @classmethod
    def delete_spans(cls, units, ids, *, root=".", dry_run=True, manifest=None, review=None, Q=None):
        """Delete the spans of `ids`. With `manifest`, each target must match its extraction record and the
        current source snapshot. With `review` (a durable review path) and `Q`, the review must be complete,
        current, and confirmed for every target; its lock is held through the write. Failures raise before any
        file is written, including in dry runs. A review grants no edit authority beyond this explicit call."""
        if review is not None and (manifest is None or Q is None):
            raise ValueError("review requires the extraction manifest and question set it adjudicates")
        bundle = None if review is None else cls._open_review(review)
        try:
            return cls._delete_units(units, ids, root=root, dry_run=dry_run, manifest=manifest,
                                     bundle=bundle, Q=Q)
        finally:
            if bundle is not None:
                bundle.release()

    @classmethod
    def _delete_units(cls, units, ids, *, root, dry_run, manifest, bundle, Q):
        root_path = _jv_pathlib.Path(root).resolve()
        selected = set(ids)
        missing = selected - units.keys()
        if missing:
            print(f"warning: {len(missing)} ids missing from units: {sorted(missing)[:10]}")
        if manifest is not None:
            cls._guard_deletion(units, sorted(selected & units.keys()), manifest, bundle, Q, root)
        per_file = _jv_collections.defaultdict(list)
        for uid in selected & units.keys():
            unit = units[uid]
            rel = unit["file"]
            if _jv_pathlib.Path(rel).is_absolute():
                raise ValueError(f"absolute unit path: {rel}")
            path = (root_path / rel).resolve()
            if not path.is_relative_to(root_path) or (root_path / rel).is_symlink():
                raise ValueError(f"unit path escapes root or is a symlink: {rel}")
            start, end = unit["line"], unit["end_line"]
            if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
                raise ValueError(f"invalid 1-based span for {uid}")
            per_file[rel].append((start, end))
        prepared = {}
        for rel, spans in sorted(per_file.items()):
            path = root_path / rel
            with path.open("r", encoding="utf-8", newline="") as source:
                lines = source.read().splitlines(keepends=True)
            merged = []
            for start, end in sorted(spans):
                if end > len(lines):
                    raise ValueError(f"span exceeds file for {rel}: {start}-{end}")
                if merged and start <= merged[-1][1] + 1:
                    merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
                else:
                    merged.append((start, end))
            removed = 0
            for start, end in reversed(merged):
                lo, hi = start - 1, end
                if hi < len(lines) and not lines[hi].strip():
                    hi += 1
                removed += hi - lo
                del lines[lo:hi]
            prepared[rel] = (path, "".join(lines), removed, len(spans))
        results = {}
        for rel, (path, content, removed, count) in prepared.items():
            results[rel] = removed
            print(f"{rel}: -{removed} lines ({count} units)")
            if not dry_run:
                with path.open("w", encoding="utf-8", newline="") as output:
                    output.write(content)
        print(f"total: -{sum(results.values())} lines across {len(results)} files"
              + (" (dry run)" if dry_run else ""))
        return results

    @classmethod
    def report(cls, rows, Q, *, title="jevify"):
        errors = sum("error" in row for row in rows)
        fallbacks = sum("error" not in row and row.get("fallback") is True for row in rows)
        unknown = sum("error" not in row and cls._provenance_unknown(row) for row in rows)
        hashes = sorted({row.get("qhash", "<missing>") for row in rows})
        models = _jv_collections.Counter(str(row.get("model")) for row in rows if "error" not in row)
        out = [f"# {title}", "", f"- Units: {len(rows)}; ok: {len(rows) - errors}; "
               f"errors: {errors}; fallback rows: {fallbacks}; provenance unknown rows: {unknown}.",
               f"- Question hashes: {', '.join(hashes) if hashes else cls.qhash(Q)}.",
               f"- Models: {dict(models)}."]
        for qid, question in Q.items():
            good = [row[qid] for row in rows if "error" not in row and qid in row]
            if question["type"] == "choice":
                counts = _jv_collections.Counter(good)
                out.append(f"- {qid}: " + ", ".join(
                    f"{label}={counts[label]}" for label in question["criteria"]))
            elif question["type"] == "bool":
                out.append(f"- {qid}: P(yes) ≥ 0.5 in {sum(value >= 0.5 for value in good)}/{len(good)}.")
            elif question["type"] == "score":
                out.append(f"- {qid}: mean weighted level index "
                           + (f"{sum(good) / len(good):.2f}" if good else "n/a") + ".")
        run = getattr(rows, "receipt", None)
        if run is not None:
            elapsed = run.get("elapsed")
            out.append(f"- Bound run: {run['units']} units, {run['ok']} ok, "
                       f"{run['errors']} errors, models={run['models']}, "
                       f"batch cost estimate (unverified, priced by host)={run['cost']:.6g}, "
                       f"elapsed={'unknown' if elapsed is None else f'{elapsed:.1f}s'}.")
            out.append(f"- Assembly: {run.get('assembly', 'single')}"
                       + (f" ({len(run.get('replaced_ids') or [])} of {run['units']} re-judged; "
                          "not a single full sweep)" if run.get("assembly") == "spliced" else "")
                       + f"; served models={run.get('served_models', 'unknown')}; "
                       f"comparable={run.get('comparable', 'unknown')}.")
            out.append(f"- Run coverage complete: {run['complete']}; "
                       f"cache counts={run.get('cache_counts', 'unknown')}; host batches={run['batch_ids']}.")
            out.append(f"- Original units: {len(run['original_ids'])}; "
                       f"submitted units: {len(run['unit_ids'])}; excluded units: {len(run['excluded_ids'])}.")
            if run["exclusions"]:
                out.append(f"- Excluded original IDs and reasons: {run['exclusions']}.")
            if run["coverage_errors"]:
                out.append(f"- Coverage errors: {run['coverage_errors']}.")
        else:
            out.append("- Run receipt unavailable; no run-level coverage claim.")
        return "\n".join(out) + "\n"


print("jevify: jv ready · " + " ".join(sorted(
    name for name in vars(jv) if not name.startswith("_") and callable(getattr(jv, name)))))
