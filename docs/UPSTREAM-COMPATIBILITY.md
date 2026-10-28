# Upstream Compatibility Snapshot

## AstrBot

- Repository: `https://github.com/AstrBotDevs/AstrBot`
- Stable release checked: `v4.29.3`
- Release tag object: `f61a7f42696da6df25dc7a249ec9b8d54ed9da18`
- Main branch head checked: `dad255c60f7983fd63c7c0282ded1dde859fbc9a`
- Checked: 2026-10-27
- Local source snapshot: `E:\plugins\tmp\astrbot-v4.29.3.tar.gz`

### Support levels

| Level | Meaning |
| --- | --- |
| E | Real execution through a Neo-MoFox public API or OneBot service |
| D | Importable/constructable data compatibility; execution is validated and may reject |
| I | Import compatibility only; execution raises a stable capability error |
| R | Admission rejection because the declaration cannot be safely represented |

| AstrBot surface | Level | Adapter behavior |
| --- | --- | --- |
| `Star`, `Context`, lifecycle hooks | E | Per-plugin isolated unit with activation and bounded unload |
| command/regex/message-type/platform/permission filters | E | Strict event matching and explicit permission mapping |
| OneBot message components and send/event results | E | Validated segment conversion through `onebot_expand`, including `AtAll` |
| tools, task registration, web APIs, LLM request/response hooks | E | Pre-registered tools and source-bound runtime dispatch |
| provider data models, platform markers, and public SDK exports | D | Importable models with explicit host mapping requirements |
| `astrbot.api.util` and `astrbot.api.web` request/response models | D/I | Public paths import; session waiters, renderer responses, and persistence calls fail explicitly without host equivalents |
| unsupported platform adapters, arbitrary gateways, full provider manager, renderer internals | I/R | Stable capability error or admission rejection; no fake provider/renderer |

The shim is installed only for the external plugin import scope. It must never overwrite a real AstrBot runtime already present in the process.

## Validation note

- The import surface is validated by `tests/test_compat_surface.py`; strict segment conversion and `AtAll` have direct tests.
- Bundled plugin runtime admission requires `onebot_expand` to have registered `message_service` and `account_service`; these services are not registered in the isolated test process, so full host activation remains an integration check for the running MoFox instance.
- Platform registration, renderer internals, session waiters, non-chat provider execution, and agent loops remain explicit gaps; no false-success stubs are advertised as executable support.

## Bundled plugin smoke test

- `astrbot_plugin_wifepicker` reached `PREPARED` and produced bridge components when the required host service registrations were supplied by the smoke-test harness.
- `astrbot_plugin_box` was isolated during import because its declared `emoji` dependency is not installed in this environment. The adapter intentionally does not install plugin requirements at runtime; install the plugin's declared requirements in the target environment before expecting it to load.
