// Match highlighting for search results.
//
// The backend's `byte_ranges` are UTF-8 byte offsets into the line, half-open, line-local
// (app/search/grep.py). JavaScript strings are UTF-16 code units, so `String.slice(start, end)`
// with those same numbers is wrong for any line containing a multi-byte character (accents,
// CJK, emoji) that appears before or inside a highlighted span -- the byte offset and the
// UTF-16 index diverge. This module re-encodes the line with `TextEncoder`, slices the BYTE
// array at the given offsets, and decodes each slice back with `TextDecoder` so highlight
// boundaries always land on the codepoints the backend actually matched.

export type ByteRange = readonly [number, number];

export interface HighlightSegment {
  text: string;
  highlighted: boolean;
}

/** Split `line` into alternating plain/highlighted segments per `ranges` (UTF-8 byte offsets). */
export function splitLineByByteRanges(line: string, ranges: readonly ByteRange[]): HighlightSegment[] {
  const bytes = new TextEncoder().encode(line);
  if (ranges.length === 0) {
    return [{ text: line, highlighted: false }];
  }

  const merged = mergeRanges(ranges, bytes.length);
  const decoder = new TextDecoder("utf-8");
  const segments: HighlightSegment[] = [];
  let cursor = 0;
  for (const [start, end] of merged) {
    if (start > cursor) {
      segments.push({ text: decoder.decode(bytes.subarray(cursor, start)), highlighted: false });
    }
    segments.push({ text: decoder.decode(bytes.subarray(start, end)), highlighted: true });
    cursor = end;
  }
  if (cursor < bytes.length) {
    segments.push({ text: decoder.decode(bytes.subarray(cursor)), highlighted: false });
  }
  return segments;
}

/** Clamp, drop empty/out-of-bounds ranges, sort, and merge overlaps/adjacency. */
function mergeRanges(ranges: readonly ByteRange[], lineByteLength: number): [number, number][] {
  const clamped = ranges
    .map(([s, e]): [number, number] => [Math.max(0, s), Math.min(lineByteLength, e)])
    .filter(([s, e]) => e > s)
    .sort((a, b) => a[0] - b[0]);

  const merged: [number, number][] = [];
  for (const range of clamped) {
    const last = merged[merged.length - 1];
    if (last && range[0] <= last[1]) {
      last[1] = Math.max(last[1], range[1]);
    } else {
      merged.push(range);
    }
  }
  return merged;
}
