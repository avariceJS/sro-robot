# Role-bound extraction: archive validation

Evidence level: runtime. Base: `8c46c9ac9b8a8009246b92f43bbad2b45142de4d`.
Patch SHA-256: `dc44849f63ce9228d1469fcc6337c513b9d63ce8eadd6fc1290b082fb526cbc8`.

The repository's no-extra-worktree rule required archive snapshots instead of the bundled PPV runner.
Exploit and variant checks used identical side-blind assertions and printed `PPV_REACHED` before them.

| Check | Kind | Result |
| --- | --- | --- |
| 01-control | control | pass |
| 02-original-wrong-role | exploit | pass |
| 03-ocr-identity-conflict | variant | pass |
| 04-declined-name | variant | pass |
| 05-address-behavior | behavior | pass |
| 06-case-date-regression | regression | pass |
| 07-organization-security | security | pass |
| 08-core-ocr-suite | suite | pass |

Assessment: complete; human review required.
