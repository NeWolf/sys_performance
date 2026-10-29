#!/usr/bin/env node
/**
 * Shared auth utility for joyspace-kit.
 * Resolves the path to hioffice-auth.mjs regardless of install location:
 *   - npm global:  <prefix>/lib/node_modules/@jd/joyspace-kit/skills/...
 *   - local node_modules: ./node_modules/@jd/joyspace-kit/skills/...
 *   - dev:  <repo>/skills/...
 */

import { spawnSync } from "node:child_process";
import { performance } from "node:perf_hooks";
import { authProcess } from "./safe-diagnostics.mjs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const authScript = path.join(__dirname, "hioffice-auth", "scripts", "hioffice-auth.mjs");

/**
 * Get a me_token by invoking hioffice-auth.
 * @returns {{ meToken: string, me_token: string }}
 */
export function getMeToken() {
  let started;
  let status = "error";
  try {
    started = performance.now();
    authProcess("begin");
  } catch { /* Diagnostic timing must not prevent authentication. */ }
  try {
    const res = spawnSync(process.execPath, [authScript], { encoding: "utf8", shell: false, timeout: 45000 });
    if (res.error?.code === "ETIMEDOUT") status = "timeout";
    if (res.error || res.status !== 0) throw new Error("auth_process_error");
    const tok = JSON.parse(res.stdout);
    const meToken = tok?.meToken || tok?.me_token;
    if (typeof meToken !== "string" || !meToken.trim()) {
      throw new Error("auth_process_error");
    }
    status = "ok";
    return { meToken, me_token: meToken };
  } catch {
    // Never forward subprocess output, parsed values, paths or raw exceptions.
    throw new Error(status === "timeout" ? "timeout" : "auth_process_error");
  } finally {
    try {
      if (Number.isFinite(started)) authProcess(status, performance.now() - started);
    } catch { /* Final diagnostics must preserve the result or sanitized error. */ }
  }
}

export const JOYSPACE_API_BASE = "https://apijoyspace.jd.com";
export const TEAM_HEADER_ID = "00046419";

export function joyHeaders(meToken) {
  return {
    Accept: "application/json",
    "Content-Type": "application/json",
    Cookie: `me_token=${meToken}`,
    "x-team-id": TEAM_HEADER_ID,
    "User-Agent": "Mozilla/5.0 (joyspace-kit)",
  };
}
