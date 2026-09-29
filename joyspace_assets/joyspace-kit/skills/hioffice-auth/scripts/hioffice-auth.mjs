#!/usr/bin/env node

const DEFAULT_COLOR_GATEWAY_BASE = "https://api.m.jd.com";
const DEFAULT_TENANT_CODE = "CN.JD.GROUP";
const DEFAULT_APP_ID = "JDME_DESKTOP";
const DEFAULT_HIOFFICE_PORTS = Object.freeze(
  Array.from({ length: 10 }, (_, index) => 8988 + index * 2),
);

const TENANT_CONFIG = Object.freeze({
  "CN.JD.GROUP": { teamHeaderId: "00046419", ddAppId: "ee" },
  "TH.JD.GROUP": { teamHeaderId: "00046420", ddAppId: "th.ee" },
  "ID.JD.GROUP": { teamHeaderId: "00046421", ddAppId: "id.ee" },
  "SF.JD.GROUP": { teamHeaderId: "00046422", ddAppId: "sf.ee" },
});

const usage = `
Usage:
  node scripts/hioffice-auth.mjs [options]

Options:
  --tenant-code <tenant>   Tenant code. Defaults to CN.JD.GROUP.
  --device-id <deviceId>   Device id. Defaults to "noDeviceId".

Output:
  JSON with meToken, tenantCode, authMode, and timestamp.
`;

function requireTenantConfig(tenantCode) {
  const config = TENANT_CONFIG[tenantCode];
  if (!config) {
    throw new Error(
      `Unsupported tenantCode "${tenantCode}". Expected one of ${Object.keys(TENANT_CONFIG).join(", ")}`,
    );
  }
  return config;
}

function parseArgs(argv) {
  const options = {};
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (!arg.startsWith("--")) {
      continue;
    }
    const key = arg.slice(2);
    const value = argv[index + 1];
    if (!value || value.startsWith("--")) {
      throw new Error(`Missing value for: --${key}\n${usage}`);
    }
    options[key] = value;
    index += 1;
  }
  return options;
}

async function callColorGateway(functionId, body) {
  const url = `${DEFAULT_COLOR_GATEWAY_BASE}?functionId=${encodeURIComponent(functionId)}&appid=${DEFAULT_APP_ID}`;
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      functionId,
      body,
      appid: DEFAULT_APP_ID,
    }),
  });

  if (!response.ok) {
    throw new Error(`${functionId} HTTP ${response.status} ${response.statusText}`);
  }

  return response.json();
}

async function getLegacyEncryptPayload(ddAppId) {
  const timestamp = Math.floor(Date.now() / 1000);
  const response = await callColorGateway("desk.agent.auth.encrypt", {
    content: JSON.stringify({
      method: "query",
      param: "appToken",
      timestamp: String(timestamp),
      from: process.platform === "darwin" ? "hio_plugin_joydesk_Mac" : "hio_plugin_joydesk",
      to: "HiOfficeClient",
    }),
    jdmeAppId: ddAppId,
  });

  if (response?.code !== 0 || !response?.data?.aesKey || !response?.data?.content) {
    throw new Error(response?.msg || "desk.agent.auth.encrypt failed");
  }

  return {
    aesKey: response.data.aesKey,
    content: response.data.content,
  };
}

async function queryHiOfficeAppToken({ aesKey, content }) {
  const from = encodeURIComponent(
    process.platform === "darwin" ? "hio_plugin_joydesk_Mac" : "hio_plugin_joydesk",
  );

  let lastError = null;
  for (const port of DEFAULT_HIOFFICE_PORTS) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/hioffice?from=${from}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-AES-Key": aesKey,
        },
        body: content,
      });

      if (!response.ok) {
        throw new Error(`HiOffice ${port}: HTTP ${response.status}`);
      }

      const xAesKey = response.headers.get("X-AES-Key");
      if (!xAesKey) {
        throw new Error(`HiOffice ${port}: missing X-AES-Key`);
      }

      return {
        appToken: await response.text(),
        xAesKey,
      };
    } catch (error) {
      lastError = error;
    }
  }

  throw lastError || new Error("HiOffice ports unavailable");
}

async function exchangeLegacyWebToken({ appToken, xAesKey, tenantCode, deviceId }) {
  const { ddAppId } = requireTenantConfig(tenantCode);
  const response = await callColorGateway("desk.agent.auth.getWebToken", {
    token: appToken,
    tenantCode,
    deviceUuid: deviceId,
    aesKey: xAesKey,
    jdmeAppId: ddAppId,
  });

  if (response?.code !== 0 || !response?.data?.accessToken) {
    throw new Error(response?.msg || "desk.agent.auth.getWebToken failed");
  }

  return response.data.accessToken.trim();
}

async function exchangeMeTokenViaHiOffice({ tenantCode, deviceId }) {
  const { ddAppId } = requireTenantConfig(tenantCode);
  const encrypt = await getLegacyEncryptPayload(ddAppId);
  const hiOffice = await queryHiOfficeAppToken(encrypt);
  return exchangeLegacyWebToken({
    appToken: hiOffice.appToken,
    xAesKey: hiOffice.xAesKey,
    tenantCode,
    deviceId,
  });
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const tenantCode = options["tenant-code"] || DEFAULT_TENANT_CODE;
  const deviceId = options["device-id"] || "noDeviceId";

  const meToken = await exchangeMeTokenViaHiOffice({ tenantCode, deviceId });

  const result = {
    meToken,
    tenantCode,
    deviceId,
    authMode: "legacy",
    timestamp: new Date().toISOString(),
  };

  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
}

main().catch((error) => {
  process.stderr.write(`Error: ${error.message}\n`);
  process.exit(1);
});
