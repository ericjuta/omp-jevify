import { createHash, randomUUID } from "node:crypto";
import { constants } from "node:fs";
import { link, mkdir, open, realpath, unlink, type FileHandle } from "node:fs/promises";
import path from "node:path";

export const MAX_PLAN_BYTES = 16 * 1024 * 1024;
// Linux O_PATH pins traversable ancestors without requiring directory-list permission.
// Node exposes O_DIRECTORY/O_NOFOLLOW but not this Linux flag.
const O_PATH = 0x200000;
export const HARD_LIMITS = {
  max_sources: 256, max_records: 10_000, max_fields: 64,
  max_source_bytes: 4 * 1024 * 1024, max_total_source_bytes: 32 * 1024 * 1024,
  max_result_bytes: 16 * 1024 * 1024, max_value_bytes: 64 * 1024,
  max_inventory_files: 10_000, max_metadata_bytes: 64 * 1024,
  max_state_bytes: 4 * 1024 * 1024, max_total_state_bytes: 32 * 1024 * 1024,
  max_requests: 32, max_input_bytes: 1024 * 1024, max_request_bytes: 128 * 1024,
  max_response_bytes: 256 * 1024, deadline_ms: 10_000,
} as const;
export type Limits = { -readonly [K in keyof typeof HARD_LIMITS]: number };
export type JsonObject = Record<string, unknown>;
export interface Source {
  id: string; role: string; sha256: string; byte_length: number; codepoint_length: number;
  snapshot: string; classification: "public" | "private" | "secret"; transport_admitted: boolean;
  subject: string | null; version: string | null;
}
export interface EvidenceRecord {
  id: string; source_id: string; dependencies: string[]; required_fields: string[];
  required_questions: string[]; subject: string | null; version: string | null; facts: JsonObject;
}
export interface Field { description: string; source_roles: string[] }
export interface Plan {
  schema: "jevify-evidence/1"; plan_digest: string; root: string; artifact_root: string;
  sources: Record<string, Source>; records: Record<string, EvidenceRecord>;
  fields: Record<string, Field>; limits: Limits;
}
export type FieldStatus = "candidate" | "missing" | "ambiguous" | "rejected" | "error";
export interface Observation {
  status: FieldStatus; value: string | null; source_id: string | null;
  start: number | null; end: number | null; reason: string | null;
}
export interface Result {
  schema: "jevify-evidence-result/1"; plan_digest: string;
  status: "complete" | "partial" | "error";
  records: Record<string, { observations: Record<string, Observation> }>;
  provenance: JsonObject;
}
export class ArtifactError extends Error {
  constructor(cause?: unknown) { super("Evidence artifacts refused: invalid, changed, unsafe or unavailable", { cause }); }
}
export const hash = (bytes: string | Uint8Array): string => createHash("sha256").update(bytes).digest("hex");
export const object = (value: unknown): JsonObject => {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new ArtifactError();
  return value as JsonObject;
};
const fail = (): never => { throw new ArtifactError(); };
const text = (value: unknown): string => typeof value === "string" && value.length > 0 && !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(value) ? value : fail();
const digest = (value: unknown): string => typeof value === "string" && /^[a-f0-9]{64}$/.test(value) ? value : fail();
const strings = (value: unknown, cap: number): string[] => {
  if (!Array.isArray(value) || value.length > cap) return fail();
  const values = value.map(text);
  if (new Set(values).size !== values.length) return fail();
  return values;
};
const nullable = (value: unknown): string | null => value === null ? null : text(value);
const integer = (value: unknown, max: number, min = 0): number => typeof value === "number" && Number.isSafeInteger(value) && value >= min && value <= max ? value : fail();
const roles: Record<string, true> = { original_comment: true, current_code: true, base_code: true, governing_contract: true, helper: true, consumer: true, external_history: true };
const own = (value: object, key: string): boolean => Object.hasOwn(value, key);

