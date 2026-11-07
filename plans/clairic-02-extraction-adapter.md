# ExecPlan 2: optional plugin-side Clairic extraction

This living plan follows [planning conventions](../docs/agents/issue-tracker.md). Keep Progress, Surprises & Discoveries, Decision Log and Outcomes & Retrospective current. Nothing here authorizes provider calls.

## Purpose / Big Picture

An agent prepares evidence in Python, invokes one optional OMP tool to extract verbatim fields, then validates the result back in Python. Users without extraction enabled or without an available credential retain deterministic preparation and explicit unresolved results. No upstream OMP fork is required.

## Progress

- [x] (2026-11-07) Read the OpenRouter extraction contract and current OptChat precedent.
- [x] (2026-11-07) Owner resolved [provider authority](../.scratch/clairic-evidence/issues/03-provider-authority.md): default-on for selected non-secret inputs, explicit opt-out, host credentials, separate experiment approval.
- [x] (2026-11-07) Implemented the installed Python/extension artifact bridge against the observed OMP SDK.
- [x] (2026-11-07) Implemented default-on extraction with opt-out, host-only credentials, strict publication/receipt validation and explicit zero-transmission recovery.
- [x] (2026-11-07) Final adapter suite: 29 tests passed, including distinct same-source record contexts and bounded request refusal. Packaged fresh-process SDK/Python smoke passed against loopback, including four real controller receipt captures; five loopback requests and zero outbound provider requests.
- [ ] Run a live installed-plugin proof only under separately approved manifest and cap.

## Context and Orientation

At the v0.4.0 baseline, `package.json` shipped only `skills`, README and LICENSE and `omp` was empty; there was no TypeScript extraction extension to extend. Python `jv.run`/`resume` continue to orchestrate finite judgment through the host. Add only the extraction transport here; do not replace the judge or add a Python provider client. [ExecPlan 1](clairic-01-evidence-contracts.md) defines the frozen plan, declared sources and exact occurrence validation.

OptChat's current precedent uses plain fetch to OpenRouter and resolves provider credentials from the OMP host at call time. Its earlier host-extraction-role proposal is superseded. Its API is message/store-specific and is not a reusable public Jevify contract. Read the current SDK and installed-plugin behavior before selecting an extension registration API or dependency pin.

## Interfaces and Dependencies

Implemented `jevify_extract({artifact_root, plan_digest})`, registered by `src/index.ts`, with transport in `src/extract.ts` and artifact validation in `src/evidence-artifacts.ts`. The tool reads only a prepared plan under the admitted root and returns result/receipt references plus counts, never credentials or raw provider errors. The Python helper adopts the result in a later eval cell; do not assume eval exposes a native extraction primitive.

Use adapter-owned endpoint `https://openrouter.ai/api/alpha/extractions` and model `cloudflare/clairic`. Body: `state`, a bounded named `fields` map of `type: text` with frozen instructions and null thresholds, and `provider.options.cloudflare.return_state_text: true`. Do not send `questions`, numeric/derived types, images, arbitrary URLs, or source-selected provider options. Response admission requires an untruncated response, exact `state_text`, the expected answer keys, non-null string values, finite in-range null probabilities meeting policy, valid half-open Unicode code-point spans and exact span/value equality. Missing fields and null values retain explicit dispositions. Never substitute `guess` for null. Python revalidates the result before use.

Initial proposed bounds are a 10-second whole-operation deadline, 262,144 streamed response bytes per request, concurrency 1, no automatic retries, and per-run request/input/field/byte caps frozen in the plan. These are conservative design defaults, not provider limits or user decisions. Refuse missing caps before transmission. Include credential lookup in the deadline. Resolve only the host's `openrouter` credential; the plugin must not read a key env var, flag or config itself. The owner approved the host's credential cascade, including environment-key fallback. The plugin itself must not introduce an independent credential path.

## Plan of Work

First complete the provider-free installed bridge prototype. Inspect the actual host SDK, register the optional tool without import-time credential lookup, and extend package files/extension metadata only for the necessary modules. Freeze the supported SDK range and commands in this plan after the prototype; no speculative host API imports.

