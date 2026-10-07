"""A generic client example for a future custom agent; no reviewer is implemented."""

import os

from memory_platform.client import APIClient

with APIClient(os.environ["MEMORY_API_URL"], os.environ["MEMORY_API_TOKEN"]) as client:
    checkpoints = client.tasks(os.environ["MEMORY_SCOPE_ID"])
    print(checkpoints)
    # Explicit owner-approved skill lookup:
    skill = client.resolve_skill(os.environ["MEMORY_SKILLS_SCOPE_ID"], "/github_review")
    print({key: skill[key] for key in ("command", "version", "package_hash")})
