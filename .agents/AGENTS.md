# Bot Workspace Rules

## 1. Strict Configuration Access (Fail-Fast)
**CRITICAL RULE**: Do NOT use `.get(key, default)` when accessing configuration dictionaries (e.g., `_CFG`, `app.json`, `_base.json`). 
- **Always use strict dictionary indexing**: e.g., `_CFG["app"]["symbols"]`.
- **Reasoning**: If a required configuration key is missing, the application MUST crash immediately at initialization with a `KeyError`. Using `.get()` with safe defaults causes silent logic failures that are extremely hard to debug in production. 
- **Exception**: You may use `.get()` ONLY if the missing key is genuinely optional and the fallback behavior is explicitly intended and documented as safe by the user.
