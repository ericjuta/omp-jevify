# Clairic Feature Spec — shared conversation

- Source: https://chatgpt.com/share/6aef7dad-b3cc-83ed-be32-681681a895fe?ogimg=plain
- Page title: Clairic Feature Spec
- Captured: 2026-11-07 via rendered browser; two nonempty conversation turns.
- Status: reference dump, not implementation authority and not in public documentation navigation.
- Limitations: linked library attachment `omp-jevify-clairic-feature-spec.md` was not captured; its link requires separate access. Empty source-citation links on the shared page cannot be recovered. The visible conversation below is captured in full; embedded table formatting is browser text. No attachments or downloads are included.

## User

alike ericjuta/omp-optchat, I want you to spec out feature design/improvements to the original intent/repo overall by introducing clairic to ericjuta/omp-jevify

## Assistant

Worked for 18m 18s

I’d introduce Clairic as an evidence-preparation stage in Jevify, then build stronger review, contract-checking, and migration workflows on top of it. The strongest first feature is turning unstructured review findings into source-backed cases that Jevify can judge and the agent can verify.

I’ve written the complete design, including proposed interfaces, artifact contracts, implementation milestones, and acceptance criteria:

omp-jevify-clairic-feature-spec.md

The spec is grounded in Jevify v0.4.0 at 9fe7062 and OptChat v0.16.0 at a45bb30. That distinction matters: current OptChat already contains the Clairic adapter and evidence features we discussed previously, so there is now an implemented precedent to learn from.

1. The opportunity in Jevify’s original intent

Jevify’s original purpose was to generalize rubric-based review across changed code, migration call sites, diagnostics, logs, review findings, and test pruning. Its current guidance has matured into a more useful principle: choose deterministic tools, direct reading, or repeated semantic judgment according to the task. That is the principle I would preserve. Original README
 · Current skill

The repo already does substantial work after a useful judgment unit exists:

Existing capability	What it provides
git_units, rg_units, ast_units, line_units	Extract and identify source units
jv.facts	Attach deterministic results and settle questions a rule can decide
jv.plan, jv.pack	Plan bounded requests and group without silent truncation
jv.run, jv.resume	Run judgments with receipts and recover interrupted work
jv.review_queue, jv.adjudicate	Review source-bound findings and retain decisions
Evaluation machinery	Compare methods while preserving uncertainty and evidence limits

These are existing capabilities in the helper implementation
.

The gap is preparing a good unit from messy prose.

For example, the documentation-drift recipe assumes someone has already assembled a documentation claim, its location, the relevant implementation or contract, and the supporting evidence. Review-finding triage similarly assumes the finding and its context have already been assembled. The agent still does much of this preparation manually. Existing recipes

Clairic can make that step reusable:

Locate the claim, preserve its conditions, bind it to its source, and identify what evidence is still needed before judging it.

That gives Jevify a broader product promise:

Turn a declared source inventory into reviewable decisions, with an explicit account of the evidence, method, uncertainty, and unresolved work.

This fits the original repository intent and makes more of its existing machinery useful.

2. Where Clairic belongs

Clairic has a dedicated extraction interface. Through OpenRouter, it is available as cloudflare/clairic on /api/alpha/extractions, with named typed fields and outputs that can include values, null probabilities, and source spans. The route remains alpha. 
openrouter.ai

I would divide responsibilities as follows:

Component	Responsibility
Deterministic tooling	Source inventory, parsing, exact symbol resolution, counts, arithmetic, hashes, and structural checks
Clairic	Locate candidate clauses, conditions, reported behavior, and other fields in supplied text
Existing judge_batch workflow	Evaluate suitable independent cases against the frozen rubric
Main agent or direct review	Resolve cross-file relationships, ambiguity, conflicting evidence, and final source confirmation
Existing action workflow	Apply authorized changes and verify the result

For the first release, I would support verbatim text extraction. The model offers broader typed and derived outputs, but exact text is the most useful starting point for review work because the source occurrence can be checked directly.

The important distinction is between these two statements:

