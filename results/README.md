# Public historical evaluation records

This directory is a small, allowlisted export of real archived runs. It does not contain new measurements of the public release pipeline. Read [the report](../docs/evaluation-results.md) before comparing scores.

| File | Contents |
| --- | --- |
| summary.json | Scores, denominators, unknown counts, declared budgets, model settings and limitations |
| first30.json | Bare one-shot baseline and frozen C harness: 30 rows each |
| first30.csv | The same 60 per-task rows in long tabular format |
| first3.json | Independent A/B/C/D/mini runs, three rows each; bare first-three reference |
| first3.csv | The same 15 per-task rows in long tabular format |
| tasks-first30.json | Ordered task IDs, repository and pinned base/setup commits; no issue/test/gold content |
| provenance.json | SHA-256 of original archived summaries plus fresh file-check counts |
| SHA256SUMS | Checksums of the public data files and this README (excludes itself) |

## Reading a row

- official_status: graded means an official report exists; resolved is its boolean verdict. empty_patch, generation_invalid, and patch_rejected are known unsuccessful candidates retained in the denominator.
- stop_reason: the generation stop reason, not the grading result. Baseline stop/length are provider finish reasons; harness finish/compaction_failed are loop stop reasons; mini Submitted is its native submission.
- action_requests, summary_requests, model_requests: These count logical complete calls, not individual HTTP attempts. The frozen harness SDK has max_retries=2 for transport errors. model_reruns=false means no task-level regeneration or best-of selection, not zero SDK retries. Action and baseline requests use high reasoning effort; harness summary requests use none.
- Call breakdown: harness action and compaction calls are counted separately. Baseline has no action loop, hence null action/summary counts and exactly one model request. Mini uses native action calls and no harness summaries.
- usage.total_tokens: recorded input plus output summed across requests. cached_tokens is a subset of input, not additional tokens. No monetary cost is inferred.
- active_seconds: see active_seconds_scope; baseline request time and agent-loop time are different scopes. Generation and grading times are task durations; their sums are not batch wall-clock time.
- bindings.candidate_patch_sha256: hash of the original UTF-8 candidate patch, including the SHA-256 of empty bytes when applicable. official_patch_sha256 is null when the grader did not apply a candidate; official_report_sha256 is null when no official report exists.
- framework_log_sha256: original framework failure log for patch rejection, otherwise null. No log body or hidden test details are included.
- source_trace_sha256 and source result hashes bind summarized behavior to the original local records. The raw records are not distributed.
- evaluated_image_id: original immutable Docker image ID when a grader ran. It is an identity, not a currently retained local image or a promise that the registry tag remains unchanged.
- behavior: limited observed diagnostics. Exact repeated reads can be useful; complex shell writes are not inferred. last_patch_to_finish_rounds tracks successful dedicated patch calls only and may be null.
- reused_from_same_C_first3: C30 reused its three original C candidates and reports; it did not generate or choose better replacements.

The same official report bytes can arise for different candidate patches, so the report hash must be read together with the patch hash and task ID. C3 and C30 repeat the same three candidates: neither statistics nor resource totals should be added together as independent runs.

## Scope and reproducibility

These are prefixes after prepare_dataset sorts the pinned test split by instance_id ascending, not randomized samples or full SWE-bench Lite leaderboard submissions. A/B/C/D limits match, but actual compute differs; bare and mini budgets differ. Development iterated on these examples. Historical core commits and wrapper hashes are separated from newly published release code.

Checksums verify this lightweight package and bind it to locally archived evidence; without the omitted original logs and candidates they do not make the results independently reproducible proof. To evaluate changed code, run the documented pipeline into a fresh output directory.

On a system with sha256sum, run sha256sum -c SHA256SUMS from this directory. All files are UTF-8; CSV uses an empty cell for null. The original archive labels in provenance are logical record names, not local filesystem paths.
