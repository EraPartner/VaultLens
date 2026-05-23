---
title: <% tp.file.title %>
type: comparison
status: draft
created: <% tp.date.now("YYYY-MM-DD") %>
updated: <% tp.date.now("YYYY-MM-DD") %>
summary: 
comparisons:
  - item_a
  - item_b
domain: 
tags: []
confidence: 
volatility: 
---

# <% tp.file.title %>

## Summary

<% tp.file.cursor(1) %>

## Dimensions

| Dimension | Item A | Item B |
|-----------|--------|--------|
| | | |
| | | |
| | | |

## Analysis



## Verdict



## Sources

```dataview
TABLE summary, source_type
FROM "wiki/sources"
WHERE contains(file.outlinks, this.file.link)
SORT ingested_on DESC
```