“The reviewer wrote that this function always retries.”

“This function always retries.”

Clairic can help establish the first. The second requires inspecting the implementation and its governing contract.

3. The features I would build
P0 — Source-attributed field extraction

This is the foundation.

User-facing capability:

“For each finding, extract the claimed behavior, expected behavior, conditions, and cited location. Show the original evidence.”

Each result should retain:

The original record ID, assigned before extraction.

The exact source passage.

The requested field and frozen extraction instructions.

The candidate value and its source occurrence.

The relevant subject, version, or symbol identity.

An explicit result for missing, ambiguous, rejected, or failed extraction.

A model-extracted path or issue ID is a lookup hint. Code must resolve it against the declared inventory before it becomes a verified reference.

I would also give model-derived fields their own namespace, such as observations. That keeps their status visible throughout the workflow.

Why this matters: Jevify can reuse these records across review triage, documentation checks, issue intake, and migration audits. Each recipe gets a common foundation for attribution and missing evidence.

Acceptance condition: an empty field or failed provider call never makes the original record disappear from the review.

P0 — Review finding evidence assembler

This is the first complete feature I would ship.

User-facing capability:

“Take these review comments and tell me which are valid, actionable, or unsupported, with the relevant code.”

The flow would be:

Import the comments with stable identities.

Parse fields already supplied as JSON directly.

Use Clairic for the remaining prose fields.

Resolve cited paths and symbols deterministically.

Gather current code, relevant base code, and the governing contract.

Build an evidence packet.

Run the existing review rubric.

Present the result in Jevify’s existing review inbox.

A packet might contain:

Evidence role	Contents
Reviewer’s claim	Original comment and extracted claim
Qualifications	Conditions, exceptions, affected version
Current behavior	Exact current source
Prior behavior	Base source, when assessing whether the change introduced it
Governing contract	Relevant specification, API documentation, or accepted requirement
Missing context	Dependencies that could not be resolved
Judgment	Result under the frozen rubric

The current review recipe uses a finding category—valid_actionable, valid_nit, invalid, or duplicate—and a separate introduced_by_diff question. I would preserve that exact rubric for the first Clairic comparison. Splitting it into additional questions would be a separate experiment. Review recipe

This also exposes a useful improvement: missing evidence must remain an unresolved preparation outcome. It should never be forced into the existing invalid category. Likewise, deciding that a finding is a duplicate requires evidence about the related finding.

Why I would start here: it directly extends an original Jevify use case, exercises the full evidence lifecycle, and has a concrete outcome the user can assess.

P1 — Documentation and contract claim preparation

User-facing capability:

“Check the behavioral promises in these docs against this implementation.”

Use Markdown structure to create a known inventory of paragraphs or sections. Clairic then proposes contract clauses within those units.

Each candidate should preserve:

The full claim.

Its named API or subject.

Its version or scope.

Its conditions.

Its exceptions and negation.

The complete enclosing passage.

For example:

Retry 429 responses only when an idempotency key is present; never retry authentication failures.

A useful record preserves both qualifications. Extracting only “Retry 429 responses” would change the claim.

The rest of the workflow uses deterministic symbol resolution and the existing alignment rubric to identify supported, contradicted, stale, or insufficient claims.

There are two separate coverage questions here:

Did we process every declared source section?

Did we identify every relevant semantic claim within those sections?

The first can be accounted for directly. The second needs independent evaluation and may remain uncertain.

Why this is valuable: it turns the current documentation-drift recipe into a workflow the agent can run without handcrafting every unit, while keeping the implementation evidence visible.

P1 — Required-evidence checks and bounded context expansion

User-facing capability:

“Tell me what is missing before asking the judge to decide.”

Extend the idea behind jv.facts(requires=...) into an evidence contract.

For a migration review, required evidence might include:

The call site.

The resolved callee.

The old and new API contracts.

A relevant consumer.

A wrapper or alias implementation.

For an introduced-by-diff judgment, both base and current source may be necessary.

