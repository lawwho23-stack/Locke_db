import { backend, boundedText, ownerSummary, safeError, session, webOrigin } from "@/lib/server";
import { sameOrigin } from "@/lib/policy.mjs";

export async function POST(request: Request) {
  if (!sameOrigin(request.headers.get("origin"), webOrigin))
    return safeError("Request origin rejected.", 403);
  try {
    const current = await session();
    if (!current.token || !(await ownerSummary(current.token)))
      return safeError("Owner sign-in required.", 401);
    const text = await boundedText(request, 8192);
    const body = JSON.parse(text);
    const params = new URLSearchParams();
    for (const key of [
      "client_id",
      "redirect_uri",
      "code_challenge",
      "scope_id",
      "scope",
      "capabilities",
      "state",
    ]) {
      if (typeof body[key] === "string" && body[key]) params.set(key, body[key]);
    }
    params.set("response_type", "code");
    params.set("code_challenge_method", "S256");
    params.set("response_mode", "json");
    const url = new URL(`${backend}/oauth/authorize`);
    url.search = params.toString();
    const response = await fetch(url, {
      headers: { Authorization: `Bearer ${current.token}` },
      cache: "no-store",
      signal: AbortSignal.timeout(30000),
    });
    const data = await response.text();
    return new Response(data, {
      status: response.status,
      headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
    });
  } catch (failure) {
    if (failure instanceof RangeError) return safeError("Request too large.", 413);
    if (failure instanceof SyntaxError) return safeError("Invalid request.", 422);
    return safeError("Memory API is unavailable.", 503);
  }
}
