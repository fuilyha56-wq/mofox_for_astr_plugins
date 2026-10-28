---
name: astrbot-mofox-compat
description: 'Synchronize AstrBot SDK compatibility snapshots, scan public exports, load trusted AstrBot Star plugins in Neo-MoFox, validate OneBot mappings and lifecycle, and produce a compatibility gap report. Use for AstrBot upgrades, plugin load failures, SDK shim coverage, or adapter maintenance.'
argument-hint: '[AstrBot release or plugin path]'
user-invocable: true
disable-model-invocation: false
---

# AstrBot MoFox Compatibility

## Scope

Maintain `MofoxForAstrPulgins` as a trusted, in-process Star compatibility bridge for the host capabilities that have real Neo-MoFox/OneBot mappings. SDK import compatibility is not equivalent to runtime support.

## Procedure

1. Read the pinned release, commit, and support levels in [UPSTREAM-COMPATIBILITY.md](../../docs/UPSTREAM-COMPATIBILITY.md).
2. Inspect the requested AstrBot release's `astrbot/api` exports, event/filter enums, provider entities, message components, and lifecycle contracts. Compare them to `sdk/` and `plugin.py`.
3. Classify each export and behavior as `E` real execution, `D` data compatibility, `I` import compatibility, or `R` admission rejection. Record all newly added or removed public paths.
4. Preserve strict plugin discovery and load sequencing: import and prepare units before host component registration; activate only after registration; isolate failures; drain work and remove routes/modules on unload.
5. For message paths, preserve ordered segments and require a real OneBot service mapping. Never coerce unsupported content to text.
6. For tools, events, providers, LLM hooks, tasks, and web APIs, bind calls to the correct unit and source stream. Do not use a global “latest event” fallback.
7. Validate local `astrbot_plugin_box` and `astrbot_plugin_wifepicker` imports and all behavior that has host mappings. Keep unmapped providers, platform registration, rendering, session waiters, and agent loops as explicit stable errors.
8. Update [README.md](../../README.md), [DESIGN.md](../../docs/DESIGN.md), and the release snapshot. Add import-surface and behavior tests for every change.
9. Run focused tests and report required MoFox/OneBot integrations that are unavailable in the current test environment.

## Rules

- Never overwrite a real `astrbot` package already in `sys.modules`.
- Load only trusted external code; this bridge is not a sandbox.
- Do not modify Neo-MoFox core from this adapter skill.
- Do not mark `D`, `I`, or `R` entries as implemented behavior.
- Keep upstream version and commit pins exact; update tests and compatibility docs with each baseline change.
