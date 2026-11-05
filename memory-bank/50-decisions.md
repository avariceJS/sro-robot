# Active decisions

- Process private documents locally with PDF text, Tesseract, deterministic
  parsing and editable source-bearing suggestions. No Qwen integration.
- Staff have shared ordinary work access. Worker exclusivity/leases are not
  per-employee visibility or ownership rules.
- Preserve originals, request/source provenance, actor/revision history and
  uncertain results. An unknown write outcome must not become an automatic retry.
- Repeated HTTP intake requests are idempotent; new acts/independent assignments
  remain independent. A case number, hash or valid identifier alone is not a
  business-identity/role/operation proof.
- Internal cards, assignment drafts and externally persisted records are separate.
  Card creation does not automatically create an assignment; linking is explicit.
- Do not guess missing identity/address/apartment/type or conflicting OCR values.
  Human review and formal checks do not replace source ownership evidence.
- SRO actions retain their current guards. Development tests use fakes/disposable
  services; real registry Save/Send is prohibited for agents.
- Keep only main/develop; develop is the work branch. Finish each stage with
  verification, selective commit, push and GitHub read-back. Deployment is manual
  and separately authorized, even when both source branches are synchronized.
- Keep current docs compact. Completed plans, experiments, raw results and rejected
  designs belong in Git history, not a parallel archive inside the working tree.
