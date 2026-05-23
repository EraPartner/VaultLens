---
title: <% tp.file.title %>
type: inventory
kind: <%* const k = await tp.system.suggester(["item","ingest-candidate","question","task","watch","corpus","artifact"], ["item","ingest-candidate","question","task","watch","corpus","artifact"]); tR += k; %>
status: <%* const s = await tp.system.suggester(["proposed","active","blocked","ingested","superseded","archived"], ["proposed","active","blocked","ingested","superseded","archived"]); tR += s; %>
priority: <%* const p = await tp.system.suggester(["p0","p1","p2","p3","p4"], ["p0","p1","p2","p3","p4"]); tR += p; %>
created: <% tp.date.now("YYYY-MM-DD") %>
updated: <% tp.date.now("YYYY-MM-DD") %>
summary: 
tags: []
sources: []
---

# <% tp.file.title %>

## Why this record exists

<% tp.file.cursor(1) %>

## Next actions

- 

## Notes

