# Validation attempt dispositions

The initial post-patch run was incomplete because the isolated checkout used the system Python without project dependencies; the helper could not import `app`, and unit discovery could not import `starlette`. The rerun pinned the existing local virtualenv interpreter and added the checkout root to the helper import path.

The second run was incomplete because its benign control deliberately supplied a different message from the legacy event output, and the suite's drill-cleanup tests expected `.venv/bin/python` inside each checkout. The control now supplies the existing canonical duplicate message, and the suite receives `SRO_ROBOT_PYTHON` pointing to the pinned local interpreter. The final `results-verified` run is complete with no findings or gaps. The incomplete raw streams remain preserved for diagnosis.

The separate Stage 1 full verifier also had one command-construction failure before it started: the local `mktemp` requires its `XXXXXX` template at the end. It created no test output; the corrected command ran to completion. The final verifier log is outside Git at `/tmp/sro-stage1-audit-contract-full.AP5AVQ`, SHA-256 `81460142fda0eec603155f16ef4c8079f480c7c1e31fa5ec2ecb14697ffcf61c`.

A later attempt to compact the patch with zero context was refused before any checks ran because the validator requires a normally applicable unified diff. The repository rule retains the complete validated patch unchanged; Git whitespace checks now exempt the exact nested patch-artifact paths because unified-diff context spaces are data, not source whitespace.
