---
status: accepted
---

# Review storyboard semantics before accepting generated prompts

## Decision

After deterministic prompt validation, storyboard generation and rewriting call a shared semantic reviewer through the existing configured text generator. Prompt authoring uses the same reviewer before committing the visual layer.

The reviewer checks grounded contradictions in character posture, spatial relations and prop state, plus ambiguous explicitly marked keyframes. It must return every submitted scene exactly once. Findings identify camera shots, quote their original text and request a minimal repair. The application validates scene coverage, shot indexes and verbatim evidence before consuming findings.

Dynamic actions such as a light turning on and then off are valid. Foreground/background descriptions within a medium shot are valid. Marker placement alone does not cause rejection. The reviewer must not reject reasonable omissions or invent unmentioned state changes.

Findings enter the existing bounded repair loop. Malformed, incomplete or ungrounded review responses do not permit a commit. Provider failures propagate rather than silently bypassing review. Successful deterministic time-axis validation remains required.

## Scope

- Initial short-drama storyboard generation, including the mechanical planning path.
- Selected storyboard rewriting.
- Visual prompt authoring and its local repair loop.
- A new plan-cache namespace prevents plans from before this gate being reused as reviewed plans.

Explicitly preserved current scripts are not regenerated or retroactively rewritten. Legacy persisted scripts are not certified by this gate.

## Tradeoffs

Each otherwise valid generation attempt requires an additional text-model call, using existing model routing and usage accounting. This increases latency and token cost. Semantic model judgments remain probabilistic; quoting evidence guards against invented citations but cannot prove the judgment correct.

The reviewer does not currently compare neighboring scene boundaries. It checks all camera shots within each submitted scene. Existing deterministic handoff rules remain responsible for scene boundaries.

## Verification

Python lint and targeted Python type analysis; no tests are run or edited under repository policy. No live provider calls are needed for static verification.
