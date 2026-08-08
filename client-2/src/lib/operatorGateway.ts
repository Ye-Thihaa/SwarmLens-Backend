/**
 * Server-side proxy for the console's chaos actions. Two things this
 * closes that the /console page gate alone couldn't:
 *
 * 1. "Route guards don't protect server functions" (see
 *    lib/consoleAuth.ts's own docstring) -- these RPC endpoints are
 *    reachable directly regardless of whether the caller ever loaded
 *    /console, so they re-check the session cookie themselves, on every
 *    call, not just once at page load.
 * 2. main.py's OPERATOR_TOKEN never touches the browser. If the chaos
 *    calls went straight from client JS to the FastAPI nodes (like the
 *    read-only ones in lib/api.ts still do), the token would have to
 *    live in client-side state to attach as a header -- exactly what
 *    HttpOnly cookies exist to avoid. Routing through here means only
 *    this Node process ever reads OPERATOR_TOKEN, straight from its own
 *    process.env (see server.ts's dotenv loader), and the browser only
 *    ever proves it has a valid console session, never the token itself.
 */

import { createServerFn } from "@tanstack/react-start";
import { healAllNodes, isolateNode } from "./api";
import { checkConsoleSession } from "./consoleAuth";

async function requireConsoleSession(): Promise<void> {
  const { authorized } = await checkConsoleSession();
  if (!authorized) throw new Error("Not authorized");
}

function operatorHeaders(): Record<string, string> {
  const token = process.env["OPERATOR_TOKEN"];
  return token ? { "X-Operator-Token": token } : {};
}

export const serverIsolateNode = createServerFn({ method: "POST" })
  .validator((data: unknown) => {
    if (
      typeof data !== "object" ||
      data === null ||
      !("nodeId" in data) ||
      typeof (data as { nodeId: unknown }).nodeId !== "string"
    ) {
      throw new Error("Invalid request");
    }
    return { nodeId: (data as { nodeId: string }).nodeId };
  })
  .handler(async ({ data }) => {
    await requireConsoleSession();
    await isolateNode(data.nodeId, operatorHeaders());
    return { ok: true };
  });

export const serverHealAll = createServerFn({ method: "POST" }).handler(async () => {
  await requireConsoleSession();
  await healAllNodes(operatorHeaders());
  return { ok: true };
});