/** Python's sort_keys order is Unicode code point order, not JavaScript's UTF-16 order. */
function compareKeys(a: string, b: string): number {
  const left = Array.from(a, char => char.codePointAt(0)!);
  const right = Array.from(b, char => char.codePointAt(0)!);
  for (let i = 0; i < Math.min(left.length, right.length); i++) if (left[i] !== right[i]) return left[i]! - right[i]!;
  return left.length - right.length;
}
/** The frozen plan permits safe integer numbers only, including deterministic facts. */
export function canonical(value: unknown): string {
  if (value === null || typeof value === "boolean") return JSON.stringify(value);
  if (typeof value === "string") { if (value !== "") text(value); return JSON.stringify(value); }
  if (typeof value === "number") { if (!Number.isSafeInteger(value)) return fail(); return String(value); }
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  const row = object(value);
  return `{${Object.keys(row).sort(compareKeys).map(key => `${canonical(key)}:${canonical(row[key])}`).join(",")}}`;
}
function parsePlan(value: unknown, root: string, expected: string): Plan {
  const data = object(value);
  if (data.schema !== "jevify-evidence/1" || data.plan_digest !== expected || data.artifact_root !== root) return fail();
  const { plan_digest: _digest, ...unsigned } = data;
  if (hash(canonical(unsigned)) !== expected) return fail();
  if (path.join(text(data.root), ".jevify-evidence") !== root) return fail();
  const rawLimits = object(data.limits);
  const limits = {} as Limits;
  for (const key of Object.keys(HARD_LIMITS) as (keyof Limits)[]) limits[key] = integer(rawLimits[key], HARD_LIMITS[key], 1);
  const rawFields = object(data.fields);
  if (Object.keys(rawFields).length > limits.max_fields) return fail();
  const fields: Record<string, Field> = Object.create(null);
  for (const [id, raw] of Object.entries(rawFields)) {
    if (Buffer.byteLength(text(id)) > 256) return fail();
    const field = object(raw);
    const sourceRoles = strings(field.source_roles, Object.keys(roles).length);
    if (sourceRoles.some(role => !own(roles, role))) return fail();
    const description = text(field.description);
    if (Buffer.byteLength(description) > 4096) return fail();
    fields[id] = { description, source_roles: sourceRoles };
  }
  const rawSources = object(data.sources);
  if (Object.keys(rawSources).length > limits.max_sources) return fail();
  const sources: Record<string, Source> = Object.create(null);
  let bytes = 0;
  for (const [id, raw] of Object.entries(rawSources)) {
    if (Buffer.byteLength(text(id)) > 256) return fail();
    const source = object(raw);
    if (source.id !== id || !own(roles, text(source.role))) return fail();
    const sha = digest(source.sha256);
    if (source.snapshot !== path.join(root, "snapshots", `${sha}.utf8`)) return fail();
    if (typeof source.classification !== "string" || !["public", "private", "secret"].includes(source.classification) || typeof source.transport_admitted !== "boolean" || source.transport_admitted !== (source.classification !== "secret") || !source.transport_admitted) return fail();
    const byteLength = integer(source.byte_length, limits.max_source_bytes);
    bytes += byteLength;
    if (bytes > limits.max_total_source_bytes) return fail();
    sources[id] = {
      id: text(id), role: text(source.role), sha256: sha, snapshot: text(source.snapshot),
      byte_length: byteLength, codepoint_length: integer(source.codepoint_length, byteLength),
      classification: source.classification as Source["classification"], transport_admitted: source.transport_admitted,
      subject: nullable(source.subject), version: nullable(source.version),
    };
  }
  const rawRecords = object(data.records);
  if (Object.keys(rawRecords).length > limits.max_records) return fail();
  const records: Record<string, EvidenceRecord> = Object.create(null);
  for (const [id, raw] of Object.entries(rawRecords)) {
    if (Buffer.byteLength(text(id)) > 256) return fail();
    const record = object(raw);
    const sourceId = text(record.source_id);
    const dependencies = strings(record.dependencies, limits.max_sources);
    const required = strings(record.required_fields, limits.max_fields);
    const facts = object(record.facts);
    if (Buffer.byteLength(canonical(facts)) > limits.max_metadata_bytes) return fail();
    if (record.id !== id || !dependencies.includes(sourceId) || dependencies.some(dep => !own(sources, dep)) || required.some(field => !own(fields, field))) return fail();
    records[id] = {
      id: text(id), source_id: sourceId, dependencies, required_fields: required,
      required_questions: strings(record.required_questions, 10_000),
      subject: nullable(record.subject), version: nullable(record.version), facts,
    };
  }
  return { schema: "jevify-evidence/1", plan_digest: expected, root: text(data.root), artifact_root: root, fields, sources, records, limits };
}

/** Bind every read and publication to open directory descriptors, not re-resolved paths.
 * Linux /proc/self/fd gives Node openat-style confinement; other platforms fail closed. */
