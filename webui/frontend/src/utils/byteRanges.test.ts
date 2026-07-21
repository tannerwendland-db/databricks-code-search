import { describe, expect, it } from "vitest";
import { splitLineByByteRanges } from "./byteRanges";

/** Compute the UTF-8 byte offset range of `substr`'s first occurrence in `line`. */
function byteOffsetOf(line: string, substr: string): [number, number] {
  const idx = line.indexOf(substr);
  if (idx === -1) throw new Error(`"${substr}" not found in "${line}"`);
  const encoder = new TextEncoder();
  const start = encoder.encode(line.slice(0, idx)).length;
  const end = start + encoder.encode(substr).length;
  return [start, end];
}

describe("splitLineByByteRanges", () => {
  it("returns the whole line unhighlighted when there are no ranges", () => {
    expect(splitLineByByteRanges("hello world", [])).toEqual([
      { text: "hello world", highlighted: false },
    ]);
  });

  it("highlights a plain-ASCII substring", () => {
    const line = "the quick brown fox";
    const [start, end] = byteOffsetOf(line, "quick");
    expect(splitLineByByteRanges(line, [[start, end]])).toEqual([
      { text: "the ", highlighted: false },
      { text: "quick", highlighted: true },
      { text: " brown fox", highlighted: false },
    ]);
  });

  it("highlights a multi-byte CJK substring using byte offsets, not a char slice", () => {
    const line = "prefix 世界 suffix";
    const [start, end] = byteOffsetOf(line, "世界");
    const segments = splitLineByByteRanges(line, [[start, end]]);
    expect(segments).toEqual([
      { text: "prefix ", highlighted: false },
      { text: "世界", highlighted: true },
      { text: " suffix", highlighted: false },
    ]);
    // Sanity check that this line actually exercises the byte/UTF-16 divergence: naive
    // String.slice(start, end) on the SAME numbers would not recover "世界" here.
    expect(line.slice(start, end)).not.toBe("世界");
  });

  it("highlights an astral emoji (surrogate pair in UTF-16, 4 bytes in UTF-8)", () => {
    const line = "go 🚀 far";
    const [start, end] = byteOffsetOf(line, "🚀");
    const segments = splitLineByByteRanges(line, [[start, end]]);
    expect(segments.find((s) => s.highlighted)?.text).toBe("🚀");
    expect(line.slice(start, end)).not.toBe("🚀");
  });

  it("preserves multiple ranges regardless of input order and merges adjacency", () => {
    const line = "aa bb cc";
    const [aStart, aEnd] = byteOffsetOf(line, "aa");
    const [cStart, cEnd] = byteOffsetOf(line, "cc");
    const segments = splitLineByByteRanges(line, [
      [cStart, cEnd],
      [aStart, aEnd],
    ]);
    expect(segments.map((s) => s.text)).toEqual(["aa", " bb ", "cc"]);
    expect(segments.map((s) => s.highlighted)).toEqual([true, false, true]);
  });

  it("merges overlapping ranges into a single highlighted segment", () => {
    const line = "abcdef";
    const segments = splitLineByByteRanges(line, [
      [0, 3],
      [2, 5],
    ]);
    expect(segments).toEqual([
      { text: "abcde", highlighted: true },
      { text: "f", highlighted: false },
    ]);
  });

  it("clamps out-of-bounds ranges and drops empty ones", () => {
    const line = "hello";
    const segments = splitLineByByteRanges(line, [
      [0, 0],
      [-3, 2],
      [3, 999],
    ]);
    expect(segments).toEqual([
      { text: "he", highlighted: true },
      { text: "l", highlighted: false },
      { text: "lo", highlighted: true },
    ]);
  });
});
