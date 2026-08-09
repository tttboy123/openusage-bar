import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";


const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = resolve(webRoot, "..");
const canonicalPath = resolve(
  repositoryRoot,
  "openusage_bar/resources/artifact-build-identity.v1.json",
);
const packagedName = "product-build-identity.v1.json";
const copies = [resolve(webRoot, "public", packagedName)];

if (process.argv.includes("--dist")) {
  copies.push(resolve(webRoot, "dist", packagedName));
}

try {
  const canonical = await readFile(canonicalPath);
  for (const copy of copies) {
    const packaged = await readFile(copy);
    if (!canonical.equals(packaged)) {
      throw new Error("identity bytes differ");
    }
  }
  process.stdout.write("web_build_identity_ok\n");
} catch {
  process.stderr.write("web_build_identity_invalid\n");
  process.exitCode = 1;
}
