"""Deterministic extraction and review helpers for rubric-first Jev batches.

Load into the persistent Python eval kernel with %load skill://jevify/jevify.py.
The kernel supplies judge_batch, completion, and wait when their methods are called.
"""

import base64 as _jv_base64
import collections as _jv_collections
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
import subprocess as _jv_subprocess
import sys as _jv_sys
import time as _jv_time


class jv:
    CAP = 12_000
    MAX_STATE = 100_000
    SEED = 11
    EDGES = (0.0, 0.1, 0.3, 0.5, 0.7, 0.9, 1.01)
    stats = {"states": {}, "runs": []}
    frozen = None

    @staticmethod
    def choice(instructions, criteria=None, **labels):
        options = dict(criteria or {})
        for label, description in labels.items():
            if label in options:
                raise ValueError(f"duplicate choice label: {label}")
            options[label] = description
        if len(options) < 2:
            raise ValueError("a choice requires at least two labels")
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

    @staticmethod
    def qhash(Q):
        return _jv_hashlib.sha256(_jv_json.dumps(Q, sort_keys=True).encode("utf-8")).hexdigest()[:12]

    @classmethod
    def freeze(cls, Q, path="/tmp/jevify-questions.json"):
        digest = cls.qhash(Q)
        with open(path, "w", encoding="utf-8") as output:
            _jv_json.dump(Q, output, indent=2, sort_keys=True, ensure_ascii=False)
            output.write("\n")
        cls.frozen = digest
        return digest

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

    @classmethod
    def states(cls, units, render=None, *, cap=None):
        ceiling = cls.CAP if cap is None else cap
        if ceiling <= 0:
            raise ValueError("cap must be positive")
        built, dropped, truncated, oversized = {}, 0, 0, []
        for identifier, unit in units.items():
            value, count = cls._cap(render(unit) if render else unit, ceiling)
            truncated += count
            if cls._nonempty(value):
                built[identifier] = value
                if len(_jv_json.dumps(value, ensure_ascii=False, default=str)) > cls.MAX_STATE:
                    oversized.append(identifier)
            else:
                dropped += 1
        cls.stats["states"] = {"input": len(units), "built": len(built), "dropped": dropped,
                               "truncated": truncated, "cap": ceiling, "oversized": len(oversized)}
        print(f"states: {len(built)} built · {dropped} empty dropped · {truncated} truncated (cap {ceiling} chars)")
        if oversized:
            print(f"warning: {len(oversized)} states exceed MAX_STATE={cls.MAX_STATE} (fallback risk): "
                  + ", ".join(map(str, oversized)))
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
    def group(cls, units, *, by, k=8, render=None, shared=None, cap=None):
        if k < 1:
            raise ValueError("k must be positive")
        ceiling = cls.CAP if cap is None else cap
        if ceiling <= 0:
            raise ValueError("cap must be positive")
        buckets = {}
        for identifier, unit in units.items():
            value = render(unit) if render else unit
            if cls._nonempty(value):
                buckets.setdefault(by(unit), []).append((identifier, value))
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
        bounded = cls.states(groups, cap=ceiling)
        return bounded, {gid: slots[gid] for gid in bounded}

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
    async def _collect(cls, payload, questions, required, *, intent, concurrency, retries, deadline):
        found, problem, cost = {}, "no result delivered", 0.0
        batch = None
        try:
            batch = globals()["judge_batch"](payload, questions, concurrency=concurrency,
                                               retries=retries, intent=intent)
            while True:
                remaining = deadline - _jv_time.monotonic()
                if remaining <= 0:
                    problem = "judge drain timed out"
                    break
                try:
                    async for identifier, item in batch.drain_iter(min(60, remaining)):
                        if identifier in payload:
                            found[identifier] = item
                except Exception as error:
                    problem = f"judge drain failed: {error}"
                    break
                status = batch.status()
                raw_cost = status.get("cost", 0)
                if isinstance(raw_cost, (int, float)):
                    cost = float(raw_cost)
                print(f"jevify {intent}: {status.get('done', len(found))}/{status.get('total', len(payload))} "
                      f"done · {status.get('failed', 0)} failed · primary-priced cost={cost:.6g} "
                      f"· elapsed={status.get('elapsedS', 0):.1f}s")
                if status.get("done", 0) >= len(payload) or not status.get("running", False):
                    break
            status = batch.status()
            raw_cost = status.get("cost", 0)
            if isinstance(raw_cost, (int, float)):
                cost = float(raw_cost)
        except Exception as error:
            problem = f"judge submission failed: {error}"
        finally:
            if batch is not None:
                try:
                    if len(found) < len(payload):
                        batch.cancel()
                finally:
                    batch.close()
        return found, problem, cost

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
            score = float(answer["score"])
            if not _jv_math.isfinite(score) or not 0 <= score <= len(question["criteria"]) - 1:
                raise ValueError(f"invalid {qid} weighted level index")
            row[qid] = score
            row[f"{qid}.conf"] = cls._fraction(answer["confidence"])
        else:
            raise ValueError(f"unknown question type: {kind}")

    @classmethod
    def _flat_row(cls, identifier, Q, item, digest, *, slot=None, group=None, failure=None):
        model = getattr(item, "model", None) if item is not None else None
        row = {"id": identifier, "model": model, "qhash": digest}
        if group is not None:
            row["group"] = group
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
            row = {"id": identifier, "model": model, "qhash": digest}
            if group is not None:
                row["group"] = group
            row["error"] = f"invalid judge answer: {error}"
        return row

    @classmethod
    async def run(cls, states, Q, *, intent, concurrency=32, retries=1, timeout=1800, slots=None):
        if not isinstance(intent, str) or not intent.strip():
            raise ValueError("run requires a non-empty intent")
        if timeout <= 0 or concurrency < 1:
            raise ValueError("timeout and concurrency must be positive")
        digest = cls.qhash(Q)
        if cls.frozen is not None and cls.frozen != digest:
            print(f"WARNING: question hash {digest} differs from last frozen {cls.frozen}: in the same "
                  "sweep the wording drifted, so re-judge every unit; a new sweep should freeze its own Q")
        if slots is not None:
            if set(states) != set(slots) or any(not ids for ids in slots.values()):
                raise ValueError("slots must name every nonempty group state exactly once")
            questions = cls.slot_questions(Q, max(map(len, slots.values()))) if slots else {}
            required = {gid: [f"{qid}@s{i}" for i in range(len(ids)) for qid in Q]
                        for gid, ids in slots.items()}
        else:
            questions = Q
            required = {identifier: list(Q) for identifier in states}
        if states and not questions:
            raise ValueError("run requires questions")
        start = _jv_time.monotonic()
        deadline = start + timeout
        first, first_problem, first_cost = {}, "no result delivered", 0.0
        second, second_problem, second_cost = {}, "no result delivered", 0.0
        retried = set()
        if states:
            first, first_problem, first_cost = await cls._collect(
                states, questions, required, intent=intent, concurrency=concurrency,
                retries=retries, deadline=deadline)
            pending = {key: state for key, state in states.items()
                       if key not in first or not cls._item_good(first[key], questions, required[key])}
            if pending and _jv_time.monotonic() < deadline:
                retried = set(pending)
                second, second_problem, second_cost = await cls._collect(
                    pending, questions, required, intent=intent + " (retry)",
                    concurrency=concurrency, retries=retries, deadline=deadline)
        rows = []
        for key in states:
            item = second.get(key) if key in retried else first.get(key)
            issue = second_problem if key in retried else first_problem
            failure = None if item is not None else issue
            if item is not None and not cls._item_good(item, questions, required[key]):
                failure = str(getattr(item, "error", None) or "invalid judge answer")
            if slots is None:
                rows.append(cls._flat_row(key, Q, item, digest, failure=failure))
            else:
                for index, identifier in enumerate(slots[key]):
                    rows.append(cls._flat_row(identifier, Q, item, digest, slot=index,
                                              group=key, failure=failure))
        models = _jv_collections.Counter(str(row["model"]) for row in rows if row["model"] is not None)
        errors = sum("error" in row for row in rows)
        elapsed = _jv_time.monotonic() - start
        cost = first_cost + second_cost
        summary = {"intent": intent, "units": len(rows), "ok": len(rows) - errors,
                   "errors": errors, "models": dict(models), "cost": cost,
                   "elapsed": elapsed, "qhash": digest}
        cls.stats["runs"].append(summary)
        print(f"jevify: {len(rows)} units · {len(rows) - errors} ok · {errors} errors · "
              f"models={dict(models)} · primary-priced cost={cost:.6g} · "
              f"elapsed={elapsed:.1f}s · qhash={digest}")
        return rows

    @staticmethod
    def _primary(rows):
        models = _jv_collections.Counter(row.get("model") for row in rows if "error" not in row)
        return models.most_common(1)[0][0] if models else None

    @classmethod
    def flag(cls, rows, rule=None, *, primary=None):
        primary = cls._primary(rows) if primary is None else primary
        result, counts = [], _jv_collections.Counter()
        for row in rows:
            why = []
            if "error" in row:
                why.append("error")
            else:
                if row.get("model") != primary:
                    why.append(f"fallback:{row.get('model')}")
                if rule is not None:
                    reason = rule(row)
                    if reason:
                        why.append(reason if isinstance(reason, str) else "rule")
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
        metadata = {"id", "model", "qhash", "group", "why"}
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
    def tally(cls, rows, key, *, edges=None):
        values = [row[key] for row in rows if key in row and "error" not in row]
        boundaries = tuple(cls.EDGES if edges is None else edges)
        if values and all(isinstance(value, (int, float)) for value in values):
            if len(boundaries) < 2 or any(a >= b for a, b in zip(boundaries, boundaries[1:])):
                raise ValueError("histogram edges must be strictly increasing")
            result = {f"[{lo:g}, {hi:g})": 0 for lo, hi in zip(boundaries, boundaries[1:])}
            for value in values:
                for lo, hi in zip(boundaries, boundaries[1:]):
                    if lo <= value < hi:
                        result[f"[{lo:g}, {hi:g})"] += 1
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
        question = Q[qid]
        if question["type"] != "choice" or label not in question["criteria"]:
            raise ValueError("calibrate requires a choice question and one of its labels")
        if per_bin < 0:
            raise ValueError("per_bin must be nonnegative")
        boundaries = tuple(cls.EDGES if edges is None else edges)
        if len(boundaries) < 2 or any(a >= b for a, b in zip(boundaries, boundaries[1:])):
            raise ValueError("calibration edges must be strictly increasing")
        agreeing = {label} if agree is None else {agree} if isinstance(agree, str) else set(agree)
        rng = _jv_random.Random(cls.SEED if seed is None else seed)
        key = f"{qid}.{label}"
        pools = [[] for _ in range(len(boundaries) - 1)]
        seen = set()
        for row in rows:
            uid = row["id"]
            if "error" in row or uid in seen or uid not in states or key not in row:
                continue
            seen.add(uid)
            p = row[key]
            for index, (lo, hi) in enumerate(zip(boundaries, boundaries[1:])):
                if lo <= p < hi:
                    pools[index].append((uid, p))
                    break
        chosen = [entry for pool in pools for entry in rng.sample(pool, min(per_bin, len(pool)))]
        system = "\n\n".join(part for part in (
            rubric, question["instructions"],
            "Labels and criteria: " + _jv_json.dumps(question["criteria"], ensure_ascii=False)
        ) if part)
        schema = {"type": "object", "properties": {
            "verdict": {"enum": list(question["criteria"])}, "reason": {"type": "string"}},
            "required": ["verdict", "reason"]}
        verdicts = {}
        for start in range(0, len(chosen), 16):
            wave = chosen[start:start + 16]
            handles, submitted = [], []
            for uid, p in wave:
                try:
                    handles.append(globals()["completion"](
                        _jv_json.dumps(states[uid], ensure_ascii=False, default=str),
                        model=model, system=system, schema=schema))
                    submitted.append((uid, p))
                except Exception as error:
                    verdicts[uid] = {"slow": "ERR", "reason": str(error)[:200], "p": p}
            if not handles:
                continue
            try:
                outputs = globals()["wait"](handles, raise_errors=False)
            except Exception as error:
                outputs = [error] * len(handles)
            for (uid, p), answer in zip(submitted, outputs):
                if isinstance(answer, dict) and answer.get("verdict") in question["criteria"]:
                    verdicts[uid] = {"slow": answer["verdict"],
                                     "reason": str(answer.get("reason", "")), "p": p}
                else:
                    verdicts[uid] = {"slow": "ERR", "reason": str(answer)[:200], "p": p}
            for uid, p in submitted[len(outputs):]:
                verdicts[uid] = {"slow": "ERR", "reason": "no slow result", "p": p}
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
        return {"bins": bins, "thresholds": thresholds, "verdicts": verdicts}

    @staticmethod
    def save(rows, path="/tmp/jevify-rows.jsonl"):
        with open(path, "w", encoding="utf-8") as output:
            for row in rows:
                output.write(_jv_json.dumps(row, ensure_ascii=False) + "\n")
        return path

    @staticmethod
    def load(path):
        with open(path, encoding="utf-8") as source:
            return [_jv_json.loads(line) for line in source if line.strip()]

    @staticmethod
    def delete_spans(units, ids, *, root=".", dry_run=True):
        root_path = _jv_pathlib.Path(root).resolve()
        selected = set(ids)
        missing = selected - units.keys()
        if missing:
            print(f"warning: {len(missing)} ids missing from units: {sorted(missing)[:10]}")
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
        primary = cls._primary(rows)
        fallbacks = sum("error" not in row and row.get("model") != primary for row in rows)
        hashes = sorted({row.get("qhash", "<missing>") for row in rows})
        models = _jv_collections.Counter(str(row.get("model")) for row in rows if "error" not in row)
        out = [f"# {title}", "", f"- Units: {len(rows)}; ok: {len(rows) - errors}; "
               f"errors: {errors}; fallback rows: {fallbacks}.",
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
        state = cls.stats.get("states", {})
        if state:
            out.append(f"- Last states: {state.get('built', 0)} built, "
                       f"{state.get('dropped', 0)} dropped, "
                       f"{state.get('truncated', 0)} truncated (cap {state.get('cap')}).")
        if cls.stats.get("runs"):
            run = cls.stats["runs"][-1]
            out.append(f"- Last run: {run['units']} units, {run['ok']} ok, "
                       f"{run['errors']} errors, models={run['models']}, "
                       f"primary-priced cost={run['cost']:.6g}, elapsed={run['elapsed']:.1f}s.")
        return "\n".join(out) + "\n"


print("jevify: jv ready · " + " ".join(sorted(
    name for name in vars(jv) if not name.startswith("_") and callable(getattr(jv, name)))))
