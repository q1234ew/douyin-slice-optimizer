export async function api<T = Record<string, unknown>>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, options);
  const text = await response.text();
  let data: unknown = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { raw: text };
  }
  if (!response.ok) {
    const payload = data as { detail?: unknown; raw?: unknown };
    const detail = payload.detail;
    if (typeof detail === "string") {
      throw new Error(detail);
    }
    if (detail) {
      throw new Error(JSON.stringify(detail));
    }
    throw new Error(String(payload.raw || response.statusText));
  }
  return data as T;
}

export function jsonBody(payload: unknown): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  };
}
