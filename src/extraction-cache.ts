import path from "node:path";
import { scheduler } from "node:timers/promises";
import { ArtifactError, canonical, hash, object, type ArtifactStore, type JsonObject, type Observation, type Plan } from "./evidence-artifacts.js";
import type { Disposition, ToolSummary } from "./extract.js";

const fail = (): never => { throw new ArtifactError(); };
const keys = (row: JsonObject, expected: string[]): void => {
  if (Object.keys(row).length !== expected.length || expected.some(key => !Object.hasOwn(row, key))) fail();
};
const count = (value: unknown, maximum: number): number => typeof value === "number" && Number.isSafeInteger(value) && value >= 0 && value <= maximum ? value : fail();
const uuid = (value: unknown): string => typeof value === "string" && /^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/.test(value) ? value : fail();
const dispositions: Disposition[] = ["complete", "disabled", "no_credential", "transport_failure", "invalid_response", "truncated_response", "rejected_occurrence", "cap_reached"];
const attemptDispositions: Disposition[] = ["complete", "transport_failure", "invalid_response", "truncated_response", "rejected_occurrence", "cap_reached"];
const statuses = ["candidate", "missing", "ambiguous", "rejected", "error"];
export const generationKey = (digest: string, generation: number): string => generation === 0 ? digest : `${digest}--${generation}`;

