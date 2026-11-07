import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import { extractEvidence } from "./extract.js";
import { canonical, hash } from "./evidence-artifacts.js";

/** Registration never resolves credentials or touches provider/source artifacts. */
export default function jevifyExtension(pi: ExtensionAPI): void {
  pi.registerFlag("jevify-extract", {
    type: "boolean", default: true,
    description: "Extract candidate occurrences from explicitly prepared public/private evidence through OpenRouter/Clairic (default on; --no-jevify-extract wins)",
  });
  pi.registerFlag("no-jevify-extract", {
    type: "boolean", default: false,
    description: "Disable Jevify extraction transport; deterministic evidence and explicit unresolved results remain available",
  });
  const parameters = pi.zod.object({
    artifact_root: pi.zod.string().min(1).max(4096).describe("Absolute .jevify-evidence root returned by prepare_evidence; Linux artifact bridge"),
    plan_digest: pi.zod.string().regex(/^[a-f0-9]{64}$/).describe("Frozen prepare_evidence plan SHA-256"),
    recover_zero_transmission: pi.zod.boolean().optional().describe("Explicitly create a new immutable attempt only after a validated disabled/no-credential result with zero transmitted requests; never retries uncertain or billed attempts"),
  });
  pi.registerTool<typeof parameters>({
    name: "jevify_extract",
    label: "Jevify evidence extraction",
    description: "Read a prepared immutable evidence plan and extract exact candidate occurrences. Default on: explicitly selected public/private source text leaves this machine for OpenRouter cloudflare/clairic using the host credential resolver. Secrets are excluded. Frozen caps, one operation deadline, no automatic retries. Returns only result/receipt references and counts; adopt with Python adopt_evidence(plan, result_path). No credential/disabled/failure keeps deterministic evidence and unresolved fields. Occurrences are not verified findings or deletion authority.",
    parameters,
    approval: "write",
    loadMode: "essential",
    execute: async (id, input, signal, _update, ctx) => {
      const result = await extractEvidence(input, {
        enabled: pi.getFlag("no-jevify-extract") !== true && pi.getFlag("jevify-extract") !== false,
        signal,
        resolveCredential: operation => ctx.modelRegistry.getApiKeyForProvider("openrouter", ctx.sessionManager.getSessionId(), { signal: operation }),
      });
      // Files are model-writable. Even strictly admitted cache contents cannot attest this call's billing.
      if (result.reused || result.disposition === "uncertain_attempt" || result.disposition === "busy") {
        return { content: [{ type: "text", text: JSON.stringify(result) }], details: { accountingTrust: "untrusted_history" } };
      }
      const accountingResult = {
        schema: "jevify-extraction-accounting/1", plan_digest: result.plan_digest,
        run_id: result.run_id, generation: result.generation, disposition: result.disposition,
        records: result.records, fields: result.fields, candidates: result.candidates,
        request_attempts: result.request_attempts, reused: result.reused,
        transmission_uncertain: result.transmission_uncertain,
        result_sha256: result.result_sha256, provenance: result.accounting,
      };
      return {
        content: [{ type: "text", text: JSON.stringify(result) }],
        details: {
          controllerReceipt: {
            schema: "jevify-extraction-receipt/1", tool_call_id: id,
            plan_digest: input.plan_digest, artifact_root: input.artifact_root,
            result_sha256: hash(canonical(accountingResult)), result: accountingResult,
            published: result.result_path !== null && result.receipt_path !== null,
          },
        },
      };
    },
  });
}
