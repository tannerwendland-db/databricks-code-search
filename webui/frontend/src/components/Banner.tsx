import { recognize } from "../utils/queryModel";
import type { SearchBanners } from "../utils/searchReducer";

// Banners come ONLY from these envelope fields (ralplan-35 A2 contract) -- never inferred
// from an empty `files` list, which is legitimately reachable via no_content_atom /
// zero_width_only_atoms without any of these signals firing. `resolved` / `commitNotIndexed`
// (ralplan-commit-hash-search step 6) extend this the same way: additive envelope fields, not
// inferred from `files`.

/** The `resolved` payload has no "typed prefix" field (see ResolvedCommit) -- match each
 * resolution back to whichever `commit:` atom in the searched query it satisfies, so the
 * banner can echo what the user typed rather than the full resolved SHA. */
function commitPrefixesFrom(query: string): string[] {
  const model = recognize(query);
  if (!model.safe) return [];
  return model.atoms.filter((a) => a.field === "commit").map((a) => a.value);
}

function matchedPrefix(commit: string, prefixes: string[]): string {
  const hit = prefixes.find((p) => commit.toLowerCase().startsWith(p.toLowerCase()));
  return hit ?? commit.slice(0, 12);
}

export function SearchBannerList({ banners, query }: { banners: SearchBanners; query: string }): JSX.Element | null {
  const items: { key: string; tone: "warn" | "error" | "info"; text: string }[] = [];

  if (banners.queryParseError) {
    items.push({ key: "parse", tone: "error", text: `Query error: ${banners.queryParseError}` });
  }
  if (banners.queryTooBroad) {
    items.push({ key: "broad", tone: "warn", text: "Query too broad — results were cut short by the time budget." });
  }
  if (banners.regexIncompatible) {
    items.push({ key: "regex", tone: "warn", text: "One or more /regex/ atoms are not supported and were ignored." });
  }
  if (banners.truncated) {
    const reason = banners.truncationReason ? ` (${banners.truncationReason})` : "";
    items.push({ key: "truncated", tone: "warn", text: `Results truncated${reason}.` });
  }
  if (banners.commitNotIndexed) {
    items.push({ key: "commit-not-indexed", tone: "warn", text: "No indexed branch at this commit." });
  }
  if (banners.resolved.length > 0) {
    const prefixes = commitPrefixesFrom(query);
    banners.resolved.forEach((r, idx) => {
      const prefix = matchedPrefix(r.commit, prefixes);
      items.push({
        key: `resolved-${idx}`,
        tone: "info",
        text: `✓ ${prefix} → ${r.repo} @ ${r.branch} (${r.commit.slice(0, 12)})`,
      });
    });
  }

  if (items.length === 0) return null;
  return (
    <>
      {items.map((item) => (
        <div key={item.key} className={`banner ${item.tone}`}>
          {item.text}
        </div>
      ))}
    </>
  );
}
