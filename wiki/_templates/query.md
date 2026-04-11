---
title: <% tp.file.title %>
type: query
status: active
created: <% tp.date.now("YYYY-MM-DD") %>
updated: <% tp.date.now("YYYY-MM-DD") %>
summary: 
query: 
domain: 
tags: []
---

# <% tp.file.title %>

## Answer

<% tp.file.cursor(1) %>

## Reasoning



## Sources

```dataview
LIST
FROM "wiki"
WHERE contains(file.outlinks, this.file.link) AND file.name != this.file.name
SORT file.name ASC
```

## Related Queries

```dataview
LIST
FROM "wiki/queries"
WHERE file.name != this.file.name
SORT file.name ASC
```
