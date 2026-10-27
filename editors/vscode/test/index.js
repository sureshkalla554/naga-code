// Runs inside a real VS Code (code --extensionDevelopmentPath=editors/vscode --extensionTestsPath=editors/vscode/test).
// Needs Ollama with the completions model. Writes results to $NAGA_TEST_OUT (JSON) and fails on any broken check.
"use strict";
const vscode = require("vscode");
const assert = require("assert");
const http = require("http");
const fs = require("fs");

function post(port, token, route, body) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(body || {});
    const req = http.request({ host: "127.0.0.1", port, path: route, method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${token}`, "content-length": data.length } },
    (res) => { let b = ""; res.on("data", (c) => (b += c)); res.on("end", () => resolve({ status: res.statusCode, body: b })); });
    req.on("error", reject);
    req.end(data);
  });
}

async function run() {
  const results = {};
  const ext = vscode.extensions.getExtension("naga-code.naga");
  const api = await ext.activate();
  await new Promise((r) => setTimeout(r, 500));
  const { port, token } = api.bridge();

  results.bridge_ok = (await post(port, token, "/ping")).status === 200;
  results.bridge_rejects_bad_token = (await post(port, "x".repeat(48), "/ping")).status === 401;

  const doc = await vscode.workspace.openTextDocument({ language: "python",
    content: 'def fibonacci(n):\n    """Return the n-th Fibonacci number."""\n    \n\n\nprint(fibonacci(10))\n' });
  await vscode.window.showTextDocument(doc);
  const pos = new vscode.Position(2, 4);
  const cancel = new vscode.CancellationTokenSource();
  const timings = [];
  let suggestion = "";
  for (let i = 0; i < 3; i++) {
    const t0 = Date.now();
    const items = await api.provider.provideInlineCompletionItems(doc, pos, {}, cancel.token);
    timings.push(Date.now() - t0);
    suggestion = items.length ? String(items[0].insertText) : "";
    // a new document version each time, so the cache is not used
    await vscode.window.activeTextEditor.edit((e) => e.insert(new vscode.Position(5, 0), ""));
  }
  results.suggestion = suggestion;
  results.timings_ms = timings;

  const cancelled = new vscode.CancellationTokenSource();
  setTimeout(() => cancelled.cancel(), 50);
  results.cancel_returns_nothing = (await api.provider.provideInlineCompletionItems(doc, new vscode.Position(2, 4), {},
    cancelled.token)).length === 0;

  fs.writeFileSync(process.env.NAGA_TEST_OUT, JSON.stringify(results, null, 1));
  assert.ok(results.bridge_ok && results.bridge_rejects_bad_token, "bridge");
  assert.ok(/return/.test(suggestion) && /fibonacci|n\s*-\s*1|<=\s*1|<\s*2/.test(suggestion), `suggestion: ${suggestion}`);
  assert.ok(results.cancel_returns_nothing, "cancellation");
}

module.exports = { run };
