/**
 * epub-to-web — Cloudflare Worker
 *
 * Required secrets (set via wrangler secret put OR CF dashboard):
 *   ADMIN_SECRET    — password for the admin UI
 *   GITHUB_TOKEN    — GitHub PAT (scope: repo)
 *   GITHUB_REPO     — "owner/repo-name"
 *
 * Optional secrets (for Cloudflare Pages deployment status):
 *   CF_API_TOKEN    — Cloudflare API token (scope: Pages:Read)
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

    // ── Auth ─────────────────────────────────────────────────────────────────
    const auth = request.headers.get("Authorization") || "";
    if (auth !== `Bearer ${env.ADMIN_SECRET}`) {
      return json({ error: "Unauthorized" }, 401);
    }

    const url = new URL(request.url);
    const { pathname } = url;

    try {
      // POST /upload  — { filename: string, content: base64 }
      if (pathname === "/upload" && request.method === "POST") {
        return await handleUpload(request, env);
      }

      // DELETE /book/:filename
      if (pathname.startsWith("/book/") && request.method === "DELETE") {
        const filename = decodeURIComponent(pathname.slice(6));
        return await handleDelete(filename, env);
      }

      // GET /status — recent commits + CF Pages deployment
      if (pathname === "/status" && request.method === "GET") {
        return await handleStatus(env);
      }

      // GET /books — proxy books.json (CORS bypass)
      if (pathname === "/books" && request.method === "GET") {
        return await handleBooks(env);
      }

      return json({ error: "Not found" }, 404);
    } catch (err) {
      console.error(err);
      return json({ error: err.message }, 500);
    }
  },
};

// ── Upload ────────────────────────────────────────────────────────────────────

async function handleUpload(request, env) {
  const { filename, content } = await request.json();

  if (!filename || !content) {
    return json({ error: "filename and content (base64) required" }, 400);
  }
  if (!filename.toLowerCase().endsWith(".epub")) {
    return json({ error: "Only .epub files allowed" }, 400);
  }

  const path = `books/${filename}`;

  // Check if file exists → need its SHA to update
  const sha = await getFileSha(path, env);

  const body = {
    message: `upload: ${filename}`,
    content,               // already base64 from client
    ...(sha ? { sha } : {}),
  };

  const res = await ghApi(`contents/${path}`, "PUT", body, env);
  if (!res.ok) {
    const err = await res.json();
    return json({ error: err.message || "GitHub API error" }, 500);
  }

  return json({ ok: true, filename, sha: !!sha ? "updated" : "created" });
}

// ── Delete ────────────────────────────────────────────────────────────────────

async function handleDelete(filename, env) {
  const path = `books/${filename}`;
  const sha = await getFileSha(path, env);

  if (!sha) return json({ error: "File not found in repo" }, 404);

  const res = await ghApi(`contents/${path}`, "DELETE", {
    message: `delete: ${filename}`,
    sha,
  }, env);

  if (!res.ok) {
    const err = await res.json();
    return json({ error: err.message || "GitHub delete failed" }, 500);
  }

  return json({ ok: true, deleted: filename });
}

// ── Status ────────────────────────────────────────────────────────────────────

async function handleStatus(env) {
  const results = await Promise.allSettled([
    getGitHubCommits(env),
    getCFDeployments(env),
  ]);

  const commits = results[0].status === "fulfilled" ? results[0].value : [];
  const deploys = results[1].status === "fulfilled" ? results[1].value : null;

  return json({ commits, deploys });
}

async function getGitHubCommits(env) {
  const res = await ghApi("commits?path=books&per_page=10", "GET", null, env);
  if (!res.ok) return [];
  const data = await res.json();
  return data.map((c) => ({
    sha:     c.sha?.slice(0, 7),
    message: c.commit?.message,
    author:  c.commit?.author?.name,
    date:    c.commit?.author?.date,
    url:     c.html_url,
  }));
}

async function getCFDeployments(env) {
  if (!env.CF_API_TOKEN || !env.CF_ACCOUNT_ID || !env.CF_PAGES_PROJECT) {
    return null; // optional — skip if not configured
  }
  const res = await fetch(
    `https://api.cloudflare.com/client/v4/accounts/${env.CF_ACCOUNT_ID}/pages/projects/${env.CF_PAGES_PROJECT}/deployments?per_page=5`,
    { headers: { Authorization: `Bearer ${env.CF_API_TOKEN}` } }
  );
  if (!res.ok) return null;
  const { result } = await res.json();
  return (result || []).map((d) => ({
    id:          d.id?.slice(0, 8),
    status:      d.latest_stage?.status,
    stage:       d.latest_stage?.name,
    created_on:  d.created_on,
    url:         d.url,
    branch:      d.deployment_trigger?.metadata?.branch,
  }));
}

// ── Books proxy ───────────────────────────────────────────────────────────────

async function handleBooks(env) {
  // Use GitHub raw URL to fetch the generated books.json
  // Assumes CF Pages output is committed to gh-pages branch
  // OR just re-read from the GitHub repo... actually books.json is in output/
  // which is not committed. Let's return the epub list from books/ folder instead.
  const res = await ghApi("contents/books", "GET", null, env);
  if (!res.ok) return json({ files: [] });
  const files = await res.json();
  const epubs = Array.isArray(files)
    ? files
        .filter((f) => f.name.toLowerCase().endsWith(".epub"))
        .map((f) => ({ name: f.name, size: f.size, sha: f.sha, download_url: f.download_url }))
    : [];
  return json({ files: epubs });
}

// ── GitHub helpers ────────────────────────────────────────────────────────────

function ghApi(path, method, body, env) {
  return fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/${path}`, {
    method,
    headers: {
      Authorization: `token ${env.GITHUB_TOKEN}`,
      "Content-Type": "application/json",
      "User-Agent":   "epub-to-web-worker/1.0",
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

// ── Helpers ───────────────────────────────────────────────────────────────────

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { ...CORS, "Content-Type": "application/json" },
  });
}
