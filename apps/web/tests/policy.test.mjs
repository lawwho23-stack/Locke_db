import { test } from "node:test";
import assert from "node:assert/strict";
import { allowedPath, sameOrigin } from "../lib/policy.mjs";
test("proxy permits intentional owner operations and refuses skill import and traversal", () => {
  for (const path of ["skills", "../credentials", "memories/x", "dashboard/../credentials"]) {
    assert.equal(allowedPath(path, "POST"), false);
  }
  assert.equal(allowedPath("skills/resolve", "GET"), true);
  assert.equal(allowedPath("sources", "POST"), true);
});
test("writes require exact configured origin", () => {
  assert.equal(sameOrigin(null, "http://localhost:3000"), false);
  assert.equal(sameOrigin("https://evil.test", "http://localhost:3000"), false);
  assert.equal(sameOrigin("http://localhost:3000", "http://localhost:3000"), true);
});

test("credential and relation operations have exact methods and paths", () => {
  const id = "12345678-1234-1234-1234-123456789012";
  assert.equal(allowedPath("credentials", "POST"), true);
  assert.equal(allowedPath(`credentials/${id}`, "DELETE"), true);
  assert.equal(allowedPath("credentials", "GET"), false);
  assert.equal(allowedPath(`credentials/${id}`, "PATCH"), false);
  assert.equal(allowedPath("relations", "POST"), true);
  assert.equal(allowedPath(`relations/${id}`, "DELETE"), true);
  assert.equal(allowedPath(`sources/${id}`, "PUT"), true);
  assert.equal(allowedPath("graph", "GET"), true);
  assert.equal(allowedPath("usage", "GET"), true);
});

test("hosted uploads and processing allow only the intended interfaces", () => {
  const id = "12345678-1234-1234-1234-123456789012";
  assert.equal(allowedPath("uploads", "POST"), true);
  assert.equal(allowedPath(`uploads/${id}`, "GET"), true);
  assert.equal(allowedPath(`uploads/${id}/finalize`, "POST"), true);
  assert.equal(allowedPath("jobs/process", "POST"), true);
  assert.equal(allowedPath("internal/cron", "GET"), false);
  assert.equal(allowedPath(`uploads/${id}`, "DELETE"), false);
});