The preparation stage should check that these evidence roles are present and properly bound. When a named dependency is missing, a bounded resolver can fetch it through search, AST, or LSP facilities.

Clairic can supply candidate identifiers. Code controls which lookups are allowed and how far expansion proceeds.

This produces useful outcomes such as:

Ready for judgment.

Missing governing contract.

Wrapper unresolved.

Version mismatch.

Conflicting evidence.

Expansion budget exhausted.

Those outcomes are more informative than a low-confidence judgment over an incomplete snippet.

Acceptance condition: an unresolved wrapper never becomes a “migration complete” verdict merely because the visible call was renamed.

P1 — Explainable review and dependency-aware refresh

User-facing capability:

“Why was this flagged, and what needs to be checked again after the code changes?”

The existing inbox should show the chain from source to conclusion:

Original claim.

Extracted occurrence and qualifications.

Deterministic checks.

Supporting source.

Exact rubric question.

Judgment and uncertainty.

Reviewer confirmation or overturn.

Then track the dependencies of that conclusion.

A judgment may depend on a contract document, a helper, and a consumer even when its primary unit is one function. If the contract changes, the result may be stale despite the function remaining unchanged.

Reuse should depend on the relevant source bytes, extraction schema, policy, and judge inputs. Equal extracted strings alone are insufficient.

Every refresh must also re-enumerate the declared scope so newly added or removed units are noticed.

Why this improves the repo overall: it makes repeated review cheaper to navigate and more reliable to resume, while extending the source-bound review model Jevify already has.

P2 — OptChat evidence import

This is where the two repositories could complement each other.

User-facing capability:

“Use the decision we discussed earlier as context for this implementation review.”

OptChat supplies selected passages with their store identity, message identity, offsets, snapshot boundary, and completeness. Jevify consumes those passages as external evidence.

The handoff must preserve whether the source was:

A user decision.

An assistant proposal.

A tool report.

A historical discussion whose status remains unresolved.

That distinction determines what the passage can support.

I would use an explicit evidence interchange contract. OptChat continues to own conversation retention and historical interpretation; Jevify owns the review scope and its conclusions.

The same foundation can later support issue intake and log review, with fields for reported symptoms, environment, observed version, reproduction steps, and missing information.

4. The architecture I would use

I would follow the plugin-only integration now implemented in OptChat.

Its current adapter uses OpenRouter, obtains the key through OMP’s credential resolver, and bounds the request and response. The original host-extraction-role proposal was superseded in the project’s recorded decisions. Implemented adapter
 · Recorded decision

For Jevify, I would add an optional TypeScript extension alongside the Python helper.

Mermaid
flowchart TD
    A["Declared source inventory"] --> B["Deterministic preparation"]
    B --> C{"Method needed"}
    C -->|Rule decides| D["Deterministic result"]
    C -->|Locate prose evidence| E["Clairic candidates"]
    C -->|Evidence already prepared| G["Rubric judgment"]
    C -->|Dependent reasoning| H["Direct review"]
    E --> F["Validate and bind sources"]
    F -->|Ready| G
    F -->|Missing or conflicting| H
    D --> I["Scope ledger and review inbox"]
    G --> I
    H --> I
Python owns the workflow

The existing helper should continue to own:

Unit preparation.

Frozen schemas and rubrics.

Deterministic checks.

Coverage.

Judgment orchestration.

Review and reporting.

The extension owns the extraction call

A proposed tool, jevify_extract, would:

Read a prepared request artifact.

Verify its plan and source digests.

Resolve credentials through OMP.

Make bounded extraction requests.

Save results and an extraction-stage receipt.

Return artifact references and a concise summary.

The main agent calls that tool between Python eval steps. This avoids assuming the eval kernel already exposes a native extraction primitive.

The proposed API sequence in the full spec is:

Proposed interface	Purpose
jv.prepare_evidence(...)	Create the frozen request plan without a network call
jevify_extract(...)	Execute the optional provider stage
jv.adopt_evidence(...)	Validate and import the resulting artifact
jv.evidence_states(...)	Build judge inputs while preserving original evidence
Existing jv.run(...)	Judge under the bound manifest and rubric

