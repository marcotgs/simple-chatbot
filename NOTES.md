# Notes

## Time spent

Around 6.5 hours.

## AI usage log

- Tools: I mainly used Claude Code, with a team of agents.
- Models: Sonnet 5.5 for the orchestration agent, and Opus 5.5 for the executor agents.
- How the work was split between them: the Vinicius agent (orchestration) ran only in plan mode and was responsible for dispatching sub-agents. The sub-agents were the ones using Opus 5.5.

## Two mistakes the AI made that I caught

1. Using OpenAI endpoints: it used the wrong model, so it was not using my OpenRouter key correctly.
2. Prompt tuning with specific scenarios in the prompt: the prompt contained a literal sentence from a real Slack conversation plus test fixture names. I had it rewritten as generic principles with placeholders.

## One AI suggestion I rejected

1. No LLM judge for the extractor: it was doing something like fuzzy matching to extract the facts from the message. I rejected that and told the model to use an LLM-as-judge instead.

## What I would do next

These are my next steps:

1. Deployment (I started on it, but decided to leave it out).
2. Use Agent Builder or something else to better handle prompts.
3. An analytics dashboard.
4. A feature that lets an admin tweak the bot (conversation rules, etc.).
