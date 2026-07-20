import type { SearchBanners } from "../utils/searchReducer";

// Banners come ONLY from these envelope fields (ralplan-35 A2 contract) -- never inferred
// from an empty `files` list, which is legitimately reachable via no_content_atom /
// zero_width_only_atoms without any of these signals firing.
export function SearchBannerList({ banners }: { banners: SearchBanners }): JSX.Element | null {
  const items: { key: string; tone: "warn" | "error"; text: string }[] = [];

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
