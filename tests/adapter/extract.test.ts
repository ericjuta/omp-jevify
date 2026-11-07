import { afterEach, describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { chmod, mkdir, readFile, rename, symlink, unlink, writeFile } from "node:fs/promises";
import path from "node:path";
import { admitResponse, EXTRACT_ENDPOINT, extractEvidence } from "../../src/extract.js";
import { canonical, hash } from "../../src/evidence-artifacts.js";
import { fixture, reply, wire } from "./fixtures.js";

const cleanups: (() => Promise<void>)[] = [];
async function prepared(options: Parameters<typeof fixture>[0] = {}) {
  const value = await fixture(options);
  cleanups.push(value.cleanup);
  return value;
}
afterEach(async () => { for (const cleanup of cleanups.splice(0)) await cleanup(); });
const forbidden = async (): Promise<never> => { throw new Error("unexpected credential/network access"); };

describe("bounded extraction transport", () => {
  test("exact Unicode/CRLF occurrence, redacted receipt, zero-cost unknown and immutable reuse", async () => {
    const f = await prepared();
    let attempts = 0;
    const run = await extractEvidence(f.input, {
      resolveCredential: async () => "test-only-secret",
      request: async (url, init) => {
        expect(url).toBe(EXTRACT_ENDPOINT);
        expect(init.redirect).toBe("error");
        expect(init.headers).toEqual({ authorization: "Bearer test-only-secret", "content-type": "application/json" });
        const body = JSON.parse(String(init.body));
        expect(body.state).toBe(f.text);
        expect(body.fields.quote).toEqual({ type: "text", instructions: expect.stringContaining(f.plan.fields.quote!.description), null_threshold: 0.5 });
        expect(JSON.parse(body.fields.quote.instructions.split("Target record context (JSON): ")[1])).toEqual({
          record: f.plan.records.row0, source: { id: "source", role: "original_comment", subject: null, version: null },
        });
        expect(body.provider).toEqual({ options: { cloudflare: { return_state_text: true } } });
        expect(body.questions).toBeUndefined();
        attempts++;
        return reply(wire(f.text));
      },
    });
    expect(run.disposition).toBe("complete");
    expect(run.candidates).toBe(1);
    const resultText = await readFile(run.result_path!, "utf8");
    expect(resultText).not.toContain("test-only-secret");
    const result = JSON.parse(resultText);
    expect(result.records.row0.observations.quote).toMatchObject({ status: "candidate", value: "😀 quoted value", start: 7, end: 21 });
    expect(result.provenance.attempts[0].usage).toMatchObject({ reported_cost_usd: 0, cost_usd: null });
    const again = await extractEvidence(f.input, { resolveCredential: forbidden, request: forbidden });
    expect(again.reused).toBe(true);
    expect(again.result_path).toBe(run.result_path);
    expect(attempts).toBe(1);
  });
  test("shared-source records send distinct target contexts without shifting original Unicode spans", async () => {
    const quotes = ["Alpha@v1 retries only on timeout.", "Beta@v2 never retries."];
    const text = `😀 ${quotes[0]}\r\n${quotes[1]}\n`;
    const passages = quotes.map(quote => {
      const start = Array.from(text.slice(0, text.indexOf(quote))).length;
      return { source_id: "source", revision: quote.includes("@v1") ? "v1" : "v2", start, end: start + Array.from(quote).length, quote };
    });
    const f = await prepared({ text, recordContexts: [
      { subject: "Alpha", version: "v1", facts: { retrieved_passages: [passages[0]] } },
      { subject: "Beta", version: "v2", facts: { retrieved_passages: [passages[1]] } },
    ] });
    const bodies: string[] = [];
    const run = await extractEvidence(f.input, { resolveCredential: async () => "test", request: async (_url, init) => {
      const serialized = String(init.body);
      const body = JSON.parse(serialized);
      const context = JSON.parse(body.fields.quote.instructions.split("Target record context (JSON): ")[1]);
      const index = bodies.length;
      expect(context.record).toEqual(f.plan.records[`row${index}`]);
      expect(context.source).toEqual({ id: "source", role: "original_comment", subject: null, version: null });
      expect(body.state).toBe(text);
      expect(Buffer.byteLength(serialized)).toBeLessThanOrEqual(f.plan.limits.max_request_bytes);
      bodies.push(serialized);
      return reply(wire(body.state, context.record.facts.retrieved_passages[0].quote));
    } });
    expect(run.disposition).toBe("complete");
    expect(run.request_attempts).toBe(2);
    expect(new Set(bodies).size).toBe(2);
    const result = JSON.parse(await readFile(run.result_path!, "utf8"));
    for (const [index, passage] of passages.entries()) {
      expect(result.records[`row${index}`].observations.quote).toEqual({
        status: "candidate", value: passage.quote, source_id: "source", start: passage.start, end: passage.end, reason: null,
      });
    }
  });
  test("record-context metadata and serialized request bytes remain capped before transport", async () => {
    const context = { subject: "Alpha", version: "v1", facts: { retrieved_passages: [{ quote: "x".repeat(4096) }] } };
    const f = await prepared({ recordContexts: [context], limits: { max_request_bytes: 1024 } });
    const capped = await extractEvidence(f.input, { resolveCredential: async () => "test", request: forbidden });
    expect(capped.disposition).toBe("cap_reached");
    expect(capped.request_attempts).toBe(0);
    expect(capped.candidates).toBe(0);
    const invalid = await prepared({ recordContexts: [context], limits: { max_metadata_bytes: 2048 } });
    expect((await extractEvidence(invalid.input, { resolveCredential: forbidden, request: forbidden })).disposition).toBe("artifact_refused");
  });
  test("disabled and no credential retain every record with no network", async () => {
    const f = await prepared({ records: 2 });
    const disabled = await extractEvidence(f.input, { enabled: false, resolveCredential: forbidden, request: forbidden });
    expect(disabled.disposition).toBe("disabled");
    expect(disabled.request_attempts).toBe(0);
    expect(Object.keys(JSON.parse(await readFile(disabled.result_path!, "utf8")).records)).toEqual(["row0", "row1"]);
    const n = await prepared();
    const missing = await extractEvidence(n.input, { resolveCredential: async () => undefined, request: forbidden });
    expect(missing.disposition).toBe("no_credential");
    const keyless = await prepared();
    expect((await extractEvidence(keyless.input, { resolveCredential: async () => "N/A", request: forbidden })).disposition).toBe("no_credential");
  });
  test("structured rows bypass transport and mixed rows send only their required fields", async () => {
    const structured = await prepared({ structuredFirst: true });
    expect((await extractEvidence(structured.input, { resolveCredential: forbidden, request: forbidden })).disposition).toBe("complete");
    const mixed = await prepared({ records: 2, structuredFirst: true });
    let requests = 0;
    const result = await extractEvidence(mixed.input, { resolveCredential: async () => "test", request: async (_url, init) => {
      requests++;
      expect(Object.keys(JSON.parse(String(init.body)).fields)).toEqual(["quote"]);
      return reply(wire(mixed.text));
    } });
    expect(requests).toBe(1);
    expect(result.fields).toBe(1);
    expect(result.records).toBe(2);
    const body = JSON.parse(await readFile(result.result_path!, "utf8"));
    expect(body.records.row0.observations).toEqual({});
  });
  test("wrong digest, absent caps, secret, traversal and source mutation fail before credentials", async () => {
    const f = await prepared();
    expect((await extractEvidence({ ...f.input, plan_digest: "0".repeat(64) }, { resolveCredential: forbidden })).disposition).toBe("artifact_refused");
    const { plan_digest: _digest, ...base } = f.plan;
    const limits: Record<string, number> = { ...base.limits };
    delete limits.max_requests;
    const changed = { ...base, limits };
    const digest = hash(canonical(changed));
    await writeFile(path.join(f.input.artifact_root, "plans", `${digest}.json`), canonical({ ...changed, plan_digest: digest }));
    expect((await extractEvidence({ ...f.input, plan_digest: digest }, { resolveCredential: forbidden })).disposition).toBe("artifact_refused");
    const secret = await prepared({ secret: true });
    expect((await extractEvidence(secret.input, { resolveCredential: forbidden })).disposition).toBe("artifact_refused");
    expect((await extractEvidence({ ...f.input, artifact_root: `${f.input.artifact_root}/../.jevify-evidence` }, { resolveCredential: forbidden })).disposition).toBe("artifact_refused");
    await writeFile(f.snapshot, f.text + "changed");
    expect((await extractEvidence(f.input, { resolveCredential: forbidden })).disposition).toBe("artifact_refused");
  });
  test("execute-only ancestors do not require directory-list permission", async () => {
    const f = await prepared();
    await chmod(f.root, 0o111);
    try {
      const result = await extractEvidence(f.input, { enabled: false, resolveCredential: forbidden, request: forbidden });
      expect(result.disposition).toBe("disabled");
    } finally { await chmod(f.root, 0o700); }
  });
  test("symlinked source and output directory escapes are refused", async () => {
    const f = await prepared();
    const outside = path.join(f.root, "outside");
    await writeFile(outside, f.text);
    await unlink(f.snapshot);
    await symlink(outside, f.snapshot);
    expect((await extractEvidence(f.input, { resolveCredential: forbidden })).disposition).toBe("artifact_refused");
    const o = await prepared();
    await symlink(o.root, path.join(o.input.artifact_root, "results"));
    expect((await extractEvidence(o.input, { resolveCredential: forbidden })).disposition).toBe("artifact_refused");
  });
  test("replacement during transport cannot publish into attacker output root", async () => {
    const f = await prepared();
    const run = await extractEvidence(f.input, { resolveCredential: async () => "test",
      request: async () => {
        await rename(path.join(f.input.artifact_root, "results"), path.join(f.input.artifact_root, "old-results"));
        await symlink(f.root, path.join(f.input.artifact_root, "results"));
        return reply(wire(f.text));
      } });
    expect(run.disposition).toBe("artifact_refused");
    expect(run.result_path).toBeNull();
    expect(run.request_attempts).toBe(1);
    expect(run.records).toBe(1);
    expect(run.fields).toBe(1);
    expect(run.transmission_uncertain).toBe(true);
    expect(run.accounting?.request_attempts).toBe(1);
    expect(await Bun.file(path.join(f.root, `${f.input.plan_digest}.json`)).exists()).toBe(false);
  });
  test("slow credential is deadline-bounded and never starts transport later", async () => {
    const f = await prepared({ limits: { deadline_ms: 20 } });
    const key = Promise.withResolvers<string>();
    const settled = Promise.withResolvers<void>();
    let requests = 0;
    const run = await extractEvidence(f.input, { onSettled: settled.resolve, resolveCredential: () => key.promise, request: async () => { requests++; return reply({}); } });
    expect(run.disposition).toBe("timeout");
    key.resolve("test");
    await settled.promise;
    expect(requests).toBe(0);
  });
  test("noncooperative late transport is disposed after cancellation without a second attempt", async () => {
    const f = await prepared();
    const pending = Promise.withResolvers<Response>();
    const entered = Promise.withResolvers<void>();
    const settled = Promise.withResolvers<void>();
    const controller = new AbortController();
    let disposed = 0;
    const cancelled = Promise.withResolvers<void>();
    const running = extractEvidence(f.input, { signal: controller.signal, onSettled: settled.resolve, resolveCredential: async () => "test", request: () => { entered.resolve(); return pending.promise; } });
    await entered.promise;
    controller.abort();
    const result = await running;
    expect(result.disposition).toBe("interrupted");
    expect((await extractEvidence(f.input, { resolveCredential: forbidden })).disposition).toBe("busy");
    pending.resolve(new Response(new ReadableStream({ cancel() { disposed++; cancelled.resolve(); } })));
    await cancelled.promise;
    await settled.promise;
    expect(disposed).toBe(1);
    expect(result.candidates).toBe(0);
  });
  test("an orphaned durable claim refuses resubmission without resolving credentials", async () => {
    const f = await prepared();
    await mkdir(path.join(f.input.artifact_root, "attempts"));
    await writeFile(path.join(f.input.artifact_root, "attempts", `${f.input.plan_digest}.json`), JSON.stringify({ schema: "jevify-extraction-attempt/1", plan_digest: f.input.plan_digest, run_id: randomUUID(), generation: 0, disposition: "reserved", requested_model: "cloudflare/clairic" }));
    const result = await extractEvidence(f.input, { resolveCredential: forbidden, request: forbidden });
    expect(result.disposition).toBe("uncertain_attempt");
  });
  test("plan replacement during credential lookup prevents transmission", async () => {
    const f = await prepared();
    let requests = 0;
    const result = await extractEvidence(f.input, { resolveCredential: async () => { await writeFile(f.planPath, "{}"); return "test"; }, request: async () => { requests++; return reply({}); } });
    expect(requests).toBe(0);
    expect(result.disposition).toBe("artifact_refused");
  });
  test("oversized advertised and streamed responses are bounded without candidate", async () => {
    for (const advertised of [true, false]) {
      const f = await prepared({ limits: { max_response_bytes: 256 } });
      const run = await extractEvidence(f.input, { resolveCredential: async () => "test", request: async () => new Response(new ReadableStream({ start(controller) { controller.enqueue(new Uint8Array(257)); controller.close(); } }), advertised ? { headers: { "content-length": "257" } } : undefined) });
      expect(run.disposition).toBe("truncated_response");
      expect(run.candidates).toBe(0);
      expect(run.request_attempts).toBe(1);
    }
  });
  test("interrupted request has durable uncertainty and no silent retry", async () => {
    const f = await prepared();
    const controller = new AbortController();
    const settled = Promise.withResolvers<void>();
    const run = await extractEvidence(f.input, { signal: controller.signal, onSettled: settled.resolve, resolveCredential: async () => "test",
      request: async () => { controller.abort(); return new Response(new ReadableStream({ start(stream) { stream.enqueue(new TextEncoder().encode('{"answers":')); } })); } });
    expect(run.disposition).toBe("interrupted");
    expect(run.candidates).toBe(0);
    expect(run.result_path).toBeNull();
    expect(run.request_attempts).toBe(1);
    expect(run.transmission_uncertain).toBe(true);
    expect(await Bun.file(path.join(f.input.artifact_root, "attempts", `${f.input.plan_digest}-1.json`)).exists()).toBe(true);
    await settled.promise;
    expect((await extractEvidence({ ...f.input, recover_zero_transmission: true }, { resolveCredential: forbidden, request: forbidden })).disposition).toBe("uncertain_attempt");
  });
  test("completed earlier records survive a later failure and request cap", async () => {
    const f = await prepared({ records: 3, limits: { max_requests: 2 } });
    let count = 0;
    const run = await extractEvidence(f.input, { resolveCredential: async () => "test", request: async () => ++count === 1 ? reply(wire(f.text)) : new Response("private secret error", { status: 503 }) });
    expect(run.disposition).toBe("cap_reached");
    expect(run.candidates).toBe(1);
    expect(count).toBe(2);
    const serialized = await readFile(run.result_path!, "utf8");
    expect(serialized).not.toContain("private secret error");
    const result = JSON.parse(serialized);
    expect(result.status).toBe("partial");
    expect(result.records.row0.observations.quote.status).toBe("candidate");
    expect(result.records.row1.observations.quote.reason).toBe("transport_failure");
    expect(result.records.row2.observations.quote.reason).toBe("cap_reached");
  });
  test("simultaneous calls never produce concurrent provider requests", async () => {
    const f = await prepared();
    const entered = Promise.withResolvers<void>();
    const response = Promise.withResolvers<Response>();
    const first = extractEvidence(f.input, { resolveCredential: async () => "test", request: () => { entered.resolve(); return response.promise; } });
    await entered.promise;
    expect((await extractEvidence(f.input, { resolveCredential: forbidden })).disposition).toBe("busy");
    response.resolve(reply(wire(f.text)));
    expect((await first).disposition).toBe("complete");
  });
});

describe("strict response admission", () => {
  test("null with guess and missing fields abstain without substitution", async () => {
    const f = await prepared();
    for (const answers of [{ quote: { value: null, guess: "😀 quoted value", p_null: 0.9 } }, {}]) {
      const result = admitResponse({ ...wire(f.text), answers }, f.text, f.plan.sources.source!, f.plan.fields, 1024);
      expect(result.observations.quote!.status).toBe("missing");
      expect(result.observations.quote!.value).toBeNull();
    }
  });
  test("echo, truncation, keys, probability, UTF16 offsets and span/value mismatch fail closed", async () => {
    const f = await prepared();
    for (const change of [{ state_text: f.text.replace("\r\n", "\n") }, { truncated: true }, { truncated: undefined }, { answers: { other: {} } }]) {
      expect(() => admitResponse({ ...wire(f.text), ...change }, f.text, f.plan.sources.source!, f.plan.fields, 1024)).toThrow();
    }
    for (const change of [{ p_null: null }, { p_null: 0.5 }, { p_null: -0.1 }, { p_null: 2 }, { p_null: NaN }, { span: [7, 22] }, { span: [7.1, 21] }, { value: "fabricated" }]) {
      const body = wire(f.text);
      Object.assign(body.answers.quote, change);
      const parsed = admitResponse(body, f.text, f.plan.sources.source!, f.plan.fields, 1024);
      expect(parsed.disposition).toBe("rejected_occurrence");
      expect(parsed.observations.quote!.status).toBe("rejected");
    }
  });
  test("fractional usage uses canonical-safe decimal strings, never a guessed free cost", async () => {
    const f = await prepared();
    const body = wire(f.text);
    body.usage.cost = 0.0000123;
    const parsed = admitResponse(body, f.text, f.plan.sources.source!, f.plan.fields, 1024);
    expect(parsed.usage.reported_cost_usd).toBe("0.0000123");
    expect(parsed.usage.cost_usd).toBe("0.0000123");
  });
  test("BOM, whitespace and astral characters are preserved exactly", async () => {
    const f = await prepared({ text: "\uFEFF\r\n\t😀 quoted value \r\n" });
    const result = await extractEvidence(f.input, { resolveCredential: async () => "test", request: async (_url, init) => reply(wire(JSON.parse(String(init.body)).state)) });
    expect(result.candidates).toBe(1);
    const body = JSON.parse(await readFile(result.result_path!, "utf8"));
    expect(body.records.row0.observations.quote.start).toBe(4);
  });
});
