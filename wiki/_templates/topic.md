---
title: <% tp.file.title %>
type: topic
status: active
created: <% tp.date.now("YYYY-MM-DD") %>
updated: <% tp.date.now("YYYY-MM-DD") %>
summary: 
domain: 
tags: []
---

# <% tp.file.title %>

## Synthesis

<% tp.file.cursor(1) %>

## Key Components

### Concepts

```dataview
LIST summary
FROM "wiki/concepts"
WHERE contains(file.outlinks, this.file.link)
SORT file.name ASC
```

### Entities

```dataview
LIST summary
FROM "wiki/entities"
WHERE contains(file.outlinks, this.file.link)
SORT file.name ASC
```

## Sources

```dataview
TABLE source_type, ingested_on
FROM "wiki/sources"
WHERE contains(file.outlinks, this.file.link)
SORT ingested_on DESC
```

## Open Questions

- 

## Related Topics

```dataview
LIST
FROM "wiki/topics"
WHERE contains(file.outlinks, this.file.link) AND file.name != this.file.name
SORT file.name ASC
```
