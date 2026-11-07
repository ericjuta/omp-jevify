import { afterEach, expect, spyOn, test } from "bun:test";
import { mkdir, readFile, unlink, writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import path from "node:path";
import { ArtifactStore, hash } from "../../src/evidence-artifacts.js";
import { extractEvidence } from "../../src/extract.js";
import { fixture, reply, wire } from "./fixtures.js";

const cleanups: (() => Promise<void>)[] = [];
async function prepared(options: Parameters<typeof fixture>[0] = {}) { const f = await fixture(options); cleanups.push(f.cleanup); return f; }
afterEach(async () => { for (const cleanup of cleanups.splice(0)) await cleanup(); });
const forbidden = async (): Promise<never> => { throw new Error("unexpected credential/network access"); };

test("orphaned completed outcome fences credential resolution and retransmission", async () => {
  const f = await prepared();
  const first = await extractEvidence(f.input, { resolveCredential: async () => "test", request: async () => reply(wire(f.text)) });
  expect(first.disposition).toBe("complete");
  for (const location of [first.result_path!, first.receipt_path!, path.join(f.input.artifact_root, "attempts", `${f.input.plan_digest}.json`), path.join(f.input.artifact_root, "attempts", `${f.input.plan_digest}-1.json`)]) await unlink(location);
  const result = await extractEvidence({ ...f.input, recover_zero_transmission: true }, { resolveCredential: forbidden, request: forbidden });
  expect(result.disposition).toBe("artifact_refused");
  expect(result.request_attempts).toBe(0);
});

test("fabricated shared-source candidates cannot trigger per-record source decoding", async () => {
  const f = await prepared({ records: 10_000, text: "x".repeat(4 * 1024 * 1024) });
  await mkdir(path.join(f.input.artifact_root, "results"));
  await mkdir(path.join(f.input.artifact_root, "attempts"));
  const run = randomUUID();
  const key = f.input.plan_digest;
  const records = Object.fromEntries(Object.keys(f.plan.records).map(id => [id, { observations: { quote: { status: "candidate", value: "x", source_id: "source", start: 0, end: 1, reason: null } } }]));
  const bytes = JSON.stringify({ schema: "jevify-evidence-result/1", plan_digest: key, status: "complete", records, provenance: {} });
  await writeFile(path.join(f.input.artifact_root, "results", `${key}.json`), bytes);
  await writeFile(path.join(f.input.artifact_root, "attempts", `${key}.json`), JSON.stringify({ schema: "jevify-extraction-attempt/1", plan_digest: key, run_id: run, generation: 0, disposition: "reserved", requested_model: "cloudflare/clairic" }));
  await writeFile(path.join(f.input.artifact_root, "attempts", `${key}-99.json`), JSON.stringify({ schema: "jevify-extraction-publication/1", plan_digest: key, run_id: run, generation: 0, result_sha256: hash(bytes), summary: {} }));
  const conversion = spyOn(Array, "from");
  try {
    const result = await extractEvidence(f.input, { resolveCredential: forbidden, request: forbidden });
    expect(result.disposition).toBe("artifact_refused");
    // Immutable snapshot counting is allocation-free; forged final rows never authorize decoding.
    expect(conversion.mock.calls.filter(call => call[0] === f.text).length).toBe(0);
  } finally { conversion.mockRestore(); }
});

test("explicit same-plan recovery preserves disabled and no-key history", async () => {
  for (const disabled of [true, false]) {
    const f = await prepared();
    const original = await extractEvidence(f.input, { enabled: !disabled, resolveCredential: disabled ? forbidden : async () => undefined, request: forbidden });
    expect(original.disposition).toBe(disabled ? "disabled" : "no_credential");
    const originalResult = await readFile(original.result_path!, "utf8");
    const originalReceipt = await readFile(original.receipt_path!, "utf8");
    const withoutAuthority = await extractEvidence(f.input, { resolveCredential: forbidden, request: forbidden });
    expect(withoutAuthority.reused).toBe(true);
    let requests = 0;
    const recovered = await extractEvidence({ ...f.input, recover_zero_transmission: true }, {
      resolveCredential: async () => "test", request: async () => { requests++; return reply(wire(f.text)); },
    });
    expect(recovered.disposition).toBe("complete");
    expect(recovered.generation).toBe(1);
    expect(recovered.result_path).not.toBe(original.result_path);
    expect(recovered.result_path).toContain(`${f.input.plan_digest}--1.json`);
    expect(await readFile(original.result_path!, "utf8")).toBe(originalResult);
    expect(await readFile(original.receipt_path!, "utf8")).toBe(originalReceipt);
    expect(requests).toBe(1);
    const latest = await extractEvidence({ ...f.input, recover_zero_transmission: true }, { resolveCredential: forbidden, request: forbidden });
    expect(latest.reused).toBe(true);
    expect(latest.result_path).toBe(recovered.result_path);
    expect(latest.accounting).toEqual(recovered.accounting);
  }
});

test("malformed or inconsistent cached artifacts never grant reuse or a new transmission", async () => {
  for (const damage of ["result", "schema", "run", "counter", "count-string", "claim", "intent", "outcome"] as const) {
    const f = await prepared();
    const first = await extractEvidence(f.input, { resolveCredential: async () => "test", request: async () => reply(wire(f.text)) });
    expect(first.disposition).toBe("complete");
    const receipt = JSON.parse(await readFile(first.receipt_path!, "utf8"));
    if (damage === "result") {
      await writeFile(first.result_path!, "{}");
      receipt.result_sha256 = hash("{}");
      receipt.summary.result_sha256 = receipt.result_sha256;
    } else if (damage === "schema") receipt.schema = "jevify-extraction-publication/999";
    else if (damage === "run") receipt.run_id = "00000000-0000-0000-0000-000000000000";
    else if (damage === "counter") receipt.summary.candidates = -1;
    else if (damage === "count-string") receipt.summary.request_attempts = "1";
    else if (damage === "claim") await writeFile(path.join(f.input.artifact_root, "attempts", `${f.input.plan_digest}.json`), "{}");
    else if (damage === "intent") {
      const location = path.join(f.input.artifact_root, "attempts", `${f.input.plan_digest}-1.json`);
      const intent = JSON.parse(await readFile(location, "utf8"));
      intent.source_digest = "0".repeat(64);
      await writeFile(location, JSON.stringify(intent));
    } else await unlink(path.join(f.input.artifact_root, "attempts", `${f.input.plan_digest}-1-0.json`));
    await writeFile(first.receipt_path!, JSON.stringify(receipt));
    const result = await extractEvidence({ ...f.input, recover_zero_transmission: true }, { resolveCredential: forbidden, request: forbidden });
    expect(result.disposition).toBe("artifact_refused");
    expect(result.reused).toBe(false);
  }
});

test("stalled artifact admission returns on cancellation but retains cleanup ownership", async () => {
  const f = await prepared();
  const entered = Promise.withResolvers<void>();
  const release = Promise.withResolvers<void>();
  const settled = Promise.withResolvers<void>();
  const controller = new AbortController();
  const original = ArtifactStore.load;
  const load = spyOn(ArtifactStore, "load").mockImplementation(async (...args) => {
    const loaded = await original(...args);
    entered.resolve();
    await release.promise;
    return loaded;
  });
  try {
    const running = extractEvidence(f.input, { signal: controller.signal, onSettled: settled.resolve, resolveCredential: forbidden, request: forbidden });
    await entered.promise;
    controller.abort();
    const result = await running;
    expect(result.disposition).toBe("interrupted");
    expect(result.request_attempts).toBe(0);
    expect((await extractEvidence(f.input, { resolveCredential: forbidden })).disposition).toBe("busy");
    release.resolve();
    await settled.promise;
    expect(await Bun.file(path.join(f.input.artifact_root, "results", `${f.input.plan_digest}.json`)).exists()).toBe(false);
  } finally { release.resolve(); load.mockRestore(); }
});

test("stalled publication cannot succeed late and preserves actual attempts and observed usage", async () => {
  const f = await prepared();
  const entered = Promise.withResolvers<void>();
  const release = Promise.withResolvers<void>();
  const settled = Promise.withResolvers<void>();
  const controller = new AbortController();
  const original = ArtifactStore.prototype.publish;
  const publication = spyOn(ArtifactStore.prototype, "publish").mockImplementation(async function (this: ArtifactStore, ...args) {
    if (args[0] === "results") { entered.resolve(); await release.promise; }
    return original.apply(this, args);
  });
  try {
    const running = extractEvidence(f.input, { signal: controller.signal, onSettled: settled.resolve, resolveCredential: async () => "test", request: async () => {
      const body = wire(f.text); body.usage.cost = 0.001; return reply(body);
    } });
    await entered.promise;
    controller.abort();
    const result = await running;
    expect(result.disposition).toBe("interrupted");
    expect(result.request_attempts).toBe(1);
    expect(result.records).toBe(1);
    expect(result.candidates).toBe(1);
    expect(result.accounting?.request_attempts).toBe(1);
    expect(JSON.stringify(result.accounting)).toContain('"reported_cost_usd":"0.001"');
    expect(result.result_path).toBeNull();
    expect((await extractEvidence(f.input, { resolveCredential: forbidden })).disposition).toBe("busy");
    release.resolve();
    await settled.promise;
    expect(await Bun.file(path.join(f.input.artifact_root, "results", `${f.input.plan_digest}.json`)).exists()).toBe(false);
    expect(await Bun.file(path.join(f.input.artifact_root, "attempts", `${f.input.plan_digest}-99.json`)).exists()).toBe(false);
    expect(await Bun.file(path.join(f.input.artifact_root, "attempts", `${f.input.plan_digest}-1-0.json`)).exists()).toBe(true);
  } finally { release.resolve(); publication.mockRestore(); }
});

test("failed final receipt retains accounting and does not expose uncommitted references", async () => {
  const f = await prepared();
  const original = ArtifactStore.prototype.publish;
  const publication = spyOn(ArtifactStore.prototype, "publish").mockImplementation(async function (this: ArtifactStore, ...args) {
    if (args[1].endsWith("-99")) throw new Error("private filesystem failure");
    return original.apply(this, args);
  });
  try {
    const result = await extractEvidence(f.input, { resolveCredential: async () => "test", request: async () => reply(wire(f.text)) });
    expect(result.disposition).toBe("artifact_refused");
    expect(result.request_attempts).toBe(1);
    expect(result.accounting?.request_attempts).toBe(1);
    expect(result.run_id).not.toBeNull();
    expect(result.result_path).toBeNull();
    expect(result.receipt_path).toBeNull();
    expect(result.result_sha256).toBeNull();
    expect(JSON.stringify(result)).not.toContain("private filesystem failure");
  } finally { publication.mockRestore(); }
});

test("settlement observer failure cannot change a completed operation", async () => {
  const f = await prepared();
  const result = await extractEvidence(f.input, { enabled: false, onSettled: () => { throw new Error("observer"); }, resolveCredential: forbidden, request: forbidden });
  expect(result.disposition).toBe("disabled");
});
