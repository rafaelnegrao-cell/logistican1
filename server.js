// N1 Alimentos - Formador de Carga
// Servidor estatico minimo, sem dependencias externas.
// Railway define a porta via process.env.PORT.
const http = require("http");
const fs = require("fs");
const path = require("path");

const PORT = process.env.PORT || 3000;
const ROOT = __dirname;
const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
  ".webmanifest": "application/manifest+json"
};

http.createServer((req, res) => {
  let urlPath = decodeURIComponent((req.url || "/").split("?")[0]);
  if (urlPath === "/" || urlPath === "") urlPath = "/index.html";
  // impede path traversal
  const safe = path.normalize(urlPath).replace(/^(\.\.[/\\])+/, "");
  const file = path.join(ROOT, safe);
  if (!file.startsWith(ROOT)) {
    res.writeHead(403); res.end("Forbidden"); return;
  }
  fs.readFile(file, (err, data) => {
    if (err) {
      // fallback para a SPA: serve index.html
      fs.readFile(path.join(ROOT, "index.html"), (e2, idx) => {
        if (e2) { res.writeHead(404, { "Content-Type": "text/plain" }); res.end("Not found"); return; }
        res.writeHead(200, { "Content-Type": MIME[".html"] }); res.end(idx);
      });
      return;
    }
    const ext = path.extname(file).toLowerCase();
    res.writeHead(200, { "Content-Type": MIME[ext] || "application/octet-stream" });
    res.end(data);
  });
}).listen(PORT, () => console.log("N1 Formador de Carga rodando na porta " + PORT));
