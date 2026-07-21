import type { RepoInfo } from "../api/client";

export interface FilterChipsProps {
  repos: RepoInfo[];
  languages: string[];
  activeRepo: string | null;
  activeLanguage: string | null;
  onToggleRepo: (name: string) => void;
  onToggleLanguage: (lang: string) => void;
}

// Chips compose `repo:`/`lang:` atoms into the query string (owned by SearchPage); they never
// call the API directly. `languages` is derived from the languages seen in the current result
// set rather than a separate endpoint, keeping this lean (no extra backend surface for v1).
export function FilterChips({
  repos,
  languages,
  activeRepo,
  activeLanguage,
  onToggleRepo,
  onToggleLanguage,
}: FilterChipsProps): JSX.Element | null {
  if (repos.length === 0 && languages.length === 0) return null;
  return (
    <div className="chips">
      {repos.map((repo) => (
        <button
          key={`repo-${repo.name}`}
          type="button"
          className={`chip${activeRepo === repo.name ? " active" : ""}`}
          onClick={() => onToggleRepo(repo.name)}
        >
          repo:{repo.name}
        </button>
      ))}
      {languages.map((lang) => (
        <button
          key={`lang-${lang}`}
          type="button"
          className={`chip${activeLanguage === lang ? " active" : ""}`}
          onClick={() => onToggleLanguage(lang)}
        >
          lang:{lang}
        </button>
      ))}
    </div>
  );
}
