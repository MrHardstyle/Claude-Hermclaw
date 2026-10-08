/** Bearer token handling: kept in sessionStorage only (cleared when the browser tab closes). */
const KEY = "hermclaw.apiToken";
export const UNAUTHORIZED_EVENT = "hermclaw:unauthorized";

export function getToken(): string | null {
  try {
    const t = window.sessionStorage.getItem(KEY);
    return t && t.trim() ? t : null;
  } catch {
    return null;
  }
}

export function setToken(token: string): void {
  try {
    window.sessionStorage.setItem(KEY, token.trim());
  } catch {
    /* storage unavailable (private mode) – the session simply ends with the page */
  }
}

export function clearToken(): void {
  try {
    window.sessionStorage.removeItem(KEY);
  } catch {
    /* ignore */
  }
}

export function notifyUnauthorized(): void {
  clearToken();
  window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
}
