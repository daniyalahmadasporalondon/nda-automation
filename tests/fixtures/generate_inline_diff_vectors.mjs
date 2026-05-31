import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const FIXTURE_DIR = path.dirname(fileURLToPath(import.meta.url));
const SOURCE_PATH = path.join(FIXTURE_DIR, "inline_diff_vectors.source.json");
const OUTPUT_PATH = path.join(FIXTURE_DIR, "inline_diff_vectors.json");

const sourceVectors = JSON.parse(fs.readFileSync(SOURCE_PATH, "utf8"));

const vectors = sourceVectors.map((vector) => {
  const expanded = { ...vector };
  if (expanded.originalTokenBlock) {
    expanded.original = tokenBlockText(expanded.originalTokenBlock);
    delete expanded.originalTokenBlock;
  }
  if (expanded.replacementTokenBlock) {
    expanded.replacement = tokenBlockText(expanded.replacementTokenBlock);
    delete expanded.replacementTokenBlock;
  }
  if (expanded.operationBlocks) {
    const blockOperations = expanded.operationBlocks.flatMap((block) => (
      Array.from({ length: block.count }, (_, index) => ({
        type: block.type,
        token: `${block.prefix}${index}`,
      }))
    ));
    expanded.operations = [...(expanded.operations || []), ...blockOperations];
    delete expanded.operationBlocks;
  }
  return expanded;
});

fs.writeFileSync(OUTPUT_PATH, `${JSON.stringify(vectors, null, 2)}\n`);

function tokenBlockText(block) {
  return Array.from({ length: block.count }, (_, index) => `${block.prefix}${index}`).join(" ");
}