These names are proposed interfaces.

The bridge needs a declared artifact root and a frozen plan digest. The adapter owns the endpoint and credential selection; source text cannot alter them. Results should be published atomically to the correct run.

I would reuse OptChat’s transport design and validation fixtures, but give Jevify its own versioned evidence contract. OptChat’s current extractor is a narrow, internal, single-text-field API tied to its message representation. Adapter and types

5. The code-level changes that make this design work

Several details distinguish this from simply wiring an API call into the skill.

Keep deterministic facts and model-derived observations distinct

jv.facts currently lets deterministic checks settle questions. That contract is useful and should remain clear. Implementation

If Clairic selects a value and a script subsequently compares it with a threshold, the conclusion still depends on the model’s selection. Calling the final comparison deterministic does not remove that dependency.

I would retain question-level provenance so the report can distinguish:

A rule established the answer directly.

A rule evaluated a model-selected candidate.

A judge evaluated supplied evidence.

A reviewer confirmed the source.

Track every supporting source explicitly

Current jv.extraction binds primary unit files and files named by independent source hits. It does not recursively discover every supporting file embedded in arbitrary unit data. Manifest implementation

The new evidence registry should explicitly list and hash every consulted contract, helper, consumer, and source version.

This requires a manifest extension and broader dependency tracking. Supporting files should have their own role; adding them as fake “independent hits” would confuse evidence dependencies with coverage checks.

Bridge sub-line extraction to full-line review citations

Clairic’s text spans use Unicode code-point offsets. Jevify’s existing review machinery expects line-addressable source, while the strict findings validator requires quotes equal to complete cited lines. 
openrouter.ai
 Findings validator

For the first release, I would materialize imported comments and consulted source versions into immutable snapshot files:

Preserve the original record identity and raw content digest.

Preserve the mapping to exact bytes.

Supply the inbox’s required file and line fields.

Keep Clairic’s narrower occurrence separately.

Track live repository origins so unchanged snapshot files cannot hide later source changes.

Imported comments and evidence snapshots should never become deletion targets.

Preserve null and uncertainty properly

A null value must remain an abstention, even if the response contains a plausible guess and high confidence. Cloudflare’s own documented output demonstrates this combination. 
Cloudflare Workers AI docs

Admission should require:

A non-null value.

Valid required metadata.

The frozen per-field null threshold.

A valid source occurrence.

Appropriate subject binding.

Even then, the output remains a candidate observation until its semantic use is established.

Preserve the strict grading boundary

Jevify’s current structured findings validator checks exact claim IDs, source revisions, allowed labels, and source quotes. Validator

Clairic should not infer missing claim IDs, repair malformed evaluated answers, or attribute nearby prose to a claim so the grader can award credit.

Explicitly importing an external reviewer’s prose is a product operation. Reconstructing an evaluated answer to improve its score is a different operation. The spec keeps that boundary intact.

Make completeness an end-to-end property

Each stage needs its own accounting:

Records inventoried.

Extraction requests attempted and completed.

Required fields obtained or unresolved.

Evidence packets ready.

Questions answered.

Sources confirmed.

A completed extraction batch does not imply a completed review.

Likewise, a unit is only resolved once every required question has a valid disposition. One answered facet cannot complete the whole unit.

This makes a result such as “all records accounted for; four still unresolved” meaningful and honest.

6. What the OptChat implementation teaches us

The most useful precedent is its treatment of successful extraction.

Current OptChat verifies source echo, offsets, exact value equality, and subject identifiers, then still presents the result as an unverified candidate occurrence. That is the right foundation for Jevify. Current extraction logic

Its retained evaluations also matter. The project records synthetic cases where deterministic retrieval and retrieval plus Clairic achieved the same grounded and abstention outcomes. It separately records a small real-chat check where Clairic selected a plausible value for the wrong version after retrieval missed the relevant passage. Those are repository-recorded results, not a fresh benchmark from this review. Evaluation record

The design consequence is straightforward:

