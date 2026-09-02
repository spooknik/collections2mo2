---
name: Bug report
about: Something went wrong converting or managing a collection
title: ""
labels: bug
---

<!--
Before filing: never paste your Nexus API key, your .env file, or anything from
Windows Credential Manager. If a log line contains it, redact it first.
-->

## Collection

- Collection URL:
- Revision (if known, e.g. from `c2wj status` or the URL's `?revision=` query):

## Instance

- Instance path (the folder you passed to `--out`/`--instance`/the GUI):
- Does `c2wj-instance.json` exist in that folder? (yes/no)
- If yes, please attach it or paste its contents (it does not contain your API key).
- How was the instance built: GUI wizard, or which `c2wj` command(s)?

## What happened

<!-- What you expected, what happened instead. A screenshot helps if it's a GUI issue. -->

## Log output

<!--
Paste the relevant lines from the terminal (CLI) or the Progress/Manage page's log
pane (GUI). Include the stage the failure happened in (fetch/download/inspect/
install/profile/build) and a few lines of context before the error, not just the
last line.
-->

```
paste here
```

## Environment

- `c2wj --version` / commit or release you're on:
- Windows version:
- Nexus Premium: yes/no
