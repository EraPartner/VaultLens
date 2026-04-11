---
title: <% tp.file.title %>
type: entity
status: active
created: <% tp.date.now("YYYY-MM-DD") %>
updated: <% tp.date.now("YYYY-MM-DD") %>
summary: 
entity_type: <%* const etype = await tp.system.suggester(["person","organization","tool","place","artifact"], ["person","organization","tool","place","artifact"]); tR += etype; %>
aliases: []
domain: 
tags: []
---

# <% tp.file.title %>

## Overview

<% tp.file.cursor(1) %>

## Properties

| Property | Value |
|----------|-------|
| Type | <% etype %> |
| Status | active |

## Connections

### Related Concepts

```dataview
LIST
FROM "wiki/concepts"
WHERE contains(file.outlinks, this.file.link)
SORT file.name ASC
```

### Related Topics

```dataview
LIST
FROM "wiki/topics"
WHERE contains(file.outlinks, this.file.link)
SORT file.name ASC
```

### Mentioned In Sources

```dataview
TABLE source_type, ingested_on
FROM "wiki/sources"
WHERE contains(file.outlinks, this.file.link)
SORT ingested_on DESC
```
