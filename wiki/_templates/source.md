---
title: <% tp.file.title %>
type: source
status: draft
created: <% tp.date.now("YYYY-MM-DD") %>
updated: <% tp.date.now("YYYY-MM-DD") %>
summary: 
source_id: <% tp.user ? tp.user.next_id : "src-" + tp.date.now("YYYY-MM-DD") + "-001" %>
source_type: <%* const stype = await tp.system.suggester(["article","paper","book","pdf","video","podcast","dataset","note","other"], ["article","paper","book","pdf","video","podcast","dataset","note","other"]); tR += stype; %>
origin: 
ingested_on: <% tp.date.now("YYYY-MM-DD") %>
domain: 
tags: []
---

# <% tp.file.title %>

## Overview

<% tp.file.cursor(1) %>

## Key Claims

- **Claim 1**: 
- **Claim 2**: 
- **Claim 3**: 

## Notes



## Pages Referencing This Source

```dataview
LIST
FROM "wiki"
WHERE contains(file.outlinks, this.file.link)
SORT file.name ASC
```

## Sources

- 
