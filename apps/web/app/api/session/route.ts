import { boundedText, ownerSummary, safeError, session, webOrigin } from "@/lib/server";
import { sameOrigin } from "@/lib/policy.mjs";

export async function GET() {
  try {
    const current = await session();
    if (!current.token) return safeError("Sign in with your owner credential.", 401);
    const data = await ownerSummary(current.token);
    return data ? Response.json(data, { headers: { "Cache-Control": "no-store" } }) : safeError("Owner credential is invalid or revoked.", 401);
  } catch { return safeError("Dashboard connection or session configuration unavailable.", 503); }
}
export async function POST(request: Request) {
  if (!sameOrigin(request.headers.get("origin"), webOrigin)) return safeError("Request origin rejected.", 403);
  try {
    const text = await boundedText(request, 2048);
    if (text.length > 2048) return safeError("Credential request too large.", 413);
    const { token } = JSON.parse(text);
    if (typeof token !== "string" || !/^mem_[A-Za-z0-9_-]+$/.test(token)) return safeError("Enter a valid owner credential.", 422);
    if (!await ownerSummary(token)) return safeError("Owner admin credential required.", 403);
    const current = await session();
    current.token = token;
    await current.save();
    return Response.json({ ok: true }, { headers: { "Cache-Control": "no-store" } });
  } catch (failure) {
    if (failure instanceof RangeError) return safeError("Credential request too large.", 413);
    if (failure instanceof SyntaxError) return safeError("Invalid credential request.", 422);
    return safeError("Dashboard connection or session configuration unavailable.", 503);
  }
}
export async function DELETE(request: Request) {
  if (!sameOrigin(request.headers.get("origin"), webOrigin)) return safeError("Request origin rejected.", 403);
  try { (await session()).destroy(); return Response.json({ ok: true }, { headers: { "Cache-Control": "no-store" } }); }
  catch { return safeError("Session configuration unavailable.", 503); }
}
