const uuid = "[0-9a-fA-F-]{36}";
/** No generic proxy: each allowed method and path is intentional. */
export function allowedPath(path, method) {
  if (method === "GET")
    return [
      /^dashboard\/(summary|activity|graph|connections)$/,
      /^(scopes|memories|sources|tasks|sessions|skills|graph|usage)$/,
      new RegExp(`^(memories|sources|tasks|sessions|uploads)/${uuid}$`),
      new RegExp(`^tasks/${uuid}/events$`),
      new RegExp(`^jobs/${uuid}$`),
      /^skills\/resolve$/,
      new RegExp(`^skills/${uuid}/versions/[1-9][0-9]*$`),
    ].some((pattern) => pattern.test(path));
  if (method === "PATCH") return new RegExp(`^memories/${uuid}$`).test(path);
  if (method === "DELETE")
    return new RegExp(
      `^(memories|sources|credentials|relations)/${uuid}$`,
    ).test(path);
  if (method === "PUT") return new RegExp(`^sources/${uuid}$`).test(path);
  if (method === "POST")
    return (
      [
        "sources",
        "credentials",
        "relations",
        "uploads",
        "jobs/process",
      ].includes(path) ||
      new RegExp(`^uploads/${uuid}/finalize$`).test(path) ||
      new RegExp(`^skills/${uuid}/revoke$`).test(path)
    );
  return false;
}
export function sameOrigin(origin, expected) {
  if (!origin) return false;
  try {
    return new URL(origin).origin === new URL(expected).origin;
  } catch {
    return false;
  }
}
