# Role-bound extraction: archive validation

Evidence level: runtime. Base: `15138ee`.
Patch SHA-256: `ba7fe8f207c2745a2546a2b3045719a5f122db32ae00a97c240af0a06cfbc9a6`.

The repository's no-extra-worktree rule required archive snapshots instead of the bundled PPV runner.
Exploit and variant checks used identical side-blind assertions and printed `PPV_REACHED` before them.

| Check | Kind | Result |
| --- | --- | --- |
| 01-control | control | pass |
| 02-blank-inn | exploit | pass |
| 03-unparsed-address-warning | variant | pass |
| 04-blank-address-corroboration | variant | pass |
| 05-ocr-wrapped-address | variant | pass |
| 06-standalone-address | behavior | pass |
| 07-real-ocr-absence | regression | pass |
| 08-other-person-boundary | security | pass |
| 09-core-ocr-suite | suite | pass |

Assessment: complete; human review required.
