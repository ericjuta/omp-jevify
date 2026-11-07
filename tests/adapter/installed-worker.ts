import assert from "node:assert/strict";
import { mkdir, readFile, symlink, writeFile } from "node:fs/promises";
import { Database } from "bun:sqlite";
import path from "node:path";
import type { ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import type { ToolSummary } from "../../src/extract.js";

const [candidate, temporary, controllerModule] = process.argv.slice(2);
assert(candidate && temporary && controllerModule, "candidate package, private temporary root and source controller module required");
const realFetch = globalThis.fetch;
let requests = 0;
let mode: "valid" | "interrupted" = "valid";
let activeController: AbortController | undefined;
const server = Bun.serve({
  hostname: "127.0.0.1", port: 0,
  async fetch(request) {
    requests++;
    assert.equal(request.headers.get("authorization"), "Bearer loopback-only");
    const body = await request.json() as { state: string; fields: Record<string, unknown> };
    if (mode === "interrupted") {
      return new Response(new ReadableStream({ start(controller) {
        controller.enqueue(new TextEncoder().encode('{"answers":'));
        activeController!.abort();
      } }));
    }
    const value = "😀 quoted value";
    const offset = body.state.indexOf(value);
    const answer = offset < 0 ? { value: null, guess: value, p_null: 0.99 } : {
      value, p_null: 0.1, span: [Array.from(body.state.slice(0, offset)).length, Array.from(body.state.slice(0, offset)).length + Array.from(value).length],
    };
    return Response.json({ truncated: false, state_text: body.state, answers: { quote: answer }, usage: { cost: 0, passes: 1 } });
  },
});
// No public endpoint override exists. Only this test process intercepts the fixed production URL.
globalThis.fetch = (async (url: string | URL | Request, init?: RequestInit) => {
  assert.equal(String(url), "https://openrouter.ai/api/alpha/extractions", "all other outbound requests are prohibited");
  return realFetch(`http://127.0.0.1:${server.port}/`, init);
}) as typeof fetch;
const pythonPrelude = `import importlib.util,json,sys\nfrom pathlib import Path\nspec=importlib.util.spec_from_file_location('jevify_evidence',sys.argv[1])\ne=importlib.util.module_from_spec(spec)\nspec.loader.exec_module(e)\n`;
async function python(code: string, args: string[] = [], input?: unknown): Promise<any> {
  const child = Bun.spawn(["python3", "-c", pythonPrelude + code, path.join(candidate!, "skills/jevify/evidence.py"), ...args], { stdin: "pipe", stdout: "pipe", stderr: "pipe" });
  if (input !== undefined) child.stdin.write(JSON.stringify(input));
  child.stdin.end();
  const [stdout, stderr, status] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited]);
  assert.equal(status, 0, stderr);
  return JSON.parse(stdout);
}
async function prepare(name: string) {
  const root = path.join(temporary!, name);
  await mkdir(root);
  return python(`p=e.prepare_evidence([{'id':'unicode','source_id':'s1','required_fields':['quote'],'required_questions':['supported']},{'id':'abstain','source_id':'s2','required_fields':['quote'],'required_questions':['supported']}],{'quote':{'description':'Exact relevant words; null if absent'}},root=sys.argv[2],sources=[{'id':'s1','role':'original_comment','text':'first\\r\\n😀 quoted value\\nlast','snapshot_identity':'smoke:s1','classification':'public'},{'id':'s2','role':'original_comment','text':'nothing to extract','snapshot_identity':'smoke:s2','classification':'public'}])\nprint(json.dumps(p,ensure_ascii=False))`, [root]);
}
try {
  // Actual SDK loader; the observer also uses an isolated in-memory session and empty credential store.
  const { loadExtensions } = await import("@oh-my-pi/pi-coding-agent/extensibility/extensions/loader");
  const { canonical, hash } = await import(path.join(candidate, "src/evidence-artifacts.ts"));
  async function verifyHostObserver() {
    const source: unknown = await python(`observer_spec=importlib.util.spec_from_file_location('jevify_preparation_observer',sys.argv[2])\nobserver=importlib.util.module_from_spec(observer_spec)\nsys.modules[observer_spec.name]=observer\nobserver_spec.loader.exec_module(observer)\nprint(json.dumps(observer.HOST_OBSERVER))`, [controllerModule!]);
    assert(typeof source === "string");
    const observerPath = path.join(temporary!, "actual-host-observer.ts");
    await writeFile(observerPath, source);
    const observed = await loadExtensions([observerPath], temporary!);
    assert.deepEqual(observed.errors, []);
    const { SessionManager, ModelRegistry, AuthStorage, SqliteAuthCredentialStore, z } = await import("@oh-my-pi/pi-coding-agent");
    const { ExtensionRunner } = await import("@oh-my-pi/pi-coding-agent/extensibility/extensions/runner");
    const { ThinkingLevel } = await import("@oh-my-pi/pi-agent-core");
    observed.runtime.getThinkingLevel = () => ThinkingLevel.High;
    const session = SessionManager.inMemory(temporary!);
    const credentials = new SqliteAuthCredentialStore(new Database(":memory:"));
    const originalWrite = process.stdout.write;
    try {
      const registry = new ModelRegistry(new AuthStorage(credentials), path.join(temporary!, "observer-models.yml"), {
        ignoreLocalModelConfig: true, cacheDbPath: path.join(temporary!, "observer-models.db"),
        fetch: async () => { throw new Error("observer smoke forbids provider requests"); },
      });
      const runner = new ExtensionRunner(observed.extensions, observed.runtime, temporary!, session, registry);
      const chunks: string[] = [];
      process.stdout.write = (chunk: string | Uint8Array, encodingOrCallback?: BufferEncoding | ((error?: Error | null) => void), callback?: (error?: Error | null) => void): boolean => {
        chunks.push(typeof chunk === "string" ? chunk : Buffer.from(chunk).toString("utf8"));
        (typeof encodingOrCallback === "function" ? encodingOrCallback : callback)?.();
        return true;
      };
      await runner.emit({ type: "session_start" });
      const entryId = session.appendModelUsage({
        purpose: "judgment", role: "judge", api: "openai-responses", provider: "openrouter", model: "fixture-judge",
        usage: { input: 4, output: 2, cacheRead: 0, cacheWrite: 0, totalTokens: 6,
          cost: { input: 0.01, output: 0.02, cacheRead: 0, cacheWrite: 0, total: 0.03 } },
        stopReason: "stop", errorMessage: "private-provider-diagnostic-must-not-cross",
      }, { sessionId: session.getSessionId(), parentId: null });
      assert(entryId, "real SDK must admit and assign the usage entry");
      const entry = session.getEntries().find(row => row.id === entryId);
      assert(entry?.type === "model_usage");
      await runner.emit({ type: "tool_execution_end", toolName: "judge_batch", toolCallId: "observer-smoke", result: { content: [] }, isError: false });
      await runner.emit({ type: "agent_end", messages: [] });
      await runner.emit({ type: "session_shutdown" });
      process.stdout.write = originalWrite;
      const frameSchema = z.object({ type: z.literal("preparation_observation"), schema: z.literal("jevify-host-observation/1"),
        session_id: z.string(), thinking: z.string(), usage: z.array(z.unknown()), final: z.boolean() });
      const frames = chunks.join("").trim().split("\n").map(line => frameSchema.parse(JSON.parse(line)));
      assert.equal(frames.length, 4, "all four actual SDK lifecycle hooks must emit host observations");
      assert.equal(frames[0]!.thinking, "high", "initial fixed effort must come from the host API");
      assert(frames.every(frame => frame.session_id === session.getSessionId()));
      assert.equal(frames.at(-1)!.final, true);
      const usages = frames.flatMap(frame => frame.usage);
      assert.equal(usages.length, 1, "repeated lifecycle hooks must not recount one SDK usage entry");
      assert.deepEqual(usages[0], { id: entry.id, purpose: entry.purpose, role: entry.role, api: entry.api,
        provider: entry.provider, model: entry.model, stopReason: entry.stopReason, usage: entry.usage });
      assert(!JSON.stringify(frames).includes("private-provider-diagnostic-must-not-cross"));
      return frames.length;
    } finally {
      process.stdout.write = originalWrite;
      credentials.close();
    }
  }
  const loaded = await loadExtensions([path.join(candidate, "src/index.ts")], temporary);
  assert.deepEqual(loaded.errors, []);
  const tool = loaded.extensions[0]?.tools.get("jevify_extract")?.definition;
  assert(tool, "actual registered tool absent");
  let credentials = 0;
  const context = {
    modelRegistry: { async getApiKeyForProvider(provider: string, sessionId?: string, options?: { signal?: AbortSignal }) {
      assert.equal(provider, "openrouter"); assert.equal(sessionId, "isolated-loopback"); assert(options?.signal);
      credentials++; return "loopback-only";
    } },
    sessionManager: { getSessionId: () => "isolated-loopback" },
  } as unknown as ExtensionContext;
  const controllerDirectory = path.join(temporary, "controller-capture");
  await mkdir(controllerDirectory);
  let invocation = 0;
  let captures = 0;
  async function invoke(plan: { artifact_root: string; plan_digest: string; recover_zero_transmission?: boolean }, signal?: AbortSignal): Promise<ToolSummary> {
    const id = `smoke-${++invocation}`;
    const args = { artifact_root: plan.artifact_root, plan_digest: plan.plan_digest, recover_zero_transmission: plan.recover_zero_transmission };
    const result = await tool!.execute(id, args, signal, undefined, context);
    const text = result.content.find(part => part.type === "text");
    assert(text?.type === "text");
    const summary = JSON.parse(text.text);
    const receipt = result.details.controllerReceipt;
    if (summary.reused || summary.disposition === "uncertain_attempt" || summary.disposition === "busy") {
      assert.equal(receipt, undefined, "model-writable history cannot attest controller accounting");
      assert.equal(result.details.accountingTrust, "untrusted_history");
      return summary;
    }
    assert.equal(receipt.schema, "jevify-extraction-receipt/1");
    assert.equal(receipt.tool_call_id, id);
    assert.equal(receipt.plan_digest, plan.plan_digest);
    assert.equal(receipt.artifact_root, plan.artifact_root);
    assert.equal(receipt.result.schema, "jevify-extraction-accounting/1");
    assert.equal(receipt.result_sha256, hash(canonical(receipt.result)));
    assert(!JSON.stringify(receipt).includes("😀 quoted value"), "controller accounting must contain no source text");
    assert.equal(receipt.result.request_attempts, summary.request_attempts);
    assert.equal(receipt.result.result_sha256, summary.result_sha256);
    assert.equal(receipt.published, summary.result_path !== null && summary.receipt_path !== null);
    if (summary.disposition !== "artifact_refused") {
      const captured = await python(`controller_spec=importlib.util.spec_from_file_location('jevify_preparation_controller',sys.argv[2])\ncontroller=importlib.util.module_from_spec(controller_spec)\nsys.modules[controller_spec.name]=controller\ncontroller_spec.loader.exec_module(controller)\npayload=json.load(sys.stdin)\ncaptured=controller.HostExecutor.capture_extraction(payload['event'],payload['call'],payload['plan'],Path(sys.argv[3]),int(sys.argv[4]))\nprint(json.dumps(captured,ensure_ascii=False))`, [controllerModule!, controllerDirectory, String(++captures)], {
        event: { type: "tool_execution_end", toolName: "jevify_extract", toolCallId: id, result, isError: false },
        call: { id, tool: "jevify_extract", args }, plan,
      });
      assert.deepEqual(captured, receipt, "Python controller must accept actual SDK return details unchanged");
    }
    return summary;
  }
  const plan = await prepare("valid");
  const result = await invoke(plan);
  assert.equal(result.disposition, "complete");
  assert.equal(result.request_attempts, 2);
  assert.equal(result.candidates, 1);
  assert(result.result_path);
  const adopted = await python(`plan=json.loads(Path(sys.argv[2]).read_text())\na=e.adopt_evidence(plan,sys.argv[3])\nprint(json.dumps(a,ensure_ascii=False))`, [path.join(plan.artifact_root, "plans", `${plan.plan_digest}.json`), result.result_path]);
  assert.equal(adopted.records.unicode.observations.quote.occurrence.byte_start, 7);
  assert.equal(adopted.records.unicode.observations.quote.occurrence.byte_end, 24);
  assert.equal(adopted.records.abstain.observations.quote.value, null);
  assert.equal(adopted.ledger.complete, false);
  assert.equal((await invoke(plan)).reused, true);
  const disabled = await prepare("disabled");
  loaded.runtime.flagValues.set("no-jevify-extract", true);
  const previousRequests = requests;
  const previousCredentials = credentials;
  const disabledResult = await invoke(disabled);
  assert.equal(disabledResult.disposition, "disabled");
  const retainedDisabled = await readFile(disabledResult.result_path!, "utf8");
  assert.equal(requests, previousRequests);
  assert.equal(credentials, previousCredentials);
  loaded.runtime.flagValues.set("no-jevify-extract", false);
  const invalid = await prepare("invalid");
  assert.equal((await invoke({ ...invalid, plan_digest: "0".repeat(64) })).disposition, "artifact_refused");
  assert.equal(credentials, previousCredentials);
  const escape = await prepare("escape");
  await symlink(temporary, path.join(escape.artifact_root, "results"));
  assert.equal((await invoke(escape)).disposition, "artifact_refused");
  assert.equal(credentials, previousCredentials);
  const recovered = await invoke({ ...disabled, recover_zero_transmission: true });
  assert.equal(recovered.disposition, "complete");
  assert.equal(recovered.generation, 1);
  assert.notEqual(recovered.result_path, disabledResult.result_path);
  assert.equal(await readFile(disabledResult.result_path!, "utf8"), retainedDisabled);
  assert.equal(recovered.accounting?.request_attempts, 2);
  assert(recovered.result_sha256);
  const interrupted = await prepare("interrupted");
  mode = "interrupted";
  activeController = new AbortController();
  const stopped = await invoke(interrupted, activeController.signal);
  assert.equal(stopped.disposition, "interrupted");
  assert.equal(stopped.candidates, 0);
  assert.equal(stopped.result_path, null);
  assert.equal(stopped.request_attempts, 1);
  assert.equal(stopped.transmission_uncertain, true);
  assert.equal(stopped.accounting?.request_attempts, 1);
  assert(captures >= 4, "success, disabled, recovery and interruption must cross the TS/Python controller boundary");
  const observerFrames = await verifyHostObserver();
  console.log(JSON.stringify({ packaged: true, fresh_process: true, sdk_loader: true, unicode_occurrence: true, null_guess_abstention: true, disabled_no_access: true, explicit_zero_request_recovery: true, unsafe_path_no_access: true, interrupted_no_candidate: true, controller_accounting: true, python_controller_captures: captures, sdk_model_usage_observer: true, host_observer_frames: observerFrames, loopback_requests: requests, outbound_provider_requests: 0 }));
} finally { globalThis.fetch = realFetch; server.stop(true); }
