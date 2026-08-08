/**
 * Real server-side gate for /console. Unlike the old client-only check
 * (any non-empty string unlocked it -- trivially bypassed by reading the
 * compiled JS), this verifies the password on the server and issues an
 * HttpOnly session cookie that route.beforeLoad checks on every request,
 * including the first SSR render -- an unauthenticated visitor never
 * receives the console's HTML or triggers its data polling at all.
 *
 * Caveat, and it's an important one: this protects the /console PAGE,
 * not the backend. The chaos/quorum/health calls this page makes go
 * straight from the browser to the FastAPI cluster (see src/lib/api.ts),
 * which has no auth of its own (see main.py) -- matches this whole
 * project's stated scope (a local demo, zero auth anywhere in the
 * backend). Someone who already knows those node URLs can still call
 * POST /chaos/partition/0 directly, bypassing this gate entirely. Real
 * protection of the cluster itself would mean adding auth to main.py,
 * which is a separate, larger change than "gate the console page."
 */

import { createServerFn } from "@tanstack/react-start";
import { getCookie, setCookie, deleteCookie } from "@tanstack/react-start/server";
import crypto from "node:crypto";

const COOKIE_NAME = "swarmlens_console_session";
const MAX_AGE_SECONDS = 60 * 60 * 8; // 8 hours

function requiredPassword(): string {
  const p = process.env["CONSOLE_PASSWORD"];
  if (!p) throw new Error("CONSOLE_PASSWORD is not set on the server (see client-2/.env)");
  return p;
}

// Session token is an HMAC of a fixed label, keyed by the password itself
// -- no separate secret to manage for a single-operator local demo.
// Anyone who can compute this already knows the password, which is fine:
// they're already authorized.
function expectedToken(): string {
  return crypto.createHmac("sha256", requiredPassword()).update("console-authorized").digest("hex");
}

function timingSafeStringEqual(a: string, b: string): boolean {
  const bufA = Buffer.from(a);
  const bufB = Buffer.from(b);
  if (bufA.length !== bufB.length) return false;
  return crypto.timingSafeEqual(bufA, bufB);
}

export const checkConsoleSession = createServerFn({ method: "GET" }).handler(async () => {
  const token = getCookie(COOKIE_NAME);
  return { authorized: !!token && timingSafeStringEqual(token, expectedToken()) };
});

export const loginToConsole = createServerFn({ method: "POST" })
  .validator((data: unknown) => {
    if (
      typeof data !== "object" ||
      data === null ||
      !("password" in data) ||
      typeof (data as { password: unknown }).password !== "string"
    ) {
      throw new Error("Invalid request");
    }
    return { password: (data as { password: string }).password };
  })
  .handler(async ({ data }) => {
    if (!timingSafeStringEqual(data.password, requiredPassword())) {
      throw new Error("Incorrect password");
    }
    setCookie(COOKIE_NAME, expectedToken(), {
      httpOnly: true,
      secure: process.env["NODE_ENV"] === "production",
      sameSite: "lax",
      path: "/",
      maxAge: MAX_AGE_SECONDS,
    });
    return { ok: true };
  });

export const logoutOfConsole = createServerFn({ method: "POST" }).handler(async () => {
  deleteCookie(COOKIE_NAME, { path: "/" });
  return { ok: true };
});
