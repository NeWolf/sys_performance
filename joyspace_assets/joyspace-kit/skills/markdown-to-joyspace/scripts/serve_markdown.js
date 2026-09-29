import http from "node:http";
import fs from "node:fs/promises";
import { realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";

/**
 * Start a read-only HTTP server that serves one markdown file for JoySpace page fetches.
 */
export async function createMarkdownServer({
  filePath,
  host = "127.0.0.1",
  port = 8765,
  allowedOrigin = "https://joyspace.jd.com",
}) {
  if (!filePath) {
    throw new Error("filePath is required");
  }

  const markdown = await fs.readFile(filePath, "utf8");

  const server = http.createServer((req, res) => {
    res.setHeader("Access-Control-Allow-Origin", allowedOrigin);
    res.setHeader("Access-Control-Allow-Credentials", "true");
    res.setHeader("Access-Control-Allow-Private-Network", "true");
    res.setHeader("Access-Control-Allow-Headers", "*");
    res.setHeader("Access-Control-Allow-Methods", "GET,OPTIONS");

    if (req.method === "OPTIONS") {
      res.writeHead(204);
      res.end();
      return;
    }

    if (req.method === "GET" && req.url === "/md") {
      res.writeHead(200, { "Content-Type": "text/plain; charset=utf-8" });
      res.end(markdown);
      return;
    }

    res.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
    res.end("not found");
  });

  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, host, () => {
      server.off("error", reject);
      resolve();
    });
  });

  const address = server.address();
  if (!address || typeof address === "string") {
    throw new Error("failed to resolve markdown server address");
  }

  return {
    host: address.address,
    port: address.port,
    url: `http://${address.address}:${address.port}`,
    close() {
      return new Promise((resolve, reject) => {
        server.close((error) => {
          if (error) {
            reject(error);
            return;
          }
          resolve();
        });
      });
    },
  };
}

function parseArgs(argv) {
  const options = {};
  for (let index = 0; index < argv.length; index += 1) {
    const current = argv[index];
    const next = argv[index + 1];
    switch (current) {
      case "--file":
        options.filePath = next;
        index += 1;
        break;
      case "--host":
        options.host = next;
        index += 1;
        break;
      case "--port":
        options.port = Number(next);
        index += 1;
        break;
      case "--origin":
        options.allowedOrigin = next;
        index += 1;
        break;
      default:
        break;
    }
  }
  return options;
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const server = await createMarkdownServer(options);
  console.log(`Markdown server ready at ${server.url}/md`);
}

const __filename = fileURLToPath(import.meta.url);

// Robust entry-point check: realpathSync dereferences symlinks (e.g. a script
// invoked through ~/.claude/skills/...) and normalizes paths, avoiding the
// `file://${process.argv[1]}` string-equality trap that silently skips main().
function isMainModule() {
  if (!process.argv[1]) return false;
  try {
    return realpathSync(process.argv[1]) === realpathSync(__filename);
  } catch {
    return false;
  }
}

if (isMainModule()) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  });
}
