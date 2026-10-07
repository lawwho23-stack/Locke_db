---
description: Retrieve the owner's approved GitHub review skill
argument-hint: [review target]
disable-model-invocation: true
---
Use shared-memory `skill_resolve` with the configured shared-skills scope and the exact command `/github_review`.
If lookup is denied, unknown, or revoked, stop and report that result. Never substitute a local draft.
Verify the returned package hash before installing files through `memory skill-install` into a new directory.
Read the approved `SKILL.md`, and apply it to the user's arguments: $ARGUMENTS.
Retrieval does not authorize executing scripts, installing dependencies, or publishing a review.
