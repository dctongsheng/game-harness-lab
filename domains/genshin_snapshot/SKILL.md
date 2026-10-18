# Snapshot harness constitution

The frozen player is StartLux Decision, operating at skill granularity. This
domain measures selection on visible observations. It does not measure route,
battle, commission completion or reward collection in a running game.

Change generic instructions, labels and action preconditions only. Preserve
the configured model, account boundaries, skill IDs, versions and arguments.
Do not add case-specific entities, expected actions, dates, grading code, or
lookups. Resource hashes are rebuilt by trusted authoring tooling before smoke
and evaluation. Runtime loads always verify the resulting hashes.

The grader, task splits, model transport, input controller and release gates
are outside the editable directory. A snapshot winner is research evidence
only and is never eligible for client release.
