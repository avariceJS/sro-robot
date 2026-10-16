# Role-scoped address boundary: archive validation

Evidence level: runtime. Base: `8c46c9ac9b8a8009246b92f43bbad2b45142de4d`.
Patch SHA-256: `40cd679bdb77d2ee23664161e0f03e3c4117830dab33267113acf058de22f50c`.

The baseline archive has the pinned prior role patch; the patched archive has the new full patch.
The repository's no-extra-worktree rule required archive snapshots instead of the bundled PPV runner.
Exploit and variant checks used identical side-blind assertions and printed `PPV_REACHED` before them.

| Check | Kind | Result |
| --- | --- | --- |
| 01-control | control | pass |
| 02-following-paragraph | exploit | pass |
| 03-multiline-room | variant | pass |
| 04-undelimited-address | variant | pass |
| 05-standalone-address | behavior | pass |
| 06-case-date | regression | pass |
| 07-no-address-marker | security | pass |
| 08-core-ocr-suite | suite | pass |

Assessment: complete; human review required.
