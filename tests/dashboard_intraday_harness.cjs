/* Synthetic fixtures only: no health paths are accepted. */
"use strict";
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "..", "dashboard", "intraday-data.js"), "utf8");
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { timeout: 2000 });
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const results = input.map(item => {
  try {
    const model = sandbox.MiFitIntradayModel;
    return { value: item.method === "sleepTimeline" ? model.sleepTimeline(item.value) : model.normalize(item.value) };
  } catch (error) {
    return { error: String(error.message), name: String(error.name) };
  }
});
process.stdout.write(JSON.stringify(results));
