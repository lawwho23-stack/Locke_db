import test from "node:test";
import assert from "node:assert/strict";
import * as flow from "../lib/direct-upload.mjs";

test("a lost upload response still finalizes the original receipt", async () => {
  let stored = false;
  const receipt = "original-receipt";
  const completed = await flow.completeDirectUpload(
    async () => {
      stored = true;
      throw new Error("Lost upload response");
    },
    async () => {
      assert.equal(stored, true);
      return { receipt, version: 1 };
    },
  );
  assert.equal(completed.receipt, receipt);
  assert.equal(completed.version, 1);
});

test("a lost finalization response retries publication without uploading again", async () => {
  let uploaded = 0,
    finalized = 0;
  const result = await flow.completeDirectUpload(
    async () => {
      uploaded++;
    },
    async () => {
      finalized++;
      if (finalized === 1) throw new Error("Lost finalization response");
      return { version: 1 };
    },
  );
  assert.equal(uploaded, 1);
  assert.equal(finalized, 2);
  assert.equal(result.version, 1);
});

test("document validation failures are not retried", async () => {
  let finalized = 0;
  await assert.rejects(
    () =>
      flow.completeDirectUpload(
        async () => {},
        async () => {
          finalized++;
          throw Object.assign(new Error("Unsupported document"), {
            status: 422,
          });
        },
      ),
    /Unsupported document/,
  );
  assert.equal(finalized, 1);
});
