---
name: ml-agentic-goal
description: Cy Score weight pipeline exists to use ML + an agentic LangGraph loop; never propose dropping either as "simpler"
trigger: always_on
---

The Cy Score weight pipeline (`rough_framework.md`) is deliberately built around ML (learning-to-rank) and an agentic loop (LangGraph + Claude). Using those tools is a core goal, not an implementation detail. Scope is learning the weights only; deploying them into the lambda is a separate concern.

**Why:** Replacing LangGraph with a plain loop or dropping the agent phase defeats the purpose of the project ("The whole thing is to utilise ML and agentic"). Likewise, the lambda's deployment constraints are out of scope and must never be raised as a blocker.

**How to apply:** Simplify *within* the ML/agentic design (fewer steps, less duplication), but never recommend removing the ML model, the agent, or LangGraph. When the agent looks underused, suggest giving it more real decisions instead.
