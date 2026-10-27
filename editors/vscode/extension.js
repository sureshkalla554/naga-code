// Naga Code for VS Code: terminal integration plus a private local bridge that lets the Naga agent read the
// editor's selection and diagnostics and open diffs. The bridge listens on 127.0.0.1 only and every request
// must carry a random per-window token, published in a lock file only the user can read.
"use strict";
const vscode = require("vscode");
const http = require("http");
const crypto = require("crypto");
const fs = require("fs");
const os = require("os");
const path = require("path");

const MAX_BODY = 4 * 1024 * 1024;
let server = null;
let lockFile = null;
let token = null;
let port = null;
let terminal = null;
let statusItem = null;
const diffDocs = new Map(); // uri -> proposed content for read-only diff views

function nagaConfigDir() {
  if (process.env.NAGA_HOME) return path.join(process.env.NAGA_HOME, "config");
  return path.join(process.env.XDG_CONFIG_HOME || path.join(os.homedir(), ".config"), "naga");
}

function workspaceFolders() {
  return (vscode.workspace.workspaceFolders || []).map((f) => f.uri.fsPath);
}

function severityName(s) {
  return ["error", "warning", "info", "hint"][s] || "info";
}

function diagnosticsFor(filePath) {
  const out = [];
  const entries = filePath
    ? [[vscode.Uri.file(filePath), vscode.languages.getDiagnostics(vscode.Uri.file(filePath))]]
    : vscode.languages.getDiagnostics();
  for (const [uri, diags] of entries) {
    if (uri.scheme !== "file") continue;
    for (const d of diags) {
      out.push({
        path: uri.fsPath,
        line: d.range.start.line + 1,
        column: d.range.start.character + 1,
        severity: severityName(d.severity),
        source: d.source || "",
        message: d.message,
      });
    }
  }
  return out;
}

function editorContext() {
  const ed = vscode.window.activeTextEditor;
  const ctx = { workspaceFolders: workspaceFolders(), activeFile: null, selection: null,
                openFiles: vscode.workspace.textDocuments.filter((d) => d.uri.scheme === "file").map((d) => d.uri.fsPath) };
  if (ed && ed.document.uri.scheme === "file") {
    ctx.activeFile = ed.document.uri.fsPath;
    if (!ed.selection.isEmpty) {
      ctx.selection = { text: ed.document.getText(ed.selection), startLine: ed.selection.start.line + 1,
                        endLine: ed.selection.end.line + 1 };
    }
  }
  return ctx;
}

async function openDiff(filePath, newContent, title) {
  const proposed = vscode.Uri.from({ scheme: "naga-proposed", path: filePath, query: String(Date.now()) });
  diffDocs.set(proposed.toString(), newContent);
  const original = fs.existsSync(filePath) ? vscode.Uri.file(filePath)
    : vscode.Uri.from({ scheme: "naga-proposed", path: filePath + ".empty" });
  if (!fs.existsSync(filePath)) diffDocs.set(original.toString(), "");
  await vscode.commands.executeCommand("vscode.diff", original, proposed,
    title || `Naga proposes: ${path.basename(filePath)}`, { preview: true, preserveFocus: true });
  return { opened: true };
}

async function handle(route, body) {
  switch (route) {
    case "/ping": return { ok: true, workspaceFolders: workspaceFolders(), vscode: vscode.version };
    case "/context": return editorContext();
    case "/diagnostics": return { diagnostics: diagnosticsFor(body.path) };
    case "/openDiff": return openDiff(body.path, String(body.content || ""), body.title);
    case "/openFile": {
      const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(body.path));
      const line = Math.max(0, (body.line || 1) - 1);
      await vscode.window.showTextDocument(doc, { selection: new vscode.Range(line, 0, line, 0), preview: true });
      return { opened: true };
    }
    default: return null;
  }
}

