import {
  backend,
  boundedText,
  ownerSummary,
  safeError,
  session,
  webOrigin,
} from "@/lib/server";
import { allowedPath, sameOrigin } from "@/lib/policy.mjs";

export const maxDuration = 300;

type Context = { params: Promise<{ path: string[] }> };
async function proxy(request: Request, context: Context) {
  const { path } = await context.params;
  const selected = path.join("/");
  if (!allowedPath(selected, request.method))
    return safeError("Unsupported dashboard operation.", 404);
  if (
    request.method !== "GET" &&
    !sameOrigin(request.headers.get("origin"), webOrigin)
  )
    return safeError("Request origin rejected.", 403);
  try {
    const current = await session();
    if (!current.token || !(await ownerSummary(current.token)))
      return safeError("Owner sign-in required.", 401);
    let body: string | undefined;
    if (request.method !== "GET" && request.method !== "DELETE") {
      const maximum =
        selected === "sources" || selected.startsWith("sources/")
          ? 29 * 1024 * 1024
          : 64 * 1024;
      body = await boundedText(request, maximum);
    }
    const url = new URL(`${backend}/v1/${selected}`);
    url.search = new URL(request.url).search;
    const response = await fetch(url, {
      method: request.method,
      body,
      headers: {
        Authorization: `Bearer ${current.token}`,
        "Content-Type": "application/json",
      },
      cache: "no-store",
      signal: AbortSignal.timeout(
        selected === "jobs/process" || selected.endsWith("/finalize")
          ? 270000
          : 30000,
      ),
      redirect: "error",
    });
    return new Response(await response.text(), {
      status: response.status,
      headers: {
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
      },
    });
  } catch (failure) {
    if (failure instanceof RangeError)
      return safeError("Upload exceeds its size limit.", 413);
    return safeError(
      "Memory API is unavailable. Check the backend connection.",
      503,
    );
  }
}
export {
  proxy as GET,
  proxy as POST,
  proxy as PATCH,
  proxy as DELETE,
  proxy as PUT,
};
