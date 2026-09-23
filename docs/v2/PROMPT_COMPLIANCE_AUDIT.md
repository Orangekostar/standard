# Prompt Compliance Audit

`python -m scripts.audit_v2_prompt` returned `PASS` against the clean release
runtime. It verified schema migrations 1-3, absence of placeholder success
markers, immutable publication binding, stock and sector coverage, persisted
formula/Jev rows, both paper accounts and their audit rows, all worker handlers,
the exact 14-file evaluation package, candidate presence, artifact hashes, and
the unopened final-test gate.

Source fidelity also passed: the archived execution prompt and frozen config
are byte-identical to the corresponding files in
`docs/standard_v2_codex_bundle.zip`. Full structured output is in
`evidence/technical_v2/be7fa3895f3013156aa0b9d5ea65f0706d0c252f/prompt_audit.json`.
