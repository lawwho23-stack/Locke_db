import "server-only";
import { cookies } from "next/headers";
import { getIronSession } from "iron-session";

export const backend = process.env.MEMORY_API_URL ?? "http://127.0.0.1:8000";
export const webOrigin = process.env.WEB_ORIGIN ?? "http://127.0.0.1:3000";
export async function session() {
  try {
    const password = process.env.SESSION_SECRET;
    if (!password || password.length < 32)
      throw new Error("SESSION_SECRET must contain at least 32 characters.");
    const origin = new URL(webOrigin);
    if (
      origin.protocol !== "https:" &&
      !["localhost", "127.0.0.1", "[::1]"].includes(origin.hostname)
    ) {
      throw new Error("HTTPS required outside local development.");
    }
    return await getIronSession<{ token?: string }>(await cookies(), {
      password,
      cookieName: "memory_owner",
      // No time expiry: the seal never expires and the cookie persists
      // (iron-session uses a very large Max-Age when ttl is 0). The owner
      // credential is still validated against the backend on every request,
      // so revoking it or rotating SESSION_SECRET ends the session.
      ttl: 0,
      cookieOptions: {
        httpOnly: true,
        secure: origin.protocol === "https:",
        sameSite: "strict",
        path: "/",
        maxAge: 2147483647,
      },
    });
  } catch (failure) {
    // Log configuration shape only; tokens, cookies and secret values never enter logs.
    let originValid = false;
    try {
      originValid = new URL(webOrigin).protocol === "https:";
    } catch {}
    console.error("Owner session unavailable", {
      secretPresent: Boolean(process.env.SESSION_SECRET),
      secretLengthValid: (process.env.SESSION_SECRET?.length ?? 0) >= 32,
      originValid,
      category: failure instanceof Error ? failure.name : "Unknown",
    });
    throw failure;
  }
}
export async function ownerSummary(token: string) {
  const response = await fetch(`${backend}/v1/dashboard/summary`, {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
    signal: AbortSignal.timeout(10000),
  });
  if (!response.ok) return null;
  const data = await response.json();
  return data.actor_kind === "owner" && data.is_admin === true ? data : null;
}
export function safeError(message: string, status: number) {
  return Response.json(
    { error: { message } },
    { status, headers: { "Cache-Control": "no-store" } },
  );
}

export async function boundedText(request: Request, maximum: number) {
  const declared = Number(request.headers.get("content-length") ?? 0);
  if (declared > maximum) throw new RangeError("Request too large.");
  const reader = request.body?.getReader();
  if (!reader) return "";
  const chunks: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.length;
    if (total > maximum) {
      await reader.cancel();
      throw new RangeError("Request too large.");
    }
    chunks.push(value);
  }
  return Buffer.concat(chunks).toString("utf8");
}
