import { ownerSummary, safeError, session } from "@/lib/server";
export async function GET() {
  try {
    const current = await session();
    if (!current.token || !(await ownerSummary(current.token)))
      return safeError("Owner sign-in required.", 401);
    return Response.json(
      {
        mode:
          process.env.STORAGE_PROVIDER === "vercel_blob" ? "direct" : "local",
      },
      { headers: { "Cache-Control": "no-store" } },
    );
  } catch {
    return safeError("Unable to check upload configuration.", 503);
  }
}
