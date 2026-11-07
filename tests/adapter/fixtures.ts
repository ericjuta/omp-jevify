import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { canonical, hash, HARD_LIMITS, type EvidenceRecord, type Limits, type Plan } from "../../src/evidence-artifacts.js";

export async function fixture(options: {
  text?: string; limits?: Partial<Limits>; secret?: boolean; records?: number; structuredFirst?: boolean;
  recordContexts?: Pick<EvidenceRecord, "subject" | "version" | "facts">[];
} = {}) {
  const root = await mkdtemp(path.join(tmpdir(), "jevify-adapter-"));
  const artifactRoot = path.join(root, ".jevify-evidence");
  await mkdir(path.join(artifactRoot, "plans"), { recursive: true });
  await mkdir(path.join(artifactRoot, "snapshots"));
  const text = options.text ?? "first\r\n😀 quoted value\nlast";
  const sha = hash(text);
  const snapshot = path.join(artifactRoot, "snapshots", `${sha}.utf8`);
  await writeFile(snapshot, text);
  const base = {
    schema: "jevify-evidence/1" as const, root, artifact_root: artifactRoot,
    sources: { source: { id: "source", role: "original_comment", sha256: sha,
      byte_length: Buffer.byteLength(text), codepoint_length: Array.from(text).length,
      snapshot, subject: null, version: null, classification: options.secret ? "secret" : "private",
      transport_admitted: !options.secret } },
    records: Object.fromEntries(Array.from({ length: options.records ?? options.recordContexts?.length ?? 1 }, (_, index) => [`row${index}`, {
      id: `row${index}`, source_id: "source", dependencies: ["source"], required_fields: options.structuredFirst && index === 0 ? [] : ["quote"],
      required_questions: ["supported"], subject: null, version: null, facts: {}, ...options.recordContexts?.[index],
    }])),
    fields: { quote: { description: "Exact relevant words, null if absent", source_roles: ["original_comment"] } },
    limits: { ...HARD_LIMITS, ...options.limits }, inventories: {}, deterministic: {},
  };
  const plan = { ...base, plan_digest: hash(canonical(base)) } as Plan;
  const planPath = path.join(artifactRoot, "plans", `${plan.plan_digest}.json`);
  await writeFile(planPath, canonical(plan));
  return { plan, text, root, snapshot, planPath,
    input: { artifact_root: artifactRoot, plan_digest: plan.plan_digest },
    cleanup: () => rm(root, { recursive: true, force: true }) };
}
export function wire(state: string, value = "😀 quoted value") {
  const points = Array.from(state);
  const start = Array.from(state.slice(0, state.indexOf(value))).length;
  return { truncated: false, state_text: state, model: "cloudflare/clairic", provider: "Cloudflare",
    answers: { quote: { value, p_null: 0.1, span: [start, start + Array.from(value).length] } },
    usage: { cost: 0, passes: 1, input_tokens: points.length, output_tokens: 0 } };
}
export const reply = (value: unknown): Response => new Response(JSON.stringify(value), { headers: { "content-type": "application/json" } });
