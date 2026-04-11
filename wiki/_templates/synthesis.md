---
title: <% tp.file.title %>
type: synthesis
status: draft
created: <% tp.date.now("YYYY-MM-DD") %>
updated: <% tp.date.now("YYYY-MM-DD") %>
summary: 
domain: 
tags: []
---

# <% tp.file.title %>

## Thesis

<% tp.file.cursor(1) %>

## Evidence

### Supporting
- 

### Contradicting
- 

## Analysis



## Implications



## Sources

```dataview
TABLE summary, source_type
FROM "wiki/sources"
WHERE contains(file.outlinks, this.file.link)
SORT ingested_on DESC
```

## Related

```dataview
LIST
FROM "wiki"
WHERE contains(file.outlinks, this.file.link) AND file.name != this.file.name
SORT file.name ASC
```