export class ArtifactStore {
  private readonly dirs = new Map<string, FileHandle>();
  private planBytes = "";
  private constructor(readonly root: string, private readonly signal: AbortSignal) {}
  private check(): void { this.signal.throwIfAborted(); }
  private async openOwned(location: string, flags: number, mode?: number): Promise<FileHandle> {
    this.check();
    const handle = await open(location, flags, mode);
    try { this.check(); return handle; }
    catch (error) { await handle.close(); throw error; }
  }
  private fdPath(handle: FileHandle): string { return `/proc/self/fd/${handle.fd}`; }
  private async directory(name: string): Promise<FileHandle> {
    this.check();
    const previous = this.dirs.get(name);
    if (previous) return previous;
    const root = this.dirs.get("");
    const location = name === "" ? this.root : `${this.fdPath(root!)}/${name}`;
    let handle: FileHandle;
    if (name === "") {
      handle = await this.openOwned("/", O_PATH | constants.O_DIRECTORY | constants.O_NOFOLLOW);
      try {
        for (const part of this.root.split("/").filter(Boolean)) {
          const next = await this.openOwned(`${this.fdPath(handle)}/${part}`, O_PATH | constants.O_DIRECTORY | constants.O_NOFOLLOW);
          await handle.close();
          handle = next;
        }
        const readable = await this.openOwned(`${this.fdPath(handle)}/.`, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
        await handle.close();
        handle = readable;
      } catch (error) { await handle.close(); throw error; }
    } else {
      handle = await this.openOwned(location, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
    }
    this.dirs.set(name, handle);
    return handle;
  }
  static async load(root: string, expected: string, signal: AbortSignal, onPlan?: (plan: Plan) => void): Promise<{ store: ArtifactStore; plan: Plan; texts: Map<string, string> }> {
    signal.throwIfAborted();
    digest(expected);
    if (process.platform !== "linux" || !path.isAbsolute(root) || path.normalize(root) !== root || await realpath(root) !== root) return fail();
    signal.throwIfAborted();
    const store = new ArtifactStore(root, signal);
    try {
      await store.directory("");
      const bytes = await store.read("plans", `${expected}.json`, MAX_PLAN_BYTES);
      store.planBytes = hash(bytes);
      const plan = parsePlan(JSON.parse(new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes)), root, expected);
      onPlan?.(plan);
      store.check();
      const texts = new Map<string, string>();
      for (const source of Object.values(plan.sources)) {
        const raw = await store.read("snapshots", `${source.sha256}.utf8`, plan.limits.max_source_bytes);
        const value = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(raw);
        let codepoints = 0;
        for (const _point of value) codepoints++;
        if (raw.length !== source.byte_length || hash(raw) !== source.sha256 || codepoints !== source.codepoint_length) return fail();
        texts.set(source.id, value);
      }
      await store.assertCurrent(plan);
      return { store, plan, texts };
    } catch (error) { await store.close(); throw new ArtifactError(error); }
  }
  async read(dir: string, name: string, limit: number): Promise<Buffer> {
    if (!/^[a-z]+$/.test(dir) || !/^[a-f0-9-]+\.(json|utf8)$/.test(name)) return fail();
    const handle = await this.openOwned(`${this.fdPath(await this.directory(dir))}/${name}`, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    try {
      const before = await handle.stat();
      this.check();
      if (!before.isFile() || before.size > limit) return fail();
      const bytes = Buffer.alloc(Math.min(before.size + 1, limit + 1));
      let size = 0;
      while (size < bytes.length) {
        const chunk = await handle.read(bytes, size, bytes.length - size, size);
        this.check();
        if (!chunk.bytesRead) break;
        size += chunk.bytesRead;
      }
      const after = await handle.stat();
      this.check();
      if (size !== before.size || before.size !== after.size || before.mtimeMs !== after.mtimeMs || before.ctimeMs !== after.ctimeMs) return fail();
      return bytes.subarray(0, size);
    } finally { await handle.close(); }
  }
  async assertCurrent(plan: Plan): Promise<void> {
    this.check();
    if (await realpath(this.root) !== this.root) return fail();
    this.check();
    for (const [name, handle] of this.dirs) {
      const check = await this.openOwned(name ? path.join(this.root, name) : this.root, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
      try {
        const a = await handle.stat();
        this.check();
        const b = await check.stat();
        this.check();
        if (a.dev !== b.dev || a.ino !== b.ino) return fail();
      } finally { await check.close(); }
    }
    if (hash(await this.read("plans", `${plan.plan_digest}.json`, MAX_PLAN_BYTES)) !== this.planBytes) return fail();
    for (const source of Object.values(plan.sources)) if (hash(await this.read("snapshots", `${source.sha256}.utf8`, plan.limits.max_source_bytes)) !== source.sha256) return fail();
  }
  async ensureOutputDirectories(): Promise<void> {
    for (const dir of ["results", "attempts"]) {
      this.check();
      try { await mkdir(`${this.fdPath(this.dirs.get("")!)}/${dir}`, { mode: 0o700 }); }
      catch (error) { if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error; }
      this.check();
      await this.directory(dir);
    }
  }
  /** Atomic no-clobber link: a pre-existing attempt/result is never overwritten. */
  async publish(dir: "results" | "attempts", name: string, value: unknown, plan: Plan): Promise<string> {
    this.check();
    const bytes = JSON.stringify(value);
    if (Buffer.byteLength(bytes) > plan.limits.max_result_bytes) return fail();
    const base = this.fdPath(await this.directory(dir));
    const temporary = `${base}/${randomUUID()}.json`;
    const destination = `${base}/${name}.json`;
    // Keep ownership even when open resolves after cancellation: finally closes and unlinks it.
    const file = await open(temporary, constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY | constants.O_NOFOLLOW, 0o600);
    let linked = false;
    try {
      this.check();
      await file.writeFile(bytes);
      this.check();
      await file.sync();
      this.check();
      await file.close();
      await this.assertCurrent(plan);
      this.check();
      await link(temporary, destination);
      linked = true;
      this.check();
      await this.assertCurrent(plan);
      await (await this.directory(dir)).sync();
      this.check();
      return path.join(this.root, dir, `${name}.json`);
    } catch (error) {
      if (linked) await unlink(destination).catch(() => {});
      throw error;
    } finally {
      await file.close().catch(() => {});
      await unlink(temporary).catch(() => {});
    }
  }
  async close(): Promise<void> { await Promise.allSettled([...this.dirs.values()].map(dir => dir.close())); }
}
