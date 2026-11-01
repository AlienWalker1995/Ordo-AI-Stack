import type { WordTiming, WordGroup } from "../types";

/**
 * Group words into caption segments for preview rendering.
 * Mirrors Python's _group_words() + _build_segments() logic.
 */
export function groupWordsForPreview(
  words: WordTiming[],
  maxWords: number = 3,
  pauseThreshold: number = 0.3
): WordGroup[] {
  if (words.length === 0) return [];

  // Partition by track
  const byTrack = new Map<number, WordTiming[]>();
  for (const w of words) {
    const track = w.track || 0;
    if (!byTrack.has(track)) byTrack.set(track, []);
    byTrack.get(track)!.push(w);
  }

  const groups: WordGroup[] = [];
  let segIdx = 0;

  for (const trackId of [...byTrack.keys()].sort((a, b) => a - b)) {
    const trackWords = byTrack.get(trackId)!;
    let currentCount = 0;
    let currentGroup: WordTiming[] = [];

    for (let i = 0; i < trackWords.length; i++) {
      const w = trackWords[i];

      if (i === 0) {
        currentCount = 1;
        currentGroup = [w];
      } else {
        const gap = w.start - trackWords[i - 1].end;
        if (currentCount >= maxWords || gap > pauseThreshold) {
          // Finalize current group
          if (currentGroup.length > 0) {
            groups.push({
              words: currentGroup,
              start: currentGroup[0].start,
              end: currentGroup[currentGroup.length - 1].end,
              segmentIdx: segIdx,
              track: trackId,
            });
            segIdx++;
          }
          currentCount = 1;
          currentGroup = [w];
        } else {
          currentCount++;
          currentGroup.push(w);
        }
      }
    }

    // Finalize last group for this track
    if (currentGroup.length > 0) {
      groups.push({
        words: currentGroup,
        start: currentGroup[0].start,
        end: currentGroup[currentGroup.length - 1].end,
        segmentIdx: segIdx,
        track: trackId,
      });
      segIdx++;
    }
  }

  return groups;
}
