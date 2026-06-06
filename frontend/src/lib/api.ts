/**
 * Cliente fetch compartido para todos los scripts del browser.
 *
 * - Centraliza API_URL (un único lugar para cambiarla).
 * - Añade credentials:'include' en cada petición para que el browser
 *   envíe automáticamente la cookie HttpOnly idp_token al backend.
 * - Ante 401 (sesión expirada / no autenticado) redirige a /login
 *   antes de propagar el error, evitando que el caller lo gestione.
 */

// PUBLIC_API_URL is baked into client-side bundles at build time.
// Set it via the PUBLIC_API_URL build arg / env var (e.g. http://192.168.10.201:8000/api).
export const API_URL: string =
  (import.meta.env.PUBLIC_API_URL as string | undefined) ?? "http://localhost:8000/api";

export function wsUrl(path: string): string {
  return API_URL.replace(/^http/, (m) => (m === 'https' ? 'wss' : 'ws')) + path;
}

export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    credentials: 'include',
  });

  if (res.status === 401) {
    window.location.href = '/login';
    throw new Error('Sesión expirada — redirigiendo al login');
  }

  return res;
}
