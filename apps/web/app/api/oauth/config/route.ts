import { backend, safeError } from "@/lib/server";

export async function GET() {
  try {
    const response = await fetch(`${backend}/oauth/config`, {
      cache: "no-store",
      signal: AbortSignal.timeout(10000),
    });
    const data = await response.json();
    return Response.json(data, {
      status: response.status,
      headers: { "Cache-Control": "no-store" },
    });
  } catch {
    return safeError("Authentication configuration unavailable.", 503);
  }
}
