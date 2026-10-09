import test from "node:test";
import assert from "node:assert/strict";
import { agentSetup } from "../lib/agent-setup.mjs";

const scope = "00000000-0000-4000-8000-000000000001";
test("client configurations use environment references, scoped prompts and HTTPS", () => {
  for (const client of ["claude", "codex", "opencode", "custom"]) {
    const kit = agentSetup(client, scope, ["memory:read", "memory:write"]);
    assert.ok(kit.config.includes("https://locke-db-api.vercel.app/mcp"));
    if (client === "opencode") {
      // One-click OAuth: no token copy-paste; the client opens the login link.
      assert.ok(kit.config.includes('"oauth": true'));
      assert.ok(!kit.config.includes("LOCKE_AGENT_TOKEN"));
    } else {
      assert.ok(kit.config.includes("LOCKE_AGENT_TOKEN"));
    }
    assert.ok(kit.prompt.includes(scope));
    assert.ok(kit.prompt.includes("idempotency_key"));
    assert.ok(kit.prompt.includes("memory_update"));
    assert.ok(!kit.prompt.includes("Use memory_forget"));
    if (client !== "codex") JSON.parse(kit.config);
  }
});
test("read-only credentials get no write instructions", () => {
  const kit = agentSetup("claude", scope, ["memory:read"]);
  assert.ok(kit.prompt.includes("Read only"));
  assert.ok(!kit.prompt.includes("Use memory_remember"));
  assert.ok(!kit.prompt.includes("Use memory_update"));
});
