---
title: <% tp.file.title %>
type: concept
status: active
created: <% tp.date.now("YYYY-MM-DD") %>
updated: <% tp.date.now("YYYY-MM-DD") %>
summary: 
aliases: []
domain: 
tags: []
---

# <% tp.file.title %>

## Definition

<% tp.file.cursor(1) %>

## Key Properties

- **Property 1**: 
- **Property 2**: 

## Related Concepts

```dataview
LIST
FROM "wiki/concepts"
WHERE contains(file.outlinks, this.file.link) AND file.name != this.file.name
SORT file.name ASC
```

## Related Entities

```dataview
LIST
FROM "wiki/entities"
WHERE contains(file.outlinks, this.file.link)
SORT file.name ASC
```

## Topics

```dataview
LIST
FROM "wiki/topics"
WHERE contains(file.outlinks, this.file.link)
SORT file.name ASC
```

## Sources

```dataview
TABLE summary, ingested_on
FROM "wiki/sources"
WHERE contains(file.outlinks, this.file.link)
SORT ingested_on DESC
```