Validate roots, regular files, size limits, plan/source digests and declared IDs before credential resolution. Refuse path traversal and symlink escape, including replacement between validation and publication. Bind the file actually read, not an earlier pathname check. Source text cannot choose the endpoint, root, credential or output destination. Avoid following provider-returned links. Check cancellation and plan identity before publishing results atomically.

Stream and bound response bytes; do not read an unbounded body and measure afterward. Keep provider failure states separate: disabled, no credential, timeout, interrupted, transport failure, invalid response, truncated response and rejected occurrence. Preserve original input and completed rows, including failures; no fallback fabricated candidate. Unknown/zero cost without corroboration stays unknown, with raw reported usage separately identified. Track request attempts, records and fields separately because usage passes are not original record counts.

Keep secrets and private source out of public diagnostics. Redacted receipt fields include plan/source digests, requested and observed model/provider when available, attempt disposition, elapsed time, observed usage and its completeness, and publication identity. Host alias/requested model must not be mislabeled as served provenance.

## Concrete Steps

At repo root, use the installed-bridge prototype to establish exact TypeScript typecheck/test commands and pin supported SDK imports before coding the provider transport. Retain `python3 -m unittest discover -s tests` for the Python integration. Run the new adapter tests with a local fake HTTP server and injected credential resolver, never a real key. The implementation must add and document its real package/CI commands; this plan does not pretend they already exist.

Launch a fresh OMP process with the packaged candidate in an isolated configuration, prepare a temporary two-record plan through the helper, invoke the registered extraction tool against the loopback test seam, adopt its artifact and open the review inbox. Observe a valid Unicode occurrence, a null-with-guess abstention, a blocked path escape and an interrupted response that cannot be adopted. Confirm zero outbound provider attempts with extraction disabled or malformed plans. Remove the temporary installation and artifacts afterward without touching the user's live plugin.

## Validation and Acceptance

The actual installed tool and Python helper round-trip immutable artifacts across processes. Disabled/no-key mode remains usable without network traffic. A wrong digest or unsafe path fails before credential/network access. Credential delay, streaming oversize, timeout and cancellation cannot publish successful evidence. Exact source echo and code-point-to-byte conversion hold for whitespace, CRLF and astral characters. A provider-reported zero cost is not asserted as free service. After approval, one bounded real installed-plugin smoke confirms the wire and captures an occurrence; it is not a quality benchmark.

## Idempotence and Recovery

Never silently resubmit a possibly billed interrupted request. Persist attempt identity before transmission and record uncertainty when completion is unknown. A resumed tool may adopt a fully published result for the identical plan; a new paid attempt needs explicit run policy/authority. Reject changed plans and half-written outputs. Disable the extension to roll back transport while retaining deterministic preparation and existing artifacts.

## Surprises & Discoveries

OpenRouter documents code-point offsets, not UTF-16 offsets. OptChat's old host-role guidance conflicts with its later plugin-only implementation; the later source and decision record are the precedent. Neither precedent grants Jevify permission to send code to a provider.

## Decision Log

2026-11-07, superseding owner decisions: implement and ship all proposals; Clairic defaults on for selected public/private code and review text, excluding secrets, with explicit opt-out and deterministic fallback. Credentials use the host resolver including its environment fallback. Every live experiment still requires separate approval of its exact manifest digest and spending cap. Private source leaves the machine when selected; receipts and diagnostics must not expose it.

2026-11-07, accepted implementation direction: plugin-only transport, explicit artifact bridge, host-resolved credentials, verbatim text only. Default-on is owner-approved; paid experiments remain separately gated. No host fork is required.

## Outcomes & Retrospective

Implemented `src/index.ts`, `src/extract.ts` and `src/evidence-artifacts.ts`. Run `bun run test:adapter` for transport boundaries and `bun run smoke:adapter` for package→fresh SDK→registered tool→Python adoption/controller verification. Both passed after repair; the latter used loopback only. The SDK type contract is pinned to 18.10.3 with peer range >=18.10.3 <19; Bun >=1.3.14. Linux descriptor confinement is required for transport; deterministic helpers remain available elsewhere. No real Clairic request ran, so new production wire compatibility, settled billing and quality gains are not claimed. Final release verification is recorded in the documentation index.
