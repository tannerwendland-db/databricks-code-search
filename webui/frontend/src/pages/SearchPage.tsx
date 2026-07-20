import { useEffect, useReducer, useRef, useState } from "react";
import { ApiError, listRepos, searchCode, type RepoInfo } from "../api/client";
import { SearchBannerList } from "../components/Banner";
import { FilterChips } from "../components/FilterChips";
import { ResultsList } from "../components/ResultsList";
import { SyntaxHelp } from "../components/SyntaxHelp";
import { replaceRoute } from "../router";
import { initialSearchState, searchReducer } from "../utils/searchReducer";

/** Replace (or remove) a single `prefix:value` atom in a zoekt-style query string. */
function setAtom(query: string, prefix: string, value: string | null): string {
  const withoutAtom = query
    .replace(new RegExp(`\\b${prefix}:\\S+`, "g"), "")
    .replace(/\s+/g, " ")
    .trim();
  if (!value) return withoutAtom;
  return withoutAtom.length > 0 ? `${withoutAtom} ${prefix}:${value}` : `${prefix}:${value}`;
}

function extractAtom(query: string, prefix: string): string | null {
  const match = query.match(new RegExp(`\\b${prefix}:(\\S+)`));
  return match ? match[1] : null;
}

export function SearchPage({ initialQuery }: { initialQuery: string }): JSX.Element {
  const [input, setInput] = useState(initialQuery);
  const [state, dispatch] = useReducer(searchReducer, initialSearchState);
  const [repos, setRepos] = useState<RepoInfo[]>([]);
  // Guards the mount-time auto-search so StrictMode's double-invoke (dev only) can't double-fire.
  const ranInitial = useRef(false);

  useEffect(() => {
    listRepos()
      .then((res) => setRepos(res.repos))
      .catch(() => setRepos([])); // repo chips are a convenience; a failed fetch shouldn't block search
  }, []);

  async function runSearch(query: string) {
    if (!query.trim()) return;
    replaceRoute(`/?q=${encodeURIComponent(query)}`);
    dispatch({ type: "search_start", query });
    try {
      const payload = await searchCode(query, { cursor: null });
      dispatch({ type: "search_success", payload });
    } catch (err) {
      const message = err instanceof ApiError ? err.message : "Search request failed.";
      dispatch({ type: "search_error", error: message });
    }
  }

  useEffect(() => {
    if (ranInitial.current) return;
    ranInitial.current = true;
    if (initialQuery.trim()) {
      void runSearch(initialQuery);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function loadMore() {
    if (!state.cursor) return;
    dispatch({ type: "load_more_start" });
    try {
      const payload = await searchCode(state.query, { cursor: state.cursor });
      dispatch({ type: "load_more_success", payload });
    } catch (err) {
      const message = err instanceof ApiError ? err.message : "Failed to load more results.";
      dispatch({ type: "load_more_error", error: message });
    }
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    void runSearch(input);
  }

  function toggleRepo(name: string) {
    const active = extractAtom(input, "repo");
    const next = setAtom(input, "repo", active === name ? null : name);
    setInput(next);
    void runSearch(next);
  }

  function toggleLanguage(lang: string) {
    const active = extractAtom(input, "lang");
    const next = setAtom(input, "lang", active === lang ? null : lang);
    setInput(next);
    void runSearch(next);
  }

  const languages = Array.from(
    new Set(state.files.map((f) => f.language).filter((l): l is string => Boolean(l)))
  ).sort();

  return (
    <div>
      <form className="search-box" onSubmit={handleSubmit}>
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder='e.g. repo:myrepo lang:go "http.Handler"'
          aria-label="Search query"
          autoFocus
        />
        <button type="submit">Search</button>
      </form>

      <SyntaxHelp />

      <FilterChips
        repos={repos}
        languages={languages}
        activeRepo={extractAtom(input, "repo")}
        activeLanguage={extractAtom(input, "lang")}
        onToggleRepo={toggleRepo}
        onToggleLanguage={toggleLanguage}
      />

      <SearchBannerList banners={state.banners} />

      {state.status === "error" && <div className="banner error">{state.error}</div>}

      {state.hasSearched && state.status !== "loading" && (
        <div className="result-summary">
          {state.fileCount} file{state.fileCount === 1 ? "" : "s"}, {state.matchCount} match
          {state.matchCount === 1 ? "" : "es"}
        </div>
      )}

      {state.status === "loading" && <div className="result-summary">Searching…</div>}

      <ResultsList files={state.files} />

      {state.cursor && (
        <button type="button" className="load-more" onClick={() => void loadMore()} disabled={state.status === "loading_more"}>
          {state.status === "loading_more" ? "Loading…" : "Load more"}
        </button>
      )}
    </div>
  );
}
