#!/usr/bin/env node
/**
 * Shared auth utility for joyspace-kit.
 * Resolves the path to hioffice-auth.mjs regardless of install location:
 *   - npm global:  <prefix>/lib/node_modules/@jd/joyspace-kit/skills/...
 *   - local node_modules: ./node_modules/@jd/joyspace-kit/skills/...
 *   - dev:  <repo>/skills/...
 */

import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const authScript = path.join(__dirname, "hioffice-auth", "scripts", "hioffice-auth.mjs");

/**
 * Get a me_token by invoking hioffice-auth.
 * @returns {{ meToken: string, me_token: string }}
 */
export function getMeToken() {
  const res = spawnSync(process.execPath, [authScript], { encoding: "utf8", shell: false, timeout: 45000 });
  if (res.status !== 0) {
    throw new Error("hioffice-auth failed: " + (res.stderr || res.stdout));
  }
  let tok;
  try {
    tok = JSON.parse(res.stdout);
  } catch {
    throw new Error("hioffice-auth returned non-JSON: " + res.stdout.slice(0, 300));
  }
  const meToken = tok.meToken || tok.me_token;
  if (!meToken) {
    throw new Error("hioffice-auth response missing meToken: " + JSON.stringify(tok));
  }
  return { meToken, me_token: meToken };
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
