// N1 Alimentos - Formador de Carga
// Servidor (sem dependencias externas): serve o app e guarda os dados COMPARTILHADOS do time.
// Os dados ficam no servidor (nao em cada navegador), entao todos no mesmo link veem o mesmo.
// Persistencia em disco; no Railway use um Volume montado e defina DATA_DIR (ex.: /data) para
// manter os dados entre os deploys.
const http = require("http");
const fs = require("fs");
const path = require("path");

const PORT = process.env.PORT || 3000;
const ROOT = __dirname;
// Persistencia: usa DATA_DIR se definido; senao usa o Volume do Railway (RAILWAY_VOLUME_MOUNT_PATH)
// se houver um anexado; senao cai para ./data (efemero, so para teste local).
const DATA_DIR = process.env.DATA_DIR || process.env.RAILWAY_VOLUME_MOUNT_PATH || path.join(ROOT, "data");
try { fs.mkdirSync(DATA_DIR, { recursive: true }); } catch (e) {}

const STORES = { auth: "auth.json", app: "app.json", audit: "audit.json" };

function readStore(name) {
  try { return JSON.parse(fs.readFileSync(path.join(DATA_DIR, STORES[name]), "utf8")); }
  catch (e) { return { rev: 0, updatedAt: 0, data: null }; }
}
function writeStore(name, obj) {
  const fp = path.join(DATA_DIR, STORES[name]);
  const tmp = fp + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(obj));
  fs.renameSync(tmp, fp); // gravacao atomica
}
function body(req) {
  return new Promise((resolve) => {
    let b = ""; req.on("data", c => { b += c; if (b.length > 25 * 1024 * 1024) req.destroy(); });
    req.on("end", () => resolve(b));
  });
}
function sendJSON(res, code, obj) {
  res.writeHead(code, { "Content-Type": "application/json; charset=utf-8" });
  res.end(JSON.stringify(obj));
}

const MIME = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
  ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml", ".ico": "image/x-icon"
};

http.createServer(async (req, res) => {
  const u = new URL(req.url, "http://x");
  const p = u.pathname;

  // ---- API de dados compartilhados ----
  if (p === "/api/ping") return sendJSON(res, 200, { ok: true });
  if (p === "/api/all" && req.method === "GET")
    return sendJSON(res, 200, { auth: readStore("auth"), app: readStore("app"), audit: readStore("audit") });
  if (p === "/api/rev" && req.method === "GET")
    return sendJSON(res, 200, { auth: readStore("auth").rev, app: readStore("app").rev, audit: readStore("audit").rev });

  const m = p.match(/^\/api\/(auth|app|audit)$/);
  if (m) {
    const name = m[1];
    if (req.method === "GET") return sendJSON(res, 200, readStore(name));
    if (req.method === "PUT") {
      let parsed; try { parsed = JSON.parse(await body(req)); } catch (e) { return sendJSON(res, 400, { error: "bad json" }); }
      const cur = readStore(name);
      const obj = { rev: (cur.rev || 0) + 1, updatedAt: Date.now(), data: parsed.data };
      try { writeStore(name, obj); } catch (e) { return sendJSON(res, 500, { error: "write failed" }); }
      return sendJSON(res, 200, { rev: obj.rev, updatedAt: obj.updatedAt });
    }
    return sendJSON(res, 405, { error: "method not allowed" });
  }

  // ---- arquivos estaticos ----
  let urlPath = decodeURIComponent(p);
  if (urlPath === "/" || urlPath === "") urlPath = "/index.html";
  const safe = path.normalize(urlPath).replace(/^(\.\.[/\\])+/, "");
  const file = path.join(ROOT, safe);
  if (!file.startsWith(ROOT)) { res.writeHead(403); return res.end("Forbidden"); }
  fs.readFile(file, (err, data) => {
    if (err) {
      fs.readFile(path.join(ROOT, "index.html"), (e2, idx) => {
        if (e2) { res.writeHead(404, { "Content-Type": "text/plain" }); return res.end("Not found"); }
        res.writeHead(200, { "Content-Type": MIME[".html"] }); res.end(idx);
      });
      return;
    }
    res.writeHead(200, { "Content-Type": MIME[path.extname(file).toLowerCase()] || "application/octet-stream" });
    res.end(data);
  });
}).listen(PORT, () => console.log("N1 Formador de Carga na porta " + PORT + " | dados em " + DATA_DIR));
