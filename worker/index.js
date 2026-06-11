/**
 * epub-to-web — Cloudflare Worker
 *
 * Required secrets (wrangler secret put <NAME>):
 *   ADMIN_SECRET    — password for the admin UI
 *   GITHUB_TOKEN    — GitHub PAT (scope: repo)
 *   GITHUB_REPO     — "owner/repo-name"
 *
 * Optional (for CF Pages deployment status in admin):
 *   CF_API_TOKEN    — Cloudflare API token (Pages:Read scope)
 *   CF_ACCOUNT_ID   — Your Cloudflare account ID
 *   CF_PAGES_PROJECT — CF Pages project name
 */

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
};

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS });
    }

    // ── Auth ──────────────────────────────────────────────────────────────────
    const auth = request.headers.get("Authorization") || "";
    if (auth !== `Bearer ${env.ADMIN_SECRET}`) {
      return json({ error: "Unauthorized" }, 401);
    }

    const { pathname } = new URL(request.url);

    try {
      if (pathname === "/upload" && request.method === "POST")
        return await handleUpload(request, env);

      if (pathname.startsWith("/book/") && request.method === "DELETE")
        return await handleDelete(decodeURIComponent(pathname.slice(6)), env);

      if (pathname === "/status" && request.method === "GET")
        return await handleStatus(env);

      // GET /books — list of .epub files currently in the repo
      if (pathname === "/books" && request.method === "GET")
        return await handleBooks(env);

      return json({ error: "Not found" }, 404);
    } catch (err) {
      console.error(err);
      return json({ error: err.message }, 500);
    }
  },
};

// ── Upload ─────────────────────────────────────────────────────────────────────
// Client sends: { filename: string, content: base64string }
// Client does the base64 encoding (FileReader.readAsDataURL) so Worker
// has minimal CPU work — just passes the b64 straight to GitHub API.

async function handleUpload(request, env) {
  const { filename, content } = await request.json();

  if (!filename || !content)
    return json({ error: "filename and content (base64) are required" }, 400);

  if (!filename.toLowerCase().endsWith(".epub"))
    return json({ error: "Only .epub files are accepted" }, 400);

  // Sanitise filename: no path traversal
  const safeName = filename.replace(/[^a-zA-Z0-9._\- ]/g, "_");
  const path = `books/${safeName}`;

  // Get existing SHA (needed to overwrite an existing file)
  const sha = await getFileSha(path, env);

  const body = {
    message: sha ? `update: ${safeName}` : `upload: ${safeName}`,
    content,   // already base64 — GitHub expects this exact format
    ...(sha ? { sha } : {}),
  };

  const res = await ghApi(`contents/${path}`, "PUT", body, env);

  if (!res.ok) {
    const err = await res.json().catch(() => ({}));

    // GitHub returns 422 when file content hasn't changed.
    // Treat as success — the file is already there.
    if (res.status === 422) {
      const msg = (err.message || "").toLowerCase();
      if (msg.includes("sha") || msg.includes("nothing to commit") || msg.includes("no change")) {
        return json({ ok: true, filename: safeName, note: "File unchanged — no new commit created" });
      }
    }

    return json({ error: err.message || `GitHub API error (${res.status})` }, 502);
  }

  const data = await res.json();
  return json({
    ok:       true,
    filename: safeName,
    sha:      sha ? "updated" : "created",
    commit:   data.commit?.sha?.slice(0, 7),
  });
}

// ── Delete ─────────────────────────────────────────────────────────────────────

async function handleDelete(filename, env) {
  // Accept both "slug" and "filename.epub" forms
  const epubName = filename.endsWith(".epub") ? filename : `${filename}.epub`;
  // Try the exact name first, then search
  let path = `books/${epubName}`;
  let sha = await getFileSha(path, env);

  if (!sha) {
    // Search all epub files for a match
    const { files } = await listBooks(env);
    const match = files.find(f => {
      const base = f.name.replace(/\.epub$/i, "");
      return base === filename || base.replace(/\s+/g, "-").toLowerCase() === filename;
    });
    if (!match) return json({ error: `File not found in repo: ${filename}` }, 404);
    path = `books/${match.name}`;
    sha  = match.sha;
  }

  const res = await ghApi(`contents/${path}`, "DELETE", {
    message: `delete: ${path}`,
    sha,
  }, env);

  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    return json({ error: err.message || "GitHub delete failed" }, 502);
  }

  return json({ ok: true, deleted: path });
}

// ── Status ─────────────────────────────────────────────────────────────────────

async function handleStatus(env) {
  const [commitsResult, deploysResult] = await Promise.allSettled([
    getGitHubCommits(env),
    getCFDeployments(env),
  ]);

  return json({
    commits: commitsResult.status === "fulfilled" ? commitsResult.value : [],
    deploys: deploysResult.status === "fulfilled" ? deploysResult.value : null,
    ts: new Date().toISOString(),
  });
}

async function getGitHubCommits(env) {
  // Get commits that touched books/ folder
  const res = await ghApi("commits?path=books&per_page=10", "GET", null, env);
  if (!res.ok) return [];
  const data = await res.json();
  return data.map(c => ({
    sha:     c.sha?.slice(0, 7),
    message: c.commit?.message,
    author:  c.commit?.author?.name,
    date:    c.commit?.author?.date,
    url:     c.html_url,
  }));
}

async function getCFDeployments(env) {
  if (!env.CF_API_TOKEN || !env.CF_ACCOUNT_ID || !env.CF_PAGES_PROJECT) return null;
  const res = await fetch(
    `https://api.cloudflare.com/client/v4/accounts/${env.CF_ACCOUNT_ID}/pages/projects/${env.CF_PAGES_PROJECT}/deployments?per_page=5`,
    { headers: { Authorization: `Bearer ${env.CF_API_TOKEN}` } }
  );
  if (!res.ok) return null;
  const { result } = await res.json();
  return (result || []).map(d => ({
    id:         d.id?.slice(0, 8),
    status:     d.latest_stage?.status,   // "success" | "failure" | "active" | ...
    stage:      d.latest_stage?.name,
    created_on: d.created_on,
    url:        d.url,
    branch:     d.deployment_trigger?.metadata?.branch,
  }));
}

// ── Books list ──────────────────────────────────────────────────────────────────

async function handleBooks(env) {
  const result = await listBooks(env);
  return json(result);
}

async function listBooks(env) {
  const res = await ghApi("contents/books", "GET", null, env);
  if (!res.ok) return { files: [] };
  const raw = await res.json();
  const files = Array.isArray(raw)
    ? raw
        .filter(f => f.name?.toLowerCase().endsWith(".epub"))
        .map(f => ({ name: f.name, size: f.size, sha: f.sha }))
    : [];
  return { files };
}

// ── GitHub helpers ──────────────────────────────────────────────────────────────

function ghApi(path, method, body, env) {
  return fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/${path}`, {
    method,
    headers: {
      Authorization:  `token ${env.GITHUB_TOKEN}`,
      "Content-Type": "application/json",
      "User-Agent":   "epub-to-web/1.0",
      Accept:         "application/vnd.github.v3+json",
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
}

async function getFileSha(path, env) {
  const res = await ghApi(`contents/${path}`, "GET", null, env);
  if (!res.ok) return null;
  const data = await res.json();
  return data.sha || null;
}

// ── Helpers ─────────────────────────────────────────────────────────────────────

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { ...CORS, "Content-Type": "application/json" },
  });
}
