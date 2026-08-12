// Build-time resilience patch for the vendored Self-hosted LiveSync core (the `lib/` submodule of
// livesync-bridge, pinned by BRIDGE_REF in the Dockerfile).
//
// Two failure modes in DirectFileManipulatorV2.beginWatch() / followUpdates() let a single bad
// document halt the whole CouchDB changes feed, so a bulk fetch-from-first replay (re-materializing
// the vault CouchDB -> disk on a fresh index) silently stops partway and leaves the mirror short:
//
//   1. The per-document fetch runs OUTSIDE the try/catch that guards the sync callback:
//        const docX = await this.getByMeta(doc);   // <-- unguarded
//        try { await callback(docX, change.seq); } catch (ex) { ...log... }
//      getByMeta() THROWS on a corrupted document ("Corrupted document: <path>"). The rejection
//      escapes the handler and stalls the feed. (Observed: a 451 MB corrupted `.rtb`.)
//   2. Even guarded, reassembling/decoding a very large binary is expensive enough to throw
//      ("RangeError: string too long", a 34 MB PDF) or to monopolize the event loop.
//
// Fix (applied to BOTH watch handlers):
//   - Skip documents over MAX_MATERIALIZE_BYTES *before* the expensive getByMeta() — such files
//     cannot reliably materialize to the disk mirror anyway; they stay safe in CouchDB and on every
//     Obsidian device, and the feed keeps going.
//   - Move getByMeta() INSIDE the try/catch so any remaining bad document is logged and SKIPPED
//     instead of stalling every document sequenced after it.
//
// Self-verifying: if the target pattern is gone (an upstream BRIDGE_REF bump changed the code),
// the build FAILS here instead of shipping an unpatched, silently-stalling bridge.
const target = "/app/lib/src/API/DirectFileManipulatorV2.ts";
const MAX = 26214400; // 25 MiB — above this a doc is kept in CouchDB but not materialized to disk.
const src = Deno.readTextFileSync(target);

// Match the original (unpatched) shape in both beginWatch() and followUpdates():
//   const docX = await this.getByMeta(doc);
//   <p1>try {
//   <p2>await callback(docX, change.seq);
const pattern =
  /const docX = await this\.getByMeta\(doc\);\n(\s*)try \{\n(\s*)await callback\(docX, change\.seq\);/g;
const sites = (src.match(pattern) ?? []).length;
if (sites < 1) {
  console.error(
    `[resilience-patch] target pattern not found in ${target} — the vendored LiveSync core changed ` +
      `(BRIDGE_REF bump?). Re-verify beginWatch/followUpdates and update this patch.`,
  );
  Deno.exit(1);
}

const patched = src.replace(pattern, (_m, _p1: string, p2: string) =>
  `try {\n` +
  `${p2}if (((doc as any).size ?? 0) > ${MAX}) { Logger(\`WATCH: SKIP oversized (\${(doc as any).size} bytes): \${doc.path} — kept in CouchDB, not materialized to disk\`, LEVEL_INFO, "watch"); return; }\n` +
  `${p2}const docX = await this.getByMeta(doc);\n` +
  `${p2}await callback(docX, change.seq);`
);
Deno.writeTextFileSync(target, patched);
console.log(`[resilience-patch] size-guarded + try/guarded getByMeta() at ${sites} watch site(s).`);
