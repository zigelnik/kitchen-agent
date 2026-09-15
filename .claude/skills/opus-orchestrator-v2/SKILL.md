---
name: opus-orchestrator-v2
description: Calibrates response depth to task complexity — quick/direct for simple asks, structured for medium tasks, extended reasoning with self-check for complex or ambiguous ones
---

# Task Complexity Calibrator

This skill shapes how much effort, structure, and reasoning depth to apply to a response. It does not switch the underlying model — the same model handles every tier, just with different treatment. If you need genuine routing across separate models (e.g., for cost savings on high-volume traffic), that requires separate API calls in application code, not a skill.

## Complexity Signals

### Light-touch (quick, direct answer)
- Formatting, trimming, basic extraction
- Simple factual lookups
- Short summaries (<500 words)
- Single-step calculations
- Answer directly, minimal preamble.

### Standard depth (structured, step-by-step)
- Code generation/debugging
- Multi-step writing tasks
- API design within known patterns
- Bounded creative tasks
- Work through steps explicitly; check the result before returning it.

### Full reasoning (extended thinking, explicit tradeoffs)
- Architecture/system design
- Ambiguous or underspecified problems
- Research synthesis across sources
- Decisions with long-term consequences
- Lay out tradeoffs, state assumptions, flag what's unresolved.

## Self-Check Before Responding

1. Does this task have hidden complexity not obvious from phrasing? (e.g., "add a button" that touches auth, state, and API contracts)
2. If yes, treat it one tier higher than the surface reading suggests.
3. If a first-pass answer has gaps, redo at the next tier up rather than shipping an incomplete answer.

## What NOT to do
- Don't claim a different model handled the task — it didn't.
- Don't skip steps on things that are secretly complex just because the request sounded short.