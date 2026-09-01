import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import puppeteer from "puppeteer-core";

const executableCandidates = [
  process.env.CINEVAULT_CHROMIUM,
  process.env.CINEVAULT_INSTALL_ROOT && path.join(process.env.CINEVAULT_INSTALL_ROOT, ".browser", "chrome-headless-shell"),
  "/usr/bin/chromium",
  "/usr/bin/google-chrome-stable",
  "/usr/bin/google-chrome",
].filter(Boolean);

let input = "";
for await (const chunk of process.stdin) input += chunk;
const jobs = JSON.parse(input || "[]").slice(0, 200);

const resolvedExecutable = await (async () => {
  for (const candidate of executableCandidates) {
    try {
      await fs.access(candidate);
      return candidate;
    } catch {
      // Try another locally installed browser.
    }
  }
  const browserRoot = path.resolve(process.cwd(), "..", ".browser");
  const findBundledBrowser = async (directory, depth = 0) => {
    if (depth > 6) return "";
    let entries;
    try {
      entries = await fs.readdir(directory, { withFileTypes: true });
    } catch {
      return "";
    }
    for (const entry of entries) {
      const candidate = path.join(directory, entry.name);
      if (entry.isFile() && entry.name === "chrome-headless-shell") return candidate;
      if (entry.isDirectory()) {
        const found = await findBundledBrowser(candidate, depth + 1);
        if (found) return found;
      }
    }
    return "";
  };
  return findBundledBrowser(browserRoot);
})();

if (!resolvedExecutable) {
  process.stderr.write("No supported Chromium executable was found.\n");
  process.exit(2);
}

const browser = await puppeteer.launch({
  executablePath: resolvedExecutable,
  headless: true,
  args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
});
const page = await browser.newPage();
await page.setViewport({ width: 600, height: 900, deviceScaleFactor: 1 });
await page.setRequestInterception(true);
page.on("request", (request) => {
  if (["http:", "https:"].includes(new URL(request.url()).protocol)) request.abort();
  else request.continue();
});

const posterDocument = `<!doctype html>
<html><head><meta charset="utf-8"><style>
  *{box-sizing:border-box}html,body{width:600px;height:900px;margin:0;overflow:hidden;background:#080808;font-family:Arial,Helvetica,sans-serif}
  #poster{position:relative;width:600px;height:900px;overflow:hidden;background:radial-gradient(circle at 70% 14%,#4a1017,#111 42%,#050505 82%);color:#fff}
  #art,#blur{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
  #art{filter:saturate(.95) contrast(1.08);transform:scale(1.03)}
  #blur{filter:blur(38px) brightness(.45) saturate(1.25);transform:scale(1.2);opacity:.75}
  .shade{position:absolute;inset:0;background:linear-gradient(180deg,rgba(0,0,0,.05) 0%,rgba(0,0,0,.08) 38%,rgba(0,0,0,.9) 78%,#050505 100%),linear-gradient(90deg,rgba(0,0,0,.45),transparent 55%)}
  .grain{position:absolute;inset:0;opacity:.08;background-image:linear-gradient(120deg,transparent 0 49%,rgba(255,255,255,.4) 50%,transparent 51%);background-size:7px 7px}
  .brand{position:absolute;left:42px;top:40px;color:#e50914;font-size:24px;font-weight:900;letter-spacing:-1.4px;text-shadow:0 3px 18px rgba(0,0,0,.8)}
  .brand span{color:#fff}.copy{position:absolute;left:42px;right:42px;bottom:48px}
  .eyebrow{display:flex;align-items:center;gap:8px;margin-bottom:15px;color:#ddd;font-size:13px;font-weight:800;letter-spacing:2.4px;text-transform:uppercase}
  .eyebrow i{width:8px;height:8px;border-radius:50%;background:#e50914;box-shadow:0 0 14px #e50914}
  h1{margin:0;display:-webkit-box;overflow:hidden;font-size:52px;line-height:.98;letter-spacing:-2.7px;text-shadow:0 4px 24px #000;-webkit-line-clamp:4;-webkit-box-orient:vertical}
  .rule{width:62px;height:5px;margin-top:25px;background:#e50914}
</style></head><body><main id="poster"><img id="blur" hidden><img id="art" hidden><div class="shade"></div><div class="grain"></div><div class="brand">CINE<span>VAULT</span></div><div class="copy"><div class="eyebrow"><i></i><span>Now streaming</span></div><h1 id="title"></h1><div class="rule"></div></div></main></body></html>`;

const results = [];
try {
  for (const job of jobs) {
    const result = { id: Number(job.id), ok: false };
    try {
      let imageData = "";
      if (job.source) {
        try {
          const raw = await fs.readFile(job.source);
          if (raw.length <= 5 * 1024 * 1024) imageData = `data:image/webp;base64,${raw.toString("base64")}`;
        } catch {
          // A title-only poster is still useful when Telegram has no preview.
        }
      }
      await page.setContent(posterDocument, { waitUntil: "domcontentloaded" });
      await page.evaluate(async ({ title, imageData }) => {
        document.querySelector("#title").textContent = String(title || "Untitled").slice(0, 180);
        if (!imageData) return;
        const images = [document.querySelector("#blur"), document.querySelector("#art")];
        await Promise.all(images.map(async (image) => {
          image.src = imageData;
          image.hidden = false;
          await image.decode();
        }));
      }, { title: job.title || job.filename, imageData });
      const poster = await page.$("#poster");
      if (!poster) throw new Error("Poster surface did not render");
      await poster.screenshot({ path: job.output, type: "webp", quality: 84 });
      result.ok = true;
    } catch (error) {
      result.error = String(error?.message || error).slice(0, 180);
    }
    results.push(result);
  }
} finally {
  await browser.close();
}

process.stdout.write(JSON.stringify(results));
