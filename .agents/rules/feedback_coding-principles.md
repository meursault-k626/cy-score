---
name: coding-principles
description: KISS first, then DRY, then YAGNI — never overengineer
trigger: always_on
---

Priority order for code decisions: KISS → DRY → YAGNI. Never overengineer.

**Why:** Simplicity is the top priority. Abstractions and cleverness only earn their place after the simple version proves insufficient.

**How to apply:** Start with the simplest thing that works. Only extract/abstract when repetition genuinely hurts. Never build for hypothetical future needs. Three similar lines beat a premature abstraction every time.
