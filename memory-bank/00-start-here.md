# Memory Bank index

Read `memory-bank/.now.md` first. It is the compact operational handoff: current Git/runtime evidence, implemented work, active risks and the next bounded task.

Then open only the file relevant to the task:

| File | Use it for |
| --- | --- |
| `10-system.md` | Product boundary, current architecture and target workflow |
| `20-processing.md` | Intake, OCR, extraction, identity and scenario contracts |
| `30-integrations.md` | KAD/MKAD and SROArbitr behavior, selectors and external safety |
| `40-operations.md` | Local runtime, tests, logs, incident checks and known operational limits |
| `50-decisions.md` | Accepted high-impact contracts and their tradeoffs |

## Documentation rules

- Keep this bank compact, English-only and factual.
- Distinguish implemented, verified, simulated, target and unknown states.
- Update `.now.md` after a meaningful engineering boundary. Add stable facts to the relevant file; do not append a chat diary.
- Do not revive obsolete commands or claims from old notes. Source code and current evidence outrank documentation when they disagree; correct the document after verifying the discrepancy.
- Avoid reading `.venv/`, `.env`, `profile/`, `out/*.png`, `out/form_map*.json`, or sample images unless a task specifically requires the artifact. Do not expose personal data.
