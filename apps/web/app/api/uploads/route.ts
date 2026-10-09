import { handleUpload, type HandleUploadBody } from "@vercel/blob/client";
import {
  backend,
  boundedText,
  ownerSummary,
  safeError,
  session,
  webOrigin,
} from "@/lib/server";
import { sameOrigin } from "@/lib/policy.mjs";

export async function POST(request: Request) {
  if (!sameOrigin(request.headers.get("origin"), webOrigin))
    return safeError("Request origin rejected.", 403);
  try {
    const current = await session();
    if (!current.token || !(await ownerSummary(current.token)))
      return safeError("Owner sign-in required.", 401);
    const body = JSON.parse(
      await boundedText(request, 8192),
    ) as HandleUploadBody;
    if (body.type !== "blob.generate-client-token")
      return safeError("Unsupported upload operation.", 400);
    const result = await handleUpload({
      request,
      body,
      onBeforeGenerateToken: async (pathname, clientPayload) => {
        if (!clientPayload || !/^[0-9a-f-]{36}$/.test(clientPayload))
          throw new Error("Invalid receipt.");
        const response = await fetch(`${backend}/v1/uploads/${clientPayload}`, {
          headers: { Authorization: `Bearer ${current.token}` },
          cache: "no-store",
          redirect: "error",
          signal: AbortSignal.timeout(15000),
        });
        if (!response.ok) throw new Error("Upload authorization rejected.");
        const receipt = await response.json();
        if (
          receipt.pathname !== pathname ||
          receipt.storage_provider !== "vercel_blob"
        )
          throw new Error("Upload destination rejected.");
        return {
          maximumSizeInBytes: receipt.byte_size,
          addRandomSuffix: false,
          allowOverwrite: false,
          validUntil: new Date(receipt.expires_at).getTime(),
        };
      },
    });
    return Response.json(result, { headers: { "Cache-Control": "no-store" } });
  } catch {
    return safeError(
      "Unable to authorize upload. Start a new upload or check your session.",
      400,
    );
  }
}
