import { useEffect, useState } from "react";
import { ApiError, getFile, type FileResponse } from "../api/client";
import { CodeBlock } from "../components/CodeBlock";

function detectLang(path: string): string | null {
  const ext = path.split(".").pop()?.toLowerCase();
  const byExt: Record<string, string> = {
    py: "python",
    js: "javascript",
    jsx: "javascript",
    ts: "typescript",
    tsx: "tsx",
    go: "go",
    java: "java",
    rs: "rust",
  };
  return ext ? (byExt[ext] ?? null) : null;
}

type FilePageState =
  | { status: "loading" }
  | { status: "not_found" }
  | { status: "error"; message: string }
  | { status: "loaded"; file: FileResponse };

export function FilePage({ repo, path, line }: { repo: string; path: string; line: number | null }): JSX.Element {
  const [state, setState] = useState<FilePageState>({ status: "loading" });
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    setState({ status: "loading" });
    getFile(repo, path)
      .then((file) => setState({ status: "loaded", file }))
      .catch((err) => {
        // /api/file 404s (not a 200 with found:false) on a miss -- see webui/main.py:api_file.
        if (err instanceof ApiError && err.status === 404) {
          setState({ status: "not_found" });
          return;
        }
        const message = err instanceof ApiError ? err.message : "Failed to load file.";
        setState({ status: "error", message });
      });
  }, [repo, path]);

  function copyPermalink() {
    const url = new URL(window.location.href);
    void navigator.clipboard.writeText(url.toString()).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  }

  if (state.status === "loading") {
    return <div className="result-summary">Loading…</div>;
  }
  if (state.status === "not_found") {
    return (
      <div className="banner warn">
        File not found: {repo}/{path}
      </div>
    );
  }
  if (state.status === "error") {
    return <div className="banner error">{state.message}</div>;
  }
  if (state.file.content == null) {
    return (
      <div className="banner warn">
        File not found: {repo}/{path}
      </div>
    );
  }

  return (
    <div>
      <div className="file-view-header">
        <h2>
          {repo}/{path}
        </h2>
        <span className="badge">{state.file.branch}</span>
        <button type="button" className="theme-toggle" onClick={copyPermalink}>
          {copied ? "Copied!" : "Copy permalink"}
        </button>
      </div>
      <CodeBlock content={state.file.content} lang={detectLang(path)} targetLine={line} />
    </div>
  );
}
