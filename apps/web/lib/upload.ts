import { upload } from "@vercel/blob/client";
import { encodeFile, request } from "./client";
import { completeDirectUpload } from "./direct-upload.mjs";
export async function uploadDocument(
  file: File,
  options: {
    scope: string;
    title?: string;
    sourceId?: string;
    expectedVersion?: number;
  },
  progress?: (message: string) => void,
) {
  if (!file.size || file.size > 20 * 1024 * 1024)
    throw new Error("Choose a document up to 20 MiB.");
  const modeResponse = await fetch("/api/upload-mode", { cache: "no-store" });
  if (!modeResponse.ok)
    throw new Error("Unable to check upload configuration. Sign in again.");
  const { mode } = await modeResponse.json();
  if (mode === "local") {
    progress?.("Reading document…");
    return request(
      options.sourceId ? `sources/${options.sourceId}` : "sources",
      options.sourceId ? "PUT" : "POST",
      {
        ...(options.sourceId
          ? { expected_version: options.expectedVersion }
          : { scope_id: options.scope, title: options.title || file.name }),
        filename: file.name,
        content_base64: await encodeFile(file),
      },
    );
  }
  if (mode !== "direct") throw new Error("Unsupported upload configuration.");
  progress?.("Authorizing upload…");
  const receipt = await request("uploads", "POST", {
    scope_id: options.scope,
    title: options.title || file.name,
    filename: file.name,
    byte_size: file.size,
    ...(options.sourceId
      ? {
          source_id: options.sourceId,
          expected_version: options.expectedVersion,
        }
      : {}),
  });
  return completeDirectUpload(
    () =>
      upload(receipt.pathname, file, {
        access: "private",
        handleUploadUrl: "/api/uploads",
        clientPayload: receipt.id,
        multipart: file.size > 4 * 1024 * 1024,
        onUploadProgress: (event) =>
          progress?.(`Uploading ${Math.round(event.percentage)}%…`),
      }),
    () => {
      progress?.("Validating document…");
      return request(`uploads/${receipt.id}/finalize`, "POST", {});
    },
  );
}