Clairic must be evaluated against the same workflow with deterministic preparation alone.

Otherwise, better retrieval, stronger source mapping, or a clearer rubric can produce a gain that gets incorrectly attributed to extraction.

7. How I would evaluate adoption

I would use four arms:

Arm	What it establishes
A — Existing workflow	Current end-to-end performance
B — Deterministic evidence preparation	Benefit from improved inventory, retrieval, and structure
C — B plus Clairic	Incremental contribution from extraction
D — Direct reading on suitable tasks	Whether the workflow justifies its overhead

For B versus C, hold constant:

Source inventory and retrieved passages.

Rubric.

Judge configuration.

Fallback policy.

Evaluation cases.

Scope and completion rules.

Change the extraction treatment alone.

The present pilot machinery does not already express this comparison: its method recommendation gate expects a strategy change from a fixed method set. I would add a versioned preparation-treatment comparison rather than weaken the existing comparability checks. Runner
 · Comparison gate

Measure the whole outcome

Extraction accuracy is only one part. Measure:

Correct source occurrence.

Correct subject attribution.

Condition and exception retention.

Missing-claim rate.

Appropriate abstention.

Final decision quality.

Unsafe recommendations.

Unresolved rate.

Reviewer overturns.

End-to-end latency and observed cost.

Actual review time needs to be measured before claiming reduced review burden.

Clairic is currently listed at $0.10 per million input tokens, but that price alone says little about total workflow cost. Repeated passes, schema overhead, judgment, retries, and fallback review all matter. 
OpenRouter

The challenge set should deliberately include nearby wrong IDs, reused symbol names, old versions, wrappers, exceptions, negation, repeated text, Unicode, stale files, interrupted requests, and null answers accompanied by plausible guesses.

The adoption criterion: C should provide a useful improvement over B in quality, measured preparation effort, review effort, or total cost and latency while meeting the same quality requirements.

8. The implementation sequence I recommend
Milestone	Deliverable	Completion condition
1. Evidence contracts	Source registry, immutable captures, validators, dependency tracking, scope ledger	An imported review comment reaches the existing inbox with valid citations
2. Optional Clairic adapter	OMP extension, artifact bridge, bounded requests, stage receipts	A real installed plugin produces a verified occurrence and preserves evidence on failure
3. Review intake workflow	Reference resolution, evidence packets, readiness checks, inbox integration	Every declared finding receives a clear disposition or explicit unresolved state
4. Controlled evaluation	A/B and B/C comparisons	Clairic’s contribution can be separated from preparation improvements
5. Additional recipes	Documentation alignment, bounded context expansion, OptChat import	Conditions, source identity, and authority survive the complete workflow

Across the repository, I would keep the main skill concise and put detailed recipes and API contracts in the on-demand reference. The extension and evidence schema justify new modules; a wholesale refactor of the existing Python helper would add review work before the first feature proves useful.

My first implementation target would be:

“Give Jevify a set of review findings, and get back source-backed cases showing what each claim says, what the code establishes, what remains missing, and which conclusions have been confirmed.”

That exercises Clairic where it can contribute, makes Jevify’s existing review machinery easier to use, and creates a foundation for documentation and migration audits without losing the repo’s method-first discipline.

If you want, I can:

Draft next integration steps for Clairic in Jevify

Propose evaluation experiment details

Outline API and interface design for jevify_extract tool

Sources

## Resolved links from rendered page

- omp-jevify-clairic-feature-spec.md: https://chatgpt.com/library?file_id=libfile_cdc245c8a6e881918135f171d37ee5a6&account_id=personal
- openrouter.ai: https://openrouter.ai/docs/guides/community/extractions?utm_source=chatgpt.com
- openrouter.ai: https://openrouter.ai/docs/guides/community/extractions?utm_source=chatgpt.com
- Cloudflare Workers AI docs: https://developers.cloudflare.com/workers-ai/models/clairic/?utm_source=chatgpt.com
- OpenRouter: https://openrouter.ai/cloudflare/clairic?utm_source=chatgpt.com
