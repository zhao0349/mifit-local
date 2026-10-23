/* Synthetic worker harness: stdin contains fixtures, never a real file path. */
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const workerSource = fs.readFileSync(
  path.join(__dirname, "..", "dashboard", "import-worker.js"), "utf8"
);
const intradaySource = fs.readFileSync(
  path.join(__dirname, "..", "dashboard", "intraday-data.js"), "utf8"
);

function virtualGPS(megabytes) {
  const prefix = '{"schema_version":"1.0","records":{"workout_gps":[';
  const row = '{"timestamp":"2026-10-20T12:00:00Z","latitude":1.25,"longitude":2.5,"marker":"synthetic-only"},';
  const count = Math.ceil(megabytes * 1024 * 1024 / row.length);
  const suffix = '{"latitude":3,"longitude":4}]}}';
  const endRows = prefix.length + count * row.length;
  return {
    size: endRows + suffix.length, expectedCount: count + 1,
    slice(start, end) {
      let text = "";
      while (start < end) {
        if (start < prefix.length) {
          const stop = Math.min(end, prefix.length);
          text += prefix.slice(start, stop);
          start = stop;
        } else if (start < endRows) {
          const stop = Math.min(end, endRows), offset = (start - prefix.length) % row.length;
          text += row.repeat(Math.ceil((offset + stop - start) / row.length))
            .slice(offset, offset + stop - start);
          start = stop;
        } else {
          text += suffix.slice(start - endRows, end - endRows);
          start = end;
        }
      }
      return Buffer.from(text, "utf8");
    }
  };
}

async function run(testCase) {
  const source = testCase.chunkBytes ? workerSource.replace(
    "const CHUNK_BYTES = 256 * 1024;", `const CHUNK_BYTES = ${Number(testCase.chunkBytes)};`
  ) : workerSource;
  const bytes = testCase.bytesBase64 ? Buffer.from(testCase.bytesBase64, "base64") :
    Buffer.from(testCase.text || "", "utf8");
  const content = testCase.stressMiB ? virtualGPS(testCase.stressMiB) : {
    size: bytes.length, slice: (start, end) => bytes.subarray(start, end)
  };
  let reads = 0, maxRead = 0;
  const file = {
    name: testCase.name || "synthetic.json",
    size: testCase.artificialSize === undefined ? content.size : testCase.artificialSize,
    text() { throw Error("Whole-file text reads are forbidden in this harness."); },
    arrayBuffer() { throw Error("Whole-file buffer reads are forbidden in this harness."); },
    slice(start, end) {
      const part = content.slice(start, end);
      reads++;
      maxRead = Math.max(maxRead, part.length);
      return { arrayBuffer: async () => Uint8Array.from(part).buffer };
    }
  };
  const messages = [];
  const sandbox = { TextDecoder, self: { postMessage: message => messages.push(message) } };
  vm.createContext(sandbox);
  vm.runInContext(intradaySource, sandbox, { timeout: 2000 });
  vm.runInContext(source, sandbox, { timeout: 2000 });
  await sandbox.self.onmessage({ data: { file } });
  return {
    final: messages.at(-1),
    progress: messages.filter(message => message.type === "progress"),
    reads, maxRead, expectedCount: content.expectedCount
  };
}

(async function () {
  const payload = JSON.parse(fs.readFileSync(0, "utf8"));
  const outputs = [];
  for (const testCase of Array.isArray(payload) ? payload : [payload]) outputs.push(await run(testCase));
  process.stdout.write(JSON.stringify(outputs));
}()).catch(() => {
  process.stderr.write("Synthetic worker harness failed.\n");
  process.exitCode = 1;
});