function startBridge(context) {
  token = crypto.randomBytes(24).toString("hex");
  server = http.createServer((req, res) => {
    const reply = (status, obj) => {
      res.writeHead(status, { "content-type": "application/json" });
      res.end(JSON.stringify(obj));
    };
    const auth = req.headers["authorization"] || "";
    const given = Buffer.from(auth.replace(/^Bearer /, ""));
    const want = Buffer.from(token);
    if (req.method !== "POST" || given.length !== want.length || !crypto.timingSafeEqual(given, want)) {
      return reply(401, { error: "unauthorized" });
    }
    let size = 0;
    const chunks = [];
    req.on("data", (c) => {
      size += c.length;
      if (size > MAX_BODY) { reply(413, { error: "too large" }); req.destroy(); return; }
      chunks.push(c);
    });
    req.on("end", async () => {
      if (res.writableEnded) return;
      let body = {};
      try { body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : {}; }
      catch (e) { return reply(400, { error: "bad json" }); }
      try {
        const result = await handle(req.url, body);
        if (result === null) return reply(404, { error: "unknown route" });
        reply(200, result);
      } catch (e) {
        reply(500, { error: String(e && e.message || e) });
      }
    });
  });
  server.listen(0, "127.0.0.1", () => {
    port = server.address().port;
    const dir = path.join(nagaConfigDir(), "ide");
    fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
    lockFile = path.join(dir, `vscode-${process.pid}-${port}.json`);
    fs.writeFileSync(lockFile, JSON.stringify({ port, token, pid: process.pid, ide: "vscode",
      workspaceFolders: workspaceFolders() }), { mode: 0o600 });
    updateStatus();
  });
  context.subscriptions.push(vscode.workspace.onDidChangeWorkspaceFolders(() => {
    if (lockFile) {
      try {
        const data = JSON.parse(fs.readFileSync(lockFile, "utf8"));
        data.workspaceFolders = workspaceFolders();
        fs.writeFileSync(lockFile, JSON.stringify(data), { mode: 0o600 });
      } catch (e) { /* ignore */ }
    }
  }));
}

function updateStatus() {
  if (!statusItem) return;
  statusItem.text = "$(terminal) Naga";
  statusItem.tooltip = port ? `Naga Code: bridge on 127.0.0.1:${port}. Click to open Naga.` : "Naga Code";
}

function nagaTerminal() {
  if (terminal && terminal.exitStatus === undefined) return terminal;
  const cfg = vscode.workspace.getConfiguration("naga");
  const cwd = workspaceFolders()[0] || os.homedir();
  terminal = vscode.window.createTerminal({
    name: "Naga", cwd,
    env: { NAGA_IDE_PORT: String(port || ""), NAGA_IDE_TOKEN: token || "" },
  });
  const args = (cfg.get("extraArgs") || []).map((a) => JSON.stringify(a)).join(" ");
  terminal.sendText(`${cfg.get("command") || "naga"} ${args}`.trim(), true);
  return terminal;
}

// ---------------------------------------------------------------- inline suggestions (fill in the middle)
const FIM = [
  // [model name pattern, prompt builder, stop tokens]
  [/qwen|codeqwen/i, (p, s) => `<|fim_prefix|>${p}<|fim_suffix|>${s}<|fim_middle|>`,
    ["<|fim_pad|>", "<|endoftext|>", "<|file_sep|>", "<|im_end|>", "<|fim_prefix|>", "<|fim_suffix|>"]],
  [/codellama/i, (p, s) => `<PRE> ${p} <SUF>${s} <MID>`, ["<EOT>", "<PRE>", "<SUF>", "<MID>"]],
  [/deepseek/i, (p, s) => `<\uFF5Cfim\u2581begin\uFF5C>${p}<\uFF5Cfim\u2581hole\uFF5C>${s}<\uFF5Cfim\u2581end\uFF5C>`,
    ["<\uFF5Cend\u2581of\u2581sentence\uFF5C>", "<\uFF5Cfim\u2581begin\uFF5C>"]],
  [/starcoder|stable-code/i, (p, s) => `<fim_prefix>${p}<fim_suffix>${s}<fim_middle>`,
    ["<|endoftext|>", "<fim_prefix>", "<file_sep>"]],
];

function fimRequest(model, prefix, suffix) {
  const fam = FIM.find(([re]) => re.test(model));
  if (!fam) return null;
  return { prompt: fam[1](prefix, suffix), stop: fam[2] };
}

