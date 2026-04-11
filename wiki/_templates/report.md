---
title: <% tp.file.title %>
type: report
status: active
created: <% tp.date.now("YYYY-MM-DD") %>
updated: <% tp.date.now("YYYY-MM-DD") %>
summary: 
report_type: <%* const rtype = await tp.system.suggester(["lint","audit","quality","contradiction","other"], ["lint","audit","quality","contradiction","other"]); tR += rtype; %>
domain: 
tags: []
---

# <% tp.file.title %>

## Summary

<% tp.file.cursor(1) %>

## Findings

- 

## Actions

- [ ] 

## Pages Reviewed

```dataview
LIST
FROM "wiki"
WHERE contains(file.outlinks, this.file.link) AND file.name != this.file.name
SORT file.name ASC
```
