#!/usr/bin/env node
// Generates the secrets the two services share and writes them to ./.env (read by docker
// compose for backend-engine), then copies the two frontend-ui needs into frontend-ui/.env
// (read by `shopify app dev`), creating it from .env.example if needed.
// Safe to re-run: existing keys are kept unless --force.
//   JWT_PRIVATE_KEY / JWT_PUBLIC_KEY  Ed25519 pair for frontend-ui -> backend-engine service JWTs
//   BACKEND_JWT_PRIVATE_KEY / BACKEND_JWT_PUBLIC_KEY  Ed25519 pair for backend-engine -> frontend-ui
//   TOKEN_ENCRYPTION_KEY             Fernet key backend-engine uses to encrypt Shopify tokens
import { generateKeyPairSync, randomBytes } from "node:crypto";
import { existsSync, readFileSync, writeFileSync } from "node:fs";

const path = new URL("../.env", import.meta.url);
const force = process.argv.includes("--force");
const current = existsSync(path) ? readFileSync(path, "utf8") : "";
const has = (key) => new RegExp(`^${key}=.+`, "m").test(current);

const KEYS = [
  "JWT_PRIVATE_KEY",
  "JWT_PUBLIC_KEY",
  "BACKEND_JWT_PRIVATE_KEY",
  "BACKEND_JWT_PUBLIC_KEY",
  "TOKEN_ENCRYPTION_KEY",
];
if (!force && KEYS.every(has)) {
  console.log(".env already has keys (use --force to rotate).");
  syncFrontend(current);
  process.exit(0);
}

const ed25519 = () =>
  generateKeyPairSync("ed25519", {
    privateKeyEncoding: { type: "pkcs8", format: "pem" },
    publicKeyEncoding: { type: "spki", format: "pem" },
  });
const ui = ed25519();
const engine = ed25519();
const oneLine = (pem) => pem.trim().replace(/\n/g, "\\n");
// Fernet keys are 32 random bytes, url-safe base64 encoded.
const fernet = randomBytes(32).toString("base64").replace(/\+/g, "-").replace(/\//g, "_");

const keep = current
  .split("\n")
  .filter((line) => !KEYS.some((key) => line.startsWith(`${key}=`)))
  .join("\n")
  .trim();

const out = [
  keep,
  `JWT_PRIVATE_KEY="${oneLine(ui.privateKey)}"`,
  `JWT_PUBLIC_KEY="${oneLine(ui.publicKey)}"`,
  `BACKEND_JWT_PRIVATE_KEY="${oneLine(engine.privateKey)}"`,
  `BACKEND_JWT_PUBLIC_KEY="${oneLine(engine.publicKey)}"`,
  `TOKEN_ENCRYPTION_KEY=${fernet}`,
]
  .filter(Boolean)
  .join("\n");
writeFileSync(path, out + "\n", { mode: 0o600 });
console.log("Wrote keys to .env. Set the same values as Railway service variables for production.");
syncFrontend(out);

// frontend-ui signs with JWT_PRIVATE_KEY and verifies backend-engine with BACKEND_JWT_PUBLIC_KEY.
function syncFrontend(root) {
  const target = new URL("../frontend-ui/.env", import.meta.url);
  const example = new URL("../frontend-ui/.env.example", import.meta.url);
  let text = existsSync(target) ? readFileSync(target, "utf8") : readFileSync(example, "utf8");
  for (const key of ["JWT_PRIVATE_KEY", "BACKEND_JWT_PUBLIC_KEY"]) {
    const line = root.split("\n").find((l) => l.startsWith(`${key}=`));
    const re = new RegExp(`^${key}=.*$`, "m");
    text = re.test(text) ? text.replace(re, () => line) : `${text.trimEnd()}\n${line}\n`;
  }
  writeFileSync(target, text, { mode: 0o600 });
  console.log("Copied the frontend keys to frontend-ui/.env.");
}