function cleanSuggestion(text, suffix) {
  if (!text) return "";
  let t = text.replace(/\s+$/, "");
  const next = suffix.split("\n").find((l) => l.trim()) || "";
  if (next.trim() && t.endsWith(next.trim())) t = t.slice(0, t.length - next.trim().length).replace(/\s+$/, "");
  const lines = t.split("\n");
  return lines.length > 16 ? lines.slice(0, 16).join("\n") : t;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function makeProvider(fetchImpl) {
  let lastKey = null;
  let lastText = null;
  return {
    async provideInlineCompletionItems(document, position, _context, token) {
      const cfg = vscode.workspace.getConfiguration("naga");
      if (!cfg.get("completions.enabled", true)) return [];
      await sleep(cfg.get("completions.debounceMs", 250));
      if (token && token.isCancellationRequested) return [];
      const offset = document.offsetAt(position);
      const text = document.getText();
      const prefix = text.slice(Math.max(0, offset - 4000), offset);
      const suffix = text.slice(offset, offset + 1500);
      const key = `${document.uri}|${document.version}|${offset}`;
      if (key === lastKey && lastText !== null) {
        return lastText ? [new vscode.InlineCompletionItem(lastText, new vscode.Range(position, position))] : [];
      }
      const model = cfg.get("completions.model", "qwen3-coder:latest");
      const req = fimRequest(model, prefix, suffix);
      if (!req) return [];
      const abort = new AbortController();
      const sub = token && token.onCancellationRequested ? token.onCancellationRequested(() => abort.abort()) : null;
      try {
        const r = await fetchImpl(`${cfg.get("completions.endpoint", "http://127.0.0.1:11434")}/api/generate`, {
          method: "POST", signal: abort.signal, headers: { "content-type": "application/json" },
          body: JSON.stringify({ model, raw: true, stream: false, prompt: req.prompt, keep_alive: "30m",
            options: { num_predict: cfg.get("completions.maxTokens", 128), temperature: 0.1, stop: req.stop } }),
        });
        if (!r.ok) return [];
        const out = cleanSuggestion((await r.json()).response || "", suffix);
        lastKey = key;
        lastText = out;
        return out ? [new vscode.InlineCompletionItem(out, new vscode.Range(position, position))] : [];
      } catch (e) {
        return [];   // cancelled (user kept typing) or Ollama not reachable: no suggestion
      } finally {
        if (sub) sub.dispose();
      }
    },
  };
}

function activate(context) {
  context.subscriptions.push(vscode.workspace.registerTextDocumentContentProvider("naga-proposed", {
    provideTextDocumentContent: (uri) => diffDocs.get(uri.toString()) || "",
  }));
  startBridge(context);
  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  statusItem.command = "naga.open";
  updateStatus();
  statusItem.show();
  context.subscriptions.push(statusItem);

  context.subscriptions.push(vscode.commands.registerCommand("naga.open", () => nagaTerminal().show()));
  context.subscriptions.push(vscode.commands.registerCommand("naga.sendSelection", () => {
    const ed = vscode.window.activeTextEditor;
    if (!ed) return;
    const rel = vscode.workspace.asRelativePath(ed.document.uri, false);
    const s = ed.selection.start.line + 1;
    const e = ed.selection.end.line + 1;
    const ref = ed.selection.isEmpty ? `@${rel} ` : `@${rel}:${s}-${e} `;
    const t = nagaTerminal();
    t.show();
    t.sendText(ref, false); // typed into Naga's input; the user finishes the request
  }));
  context.subscriptions.push(vscode.commands.registerCommand("naga.status", () => {
    vscode.window.showInformationMessage(port ? `Naga bridge on 127.0.0.1:${port} (lock file ${lockFile})`
      : "Naga bridge not running");
  }));
  const provider = makeProvider((url, opts) => fetch(url, opts));
  context.subscriptions.push(vscode.languages.registerInlineCompletionItemProvider({ pattern: "**" }, provider));
  context.subscriptions.push(vscode.commands.registerCommand("naga.toggleCompletions", async () => {
    const cfg = vscode.workspace.getConfiguration("naga");
    const on = !cfg.get("completions.enabled", true);
    await cfg.update("completions.enabled", on, vscode.ConfigurationTarget.Global);
    vscode.window.showInformationMessage(`Naga inline suggestions ${on ? "on" : "off"}`);
  }));
  context.subscriptions.push({ dispose: deactivate });
  return { bridge: () => ({ port, token, lockFile }), provider }; // used by the extension tests
}

function deactivate() {
  if (lockFile) { try { fs.unlinkSync(lockFile); } catch (e) { /* already gone */ } lockFile = null; }
  if (server) { server.close(); server = null; }
}

module.exports = { activate, deactivate, fimRequest, cleanSuggestion };
