import { defineMiddleware } from "astro:middleware";

const BACKEND_URL: string = process.env.BACKEND_URL ?? "http://localhost:8000";

const PUBLIC_PATHS = new Set(["/login"]);

export const onRequest = defineMiddleware(async (context, next) => {
  const { pathname } = context.url;

  if (
    PUBLIC_PATHS.has(pathname) ||
    pathname.startsWith("/_astro/") ||
    pathname.startsWith("/favicon")
  ) {
    return next();
  }

  const cookieHeader = context.request.headers.get("cookie") ?? "";

  try {
    const res = await fetch(`${BACKEND_URL}/api/auth/me`, {
      headers: { Cookie: cookieHeader },
    });

    if (!res.ok) {
      return context.redirect("/login");
    }

    context.locals.user = await res.json();
  } catch {
    return context.redirect("/login");
  }

  return next();
});
