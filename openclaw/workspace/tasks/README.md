# Tasks / Scratchpad

`active.md` is the single-slot working-memory scratchpad for the agent. See `AGENTS.md` → "Plan-and-Execute Protocol" and "Scratchpad Protocol" for when and how to write it.

## Schema

```yaml
goal: <one sentence — what the user asked for>
acceptance:
  - <observable criterion the user would check>
  - ...
plan:
  - step: 1
    action: <short description>
    tool: <tool name or "none">
    done: false
  - step: 2
    ...
status: in_progress | done | blocked
next_action: <step number or "finalize">
artifacts:
  - path: <absolute path to the file/output produced>
    step: <step number that produced it>
notes: <optional, for evidence citations from tool calls>
```

## Rules

- Single active task at a time. Overwrite `active.md` when a new multi-step task begins.
- Update after every step (set `done: true`, increment `next_action`).
- On task completion, set `status: done` and leave the file until the next task.
- On post-compaction turns: first action is `read_file tasks/active.md`; resume from `next_action` if `status: in_progress`.
- Never cite the scratchpad to the user as a deliverable. It is internal state.

## Why this exists

The local model's context is periodically compacted, which loses in-flight task state. Disk-backed scratchpad survives compaction. Research: Nye et al. "Show Your Work" (arXiv 2112.00114), Mem0 (2504.19413), CORAL (OpenReview NBGlItueYE), Plan-and-Act (2503.09572). Reduces stall rate driven by per-step error compounding (arXiv 2603.29231).
