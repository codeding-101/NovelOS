export type AuthState = {
  initialized: boolean;
  authenticated: boolean;
  username: string | null;
};
export type Runtime = {
  stage: string;
  schema_revision: string;
  database: string;
  environment: string;
  workers: { id: string; state: string; online: boolean }[];
  capabilities: {
    credentials: boolean;
    task_execution: boolean;
    legacy_import: string;
    finalization: boolean;
  };
};
export type Credential = {
  id: string;
  name: string;
  provider_type: string;
  created_at?: string;
};

function csrfToken(): string {
  for (const name of ["__Host-novelos-csrf", "novelos_csrf"]) {
    const value = document.cookie
      .split("; ")
      .find((item) => item.startsWith(`${name}=`));
    if (value) return decodeURIComponent(value.slice(name.length + 1));
  }
  return "";
}

export async function platformRequest<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");
  if (init.method && !["GET", "HEAD"].includes(init.method))
    headers.set("X-CSRF-Token", csrfToken());
  const response = await fetch(`/api/v1${path}`, {
    ...init,
    headers,
    credentials: "same-origin",
    cache: "no-store",
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    if (response.status === 401 && path !== "/auth/login")
      window.dispatchEvent(new Event("novelos-session-expired"));
    throw new Error(body?.error?.message || `请求失败（${response.status}）`);
  }
  return response.status === 204 ? (undefined as T) : response.json();
}
