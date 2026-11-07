# Approve provider transmission and experiment authority

Type: wayfinder:grilling
Status: resolved
Owner: unassigned
Blocked by: none

## Question for owner

May this repo transmit explicitly selected review/source text to Clairic through OpenRouter, under which opt-in and spending boundaries?

## Historical recommendation (superseded)

Keep extraction explicit opt-in, use only the host's OpenRouter credential resolver, and keep deterministic preparation available with no key. No plugin-owned key environment/config path. Record whether the host's credential cascade may use environment keys; OptChat's acceptance is not automatically a Jevify decision. Require a separately approved frozen manifest digest and total cap before live evaluation; do not copy OptChat's $5 or $50 grants. Zero or missing usage cost is unknown unless settled evidence supports otherwise.

## Historical alternatives

Deterministic-only release first; explicit opt-in adapter after a capped experiment; default-on only after separately accepted privacy and quality/cost evidence. Default-on had not been approved at proposal time; the superseding owner decision below now governs.

## Resolution

**Superseding owner decision (2026-11-07):** Clairic is enabled by default for selected evidence-preparation inputs, with explicit opt-out and deterministic fallback. This supersedes the earlier explicit-opt-in/default-off policy below. Selected public/private source remains permitted, secrets excluded, and credentials remain host-resolved (environment fallback allowed; no plugin-owned keys). Implementation is now authorized by the owner's go-ahead. Paid experiments still require separate exact-manifest digest and spending-cap approval. The subsequent owner request to “ship all your proposals” authorizes verified commits, push, release, and installation. Other planning documents' opt-in statements describe the superseded decision and must be updated during implementation.

**Initial decision (owner, 2026-11-07; superseded):** Explicit opt-in; host credentials. Permit explicitly selected code/review text, including private source, excluding secrets. Resolve OpenRouter credentials through the host, including its environment-key fallback; no plugin-owned keys. Keep deterministic preparation available. Live experiments require separate approval of the exact manifest digest and spending cap. This policy decision does not authorize implementation, provider calls now, or release. Related: [controlled evaluation plan](../../../plans/clairic-04-controlled-evaluation.md).