async function optionalRead(store: ArtifactStore, directory: string, filename: string, limit: number): Promise<Buffer | null> {
  try { return await store.read(directory, filename, limit); }
  catch (error) { if ((error as NodeJS.ErrnoException).code === "ENOENT") return null; throw error; }
}
function json(bytes: Buffer): JsonObject {
  const text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes);
  const value = object(JSON.parse(text));
  // Adapter publications use exactly JSON.stringify: reject duplicate keys, BOMs and ambiguous encodings.
  if (JSON.stringify(value) !== text) return fail();
  return value;
}
function observation(raw: unknown, plan: Plan, texts: Map<string, string>, pointsBySource: Map<string, string[]>, recordId: string, name: string): Observation {
  const value = object(raw);
  keys(value, ["status", "value", "source_id", "start", "end", "reason"]);
  if (!statuses.includes(String(value.status)) || (value.reason !== null && (typeof value.reason !== "string" || Buffer.byteLength(value.reason) > 4096)) || (value.value !== null && (typeof value.value !== "string" || Buffer.byteLength(value.value) > plan.limits.max_value_bytes))) return fail();
  if (value.status === "candidate") {
    const record = plan.records[recordId]!;
    if (typeof value.source_id !== "string" || !record.dependencies.includes(value.source_id)) return fail();
    const source = plan.sources[value.source_id]!;
    if (!plan.fields[name]!.source_roles.includes(source.role) || (source.subject !== null && record.subject !== null && source.subject !== record.subject) || (source.version !== null && record.version !== null && source.version !== record.version)) return fail();
    let points = pointsBySource.get(source.id);
    if (!points) { points = Array.from(texts.get(source.id)!); pointsBySource.set(source.id, points); }
    const start = count(value.start, points.length);
    const end = count(value.end, points.length);
    if (start >= end || !value.value || points.slice(start, end).join("") !== value.value || value.reason !== null) return fail();
  } else if (value.start !== null || value.end !== null || value.source_id !== null || value.value !== null) return fail();
  return value as unknown as Observation;
}
function validateUsage(raw: unknown): void {
  const usage = object(raw);
  keys(usage, ["reported_cost_usd", "cost_usd", "reported_passes", "input_tokens", "output_tokens", "numeric_encoding", "completeness"]);
  if (usage.numeric_encoding !== "safe_integer_or_decimal_string" || !["reported", "unknown_or_incomplete"].includes(String(usage.completeness))) fail();
  for (const key of ["reported_cost_usd", "cost_usd", "reported_passes", "input_tokens", "output_tokens"]) {
    const value = usage[key];
    if (value === null) continue;
    if (typeof value === "number") count(value, Number.MAX_SAFE_INTEGER);
    else if (typeof value !== "string" || value.length > 64 || !/^(?:\d+(?:\.\d+)?)(?:e[+-]?\d+)?$/i.test(value) || !Number.isFinite(Number(value)) || Number(value) < 0) fail();
  }
  if (usage.cost_usd !== null && (Number(usage.cost_usd) <= 0 || usage.cost_usd !== usage.reported_cost_usd)) fail();
}
function claim(raw: JsonObject, plan: Plan, generation: number, model: string): string {
  keys(raw, ["schema", "plan_digest", "run_id", "generation", "disposition", "requested_model"]);
  if (raw.schema !== "jevify-extraction-attempt/1" || raw.plan_digest !== plan.plan_digest || raw.generation !== generation || raw.disposition !== "reserved" || raw.requested_model !== model) return fail();
  return uuid(raw.run_id);
}
async function admittedCache(store: ArtifactStore, plan: Plan, texts: Map<string, string>, model: string, base: ToolSummary, generation: number, signal: AbortSignal): Promise<ToolSummary | "absent" | "uncertain"> {
  const key = generationKey(plan.plan_digest, generation);
  const resultBytes = await optionalRead(store, "results", `${key}.json`, plan.limits.max_result_bytes);
  const receiptBytes = await optionalRead(store, "attempts", `${key}-99.json`, plan.limits.max_metadata_bytes);
  const claimBytes = await optionalRead(store, "attempts", `${key}.json`, plan.limits.max_metadata_bytes);
  if (resultBytes === null && receiptBytes === null) {
    if (claimBytes !== null) { claim(json(claimBytes), plan, generation, model); return "uncertain"; }
    // Even orphaned numbered intents fence transport; a missing claim is not authority to retry.
    for (let index = 1; index <= plan.limits.max_requests; index++) {
      if (await optionalRead(store, "attempts", `${key}-${index}.json`, plan.limits.max_metadata_bytes) !== null ||
        await optionalRead(store, "attempts", `${key}-${index}-0.json`, plan.limits.max_result_bytes) !== null) return fail();
    }
    return "absent";
  }
  if (!resultBytes || !receiptBytes || !claimBytes) return fail();
  const run = claim(json(claimBytes), plan, generation, model);
  const receipt = json(receiptBytes);
  keys(receipt, ["schema", "plan_digest", "run_id", "generation", "result_sha256", "summary"]);
  if (receipt.schema !== "jevify-extraction-publication/1" || receipt.plan_digest !== plan.plan_digest || receipt.run_id !== run || receipt.generation !== generation || receipt.result_sha256 !== hash(resultBytes)) return fail();
  const result = json(resultBytes);
  keys(result, ["schema", "plan_digest", "status", "records", "provenance"]);
  if (result.schema !== "jevify-evidence-result/1" || result.plan_digest !== plan.plan_digest || !["complete", "partial", "error"].includes(String(result.status))) return fail();
  const records = object(result.records);
  keys(records, Object.keys(plan.records));
  let candidates = 0;
  const provenance = object(result.provenance);
  keys(provenance, ["schema", "run_id", "generation", "disposition", "requested_model", "requested_provider", "attempts", "request_attempts", "completed_requests", "original_records", "required_fields", "input_bytes", "elapsed_ms", "usage_complete"]);
  if (Buffer.byteLength(canonical(provenance)) > plan.limits.max_metadata_bytes || provenance.schema !== "jevify-extraction-receipt/1" || provenance.run_id !== run || provenance.generation !== generation || !dispositions.includes(provenance.disposition as Disposition) || provenance.requested_model !== model || provenance.requested_provider !== "openrouter" || provenance.original_records !== base.records || provenance.required_fields !== base.fields || provenance.usage_complete !== false || !Array.isArray(provenance.attempts)) return fail();
  const attempts = provenance.attempts;
  const attemptCount = count(provenance.request_attempts, plan.limits.max_requests);
  if (attempts.length !== attemptCount) return fail();
  count(provenance.elapsed_ms, plan.limits.deadline_ms);
  let inputBytes = 0;
  let completed = 0;
  const knownCandidates = new Set<string>();
  const pointsBySource = new Map<string, string[]>();
  for (let index = 1; index <= plan.limits.max_requests; index++) {
    const intentBytes = await optionalRead(store, "attempts", `${key}-${index}.json`, plan.limits.max_metadata_bytes);
    const outcomeBytes = await optionalRead(store, "attempts", `${key}-${index}-0.json`, plan.limits.max_result_bytes);
    if (index > attemptCount) { if (intentBytes || outcomeBytes) return fail(); continue; }
    if (!intentBytes || !outcomeBytes) return fail();
    const intent = json(intentBytes);
    keys(intent, ["schema", "plan_digest", "run_id", "generation", "attempt_id", "record_id", "source_id", "source_digest", "fields", "input_bytes", "request_bytes", "requested_model"]);
    const attempt = object(attempts[index - 1]);
    keys(attempt, ["attempt_id", "record_id", "source_id", "source_digest", "fields", "input_bytes", "request_bytes", "disposition", "requested_model", "observed_model", "observed_provider", "usage", "elapsed_ms", "completed"]);
    if (intent.schema !== "jevify-extraction-intent/1" || intent.plan_digest !== plan.plan_digest || intent.run_id !== run || intent.generation !== generation || attempt.attempt_id !== `${run}:${index}` || !attemptDispositions.includes(attempt.disposition as Disposition) || typeof attempt.completed !== "boolean") return fail();
    for (const property of ["attempt_id", "record_id", "source_id", "source_digest", "fields", "input_bytes", "request_bytes", "requested_model"]) if (attempt[property] !== intent[property]) return fail();
    if (typeof attempt.record_id !== "string" || !Object.hasOwn(plan.records, attempt.record_id) || typeof attempt.source_id !== "string" || !plan.records[attempt.record_id]!.dependencies.includes(attempt.source_id)) return fail();
    const source = plan.sources[attempt.source_id]!;
    const names = plan.records[attempt.record_id]!.required_fields.filter(name => plan.fields[name]!.source_roles.includes(source.role));
    if (attempt.source_digest !== source.sha256 || attempt.fields !== names.length || attempt.input_bytes !== source.byte_length || attempt.requested_model !== model) return fail();
    if (count(attempt.request_bytes, plan.limits.max_request_bytes) < source.byte_length) return fail();
    count(attempt.elapsed_ms, plan.limits.deadline_ms);
    for (const property of ["observed_model", "observed_provider"]) if (attempt[property] !== null && (typeof attempt[property] !== "string" || !/^[a-zA-Z0-9_./:-]{1,128}$/.test(attempt[property]))) return fail();
    validateUsage(attempt.usage);
    inputBytes += source.byte_length;
    if (inputBytes > plan.limits.max_input_bytes) return fail();
    signal.throwIfAborted();
    if (attempt.completed) completed++;
    const outcome = json(outcomeBytes);
    keys(outcome, ["schema", "plan_digest", "run_id", "generation", "attempt", "observations"]);
    if (outcome.schema !== "jevify-extraction-outcome/1" || outcome.plan_digest !== plan.plan_digest || outcome.run_id !== run || outcome.generation !== generation || canonical(outcome.attempt) !== canonical(attempt)) return fail();
    const observations = object(outcome.observations);
    keys(observations, names);
    for (const [name, value] of Object.entries(observations)) {
      const admitted = observation(value, plan, texts, pointsBySource, attempt.record_id, name);
      if (!attempt.completed && admitted.status === "candidate") return fail();
      if (admitted.status === "candidate") knownCandidates.add(canonical([attempt.record_id, name, admitted]));
    }
  }
  if (provenance.input_bytes !== inputBytes || provenance.completed_requests !== completed) return fail();
  // Only admitted request outcomes may authorize candidate validation. This avoids source-sized
  // work for fabricated final rows; each admitted source is decoded once under the input-byte cap.
  let visited = 0;
  for (const [id, raw] of Object.entries(records)) {
    if (visited++ % 64 === 0) { await scheduler.yield(); signal.throwIfAborted(); }
    const record = object(raw);
    keys(record, ["observations"]);
    const observations = object(record.observations);
    keys(observations, plan.records[id]!.required_fields);
    for (const [name, rawObservation] of Object.entries(observations)) {
      if (object(rawObservation).status === "candidate" && !knownCandidates.has(canonical([id, name, rawObservation]))) return fail();
      const admitted = observation(rawObservation, plan, texts, pointsBySource, id, name);
      if (admitted.status === "candidate") candidates++;
    }
  }
  const disposition = provenance.disposition as Disposition;
  if ((disposition === "disabled" || disposition === "no_credential") && (attemptCount !== 0 || candidates !== 0)) return fail();
  const expectedStatus = disposition === "complete" || disposition === "rejected_occurrence" ? "complete" : completed ? "partial" : "error";
  if (result.status !== expectedStatus) return fail();
  const prior = object(receipt.summary);
  keys(prior, ["disposition", "plan_digest", "result_path", "receipt_path", "records", "fields", "candidates", "request_attempts", "reused", "transmission_uncertain", "run_id", "generation", "result_sha256", "accounting"]);
  if (prior.disposition !== disposition || prior.plan_digest !== plan.plan_digest || prior.result_path !== path.join(plan.artifact_root, "results", `${key}.json`) || prior.receipt_path !== null || prior.records !== base.records || prior.fields !== base.fields || prior.candidates !== candidates || prior.request_attempts !== attemptCount || prior.reused !== false || prior.transmission_uncertain !== false || prior.run_id !== run || prior.generation !== generation || prior.result_sha256 !== receipt.result_sha256 || canonical(prior.accounting) !== canonical(provenance)) return fail();
  return { ...base, disposition, result_path: prior.result_path, receipt_path: path.join(plan.artifact_root, "attempts", `${key}-99.json`), candidates, request_attempts: attemptCount, reused: true, transmission_uncertain: false, run_id: run, generation, result_sha256: hash(resultBytes), accounting: provenance };
}

/** Recovery is explicit, bounded, append-only, and only follows a validated zero-transmission outcome. */
export async function selectAttempt(store: ArtifactStore, plan: Plan, texts: Map<string, string>, model: string, base: ToolSummary, recover: boolean, signal: AbortSignal): Promise<{ key: string; generation: number; cached?: ToolSummary }> {
  let previous: ToolSummary | undefined;
  for (let generation = 0; generation <= 32; generation++) {
    const key = generationKey(plan.plan_digest, generation);
    signal.throwIfAborted();
    const cached = await admittedCache(store, plan, texts, model, base, generation, signal);
    if (cached === "uncertain") return { key, generation, cached: { ...base, disposition: "uncertain_attempt", transmission_uncertain: true } };
    if (cached === "absent") return previous && !recover ? { key, generation, cached: previous } : { key, generation };
    if (cached.request_attempts !== 0 || (cached.disposition !== "disabled" && cached.disposition !== "no_credential")) return { key, generation, cached };
    previous = cached;
  }
  if (previous) return { key: generationKey(plan.plan_digest, 32), generation: 32, cached: previous };
  return fail();
}
