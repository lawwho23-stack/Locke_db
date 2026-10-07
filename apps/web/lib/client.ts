export async function request(path: string, method = "GET", body?: unknown, signal?: AbortSignal) {
  const response = await fetch(`/api/proxy/${path}`, { method, signal, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store" });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message ?? "Request failed.");
  return data;
}
export function message(failure: unknown) { return failure instanceof Error ? failure.message : "Operation failed."; }
export function encodeFile(file: File) { return new Promise<string>((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(",")[1]); reader.onerror = () => reject(new Error("Unable to read the document.")); reader.readAsDataURL(file); }); }
