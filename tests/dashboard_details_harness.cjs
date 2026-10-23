"use strict";
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const context = { window: {} };
vm.createContext(context);
vm.runInContext(
  fs.readFileSync(path.join(__dirname, "..", "dashboard", "detail-data.js"), "utf8"),
  context,
  { timeout: 5000 }
);
const model = context.window.MiFitDetailModel;
const dataBefore = JSON.stringify(input.data || {});
const results = (input.optionsList || [input.options || {}]).map(options => model.analyze(input.data || {}, options));
process.stdout.write(JSON.stringify({
  categories: model.categories,
  metrics: model.metrics,
  results,
  inputUnchanged: JSON.stringify(input.data || {}) === dataBefore,
}));
