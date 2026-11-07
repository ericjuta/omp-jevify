# Issue tracker for Clairic planning

This repository had no planning/tracker convention at the 2026-11-07 checkpoint. Use the Wayfinder local-Markdown fallback. No GitHub map or issue was created; do not imply these files are published tickets.

Maps live at `.scratch/<effort>/map.md`; decision tickets live at `.scratch/<effort>/issues/NN-<slug>.md`. Paths are stable ticket identities. Refer to a ticket by its title with a link, not a bare number. Every map has `Type: wayfinder:map`. Each ticket has exactly one of `wayfinder:research`, `wayfinder:prototype`, `wayfinder:grilling`, or `wayfinder:task`, plus `Status`, `Owner`, and `Blocked by` fields.

The frontier is open, unclaimed tickets whose blockers are resolved. Claim by updating `Status: claimed` and `Owner` before starting. Record evidence as source-read, runtime-observed, or inference. Resolve in the ticket with the decision, author, date, and evidence, then add only a gist/link to the map's Decisions so far. Owner-policy tickets are `wayfinder:grilling`; agents cannot resolve them on the user's behalf. An open decision is not implementation authorization.

The maps chart decisions. The `plans/clairic-*.md` files describe implementation milestones once the relevant decisions and implementation authority exist. Dependency links between local tickets are the canonical blocking relationships. Cross-map blockers use relative links. If the tracker later moves to GitHub, migrate both parent-child and blocking relationships and retain redirects from these files; do not maintain two competing maps.

All plans are living documents with Purpose, Progress, Context, Plan of Work, Concrete Steps, Validation and Acceptance, Idempotence and Recovery, Interfaces and Dependencies, Surprises & Discoveries, Decision Log, and Outcomes & Retrospective. Current steps are proposed, not executed. Validation must exercise the feature through its actual user-facing workflow; unit tests alone do not establish completion.
