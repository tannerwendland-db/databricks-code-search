import type { SearchFile, SearchMatch } from "../utils/searchReducer";
import { splitLineByByteRanges } from "../utils/byteRanges";

function fileHref(repo: string, path: string, line: number | null): string {
  const params = new URLSearchParams({ repo, path });
  const anchor = line != null ? `#L${line}` : "";
  return `/file?${params.toString()}${anchor}`;
}

function MatchLine({ repo, path, match }: { repo: string; path: string; match: SearchMatch }): JSX.Element {
  if (match.symbols && match.symbols.length > 0) {
    return (
      <div className="result-line">
        <a className="line-no" href={fileHref(repo, path, match.line)}>
          {match.line ?? ""}
        </a>
        <span className="line-text">
          {match.symbols.map((sym, i) => (
            <span key={i}>
              <mark>{sym.name}</mark> <span className="lang">({sym.kind})</span>{" "}
            </span>
          ))}
        </span>
      </div>
    );
  }
  const segments = splitLineByByteRanges(match.text, match.byte_ranges);
  return (
    <div className="result-line">
      <a className="line-no" href={fileHref(repo, path, match.line)}>
        {match.line ?? ""}
      </a>
      <span className="line-text">
        {segments.map((seg, i) =>
          seg.highlighted ? <mark key={i}>{seg.text}</mark> : <span key={i}>{seg.text}</span>
        )}
      </span>
    </div>
  );
}

export function ResultsList({ files }: { files: SearchFile[] }): JSX.Element {
  return (
    <div>
      {files.map((file) => (
        <div className="result-file" key={`${file.repo}:${file.file}`}>
          <div className="result-file-header">
            <a href={fileHref(file.repo, file.file, null)}>
              {file.repo}/{file.file}
            </a>
            {file.language && <span className="lang">{file.language}</span>}
          </div>
          {file.matches.map((match, i) => (
            <MatchLine key={i} repo={file.repo} path={file.file} match={match} />
          ))}
        </div>
      ))}
    </div>
  );
}
