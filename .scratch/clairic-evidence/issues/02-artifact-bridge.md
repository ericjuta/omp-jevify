# Prove the installed Python/extension artifact bridge

Type: wayfinder:prototype
Status: resolved
Owner: Main
Blocked by: [Freeze evidence identity and source ownership](01-evidence-contract.md)

## Question

Can an installed skill-only package grow an optional OMP extension that safely reads a frozen request, resolves credentials through the host, writes one bounded result, and lets the Python helper validate it across fresh processes?

## Scope

Use a disposable checkout and a loopback fake provider only after implementation/prototype authorization. Verify actual installed packaging, extension registration, host credential resolver contract, path containment including symlinks, byte limits during streaming, cancellation, and atomic publication. No upstream host patch, real credentials, or live provider requests.

## Proposed direction

Agent-mediated prepare → jevify_extract → adopt, not a Python HTTP client. Endpoint/model are adapter-owned; request text cannot select credentials or destinations. One deadline includes credential lookup, network, validation and publication. Review OptChat's transport source but own a Jevify contract rather than importing its store-specific API.

## Resolution evidence

`bun run test:adapter` passed 25 transport tests. `bun run smoke:adapter` packed the candidate, loaded its registered tool through the actual SDK in a fresh isolated process, prepared/adopted evidence with packaged Python, and captured four sanitized receipts in the Python controller. Unicode/CRLF, null guesses, disabled mode, zero-transmission recovery, path refusal and interruption passed with five loopback requests and zero outbound provider requests. [ExecPlan 2](../../../plans/clairic-02-extraction-adapter.md) records SDK/engine requirements and the Linux-only transport boundary. This is not real-provider or quality evidence.
