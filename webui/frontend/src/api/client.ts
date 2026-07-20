// Thin fetch wrappers around the webui FastAPI backend (webui/main.py, WS-B). Kept separate
// from the reducer/types module (../utils/searchReducer) so the wire types have exactly one
// home; this file only knows how to reach the routes.

import type { SearchEnvelope } from "../utils/searchReducer";

export class ApiError extends Error {
  status: number;
  position: number | null;

  constructor(message: string, status: number, position: number | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.position = position;
  }
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path);
  if (!res.ok) {
    // GET /api/search returns 400 {error, position?} on QueryParseError (WS-B contract).
    const body = await res.json().catch(() => null);
    const message = (body && typeof body.error === "string" ? body.error : null) ?? res.statusText;
    const position = body && typeof body.position === "number" ? body.position : null;
    throw new ApiError(message, res.status, position);
  }
  return (await res.json()) as T;
}

export function searchCode(query: string, opts: { limit?: number; cursor?: string | null } = {}): Promise<SearchEnvelope> {
  const params = new URLSearchParams({ q: query });
  if (opts.limit) params.set("limit", String(opts.limit));
  if (opts.cursor) params.set("cursor", opts.cursor);
  return getJson<SearchEnvelope>(`/api/search?${params.toString()}`);
}

export interface FileResponse {
  repo: string;
  path: string;
  branch: string;
  content: string | null;
  found: boolean;
}

export function getFile(repo: string, path: string): Promise<FileResponse> {
  const params = new URLSearchParams({ repo, path });
  return getJson<FileResponse>(`/api/file?${params.toString()}`);
}

export interface RepoInfo {
  name: string;
  branches: string[];
  index_time: string | null;
  default_branch: string | null;
  last_indexed_commit: string | null;
}

export interface ReposResponse {
  repos: RepoInfo[];
  count: number;
}

export function listRepos(): Promise<ReposResponse> {
  return getJson<ReposResponse>("/api/repos");
}
