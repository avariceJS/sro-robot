"""Hard release gates for workflows whose safety contracts are incomplete."""

# Keep disabled until KAD exposes a complete candidate inventory and an
# operation-specific act-selection/review boundary. Do not make this an env flag:
# a deployment setting cannot make the current latest-act shortcut safe.
KAD_ACQUISITION_ENABLED = False
