<%*
// Only run for files literally named TODO.md inside a project folder.
// Other files in projects/<slug>/ (project.md, CLAUDE.md, etc.) fall through.
if (tp.file.title !== "TODO") { return; }
const slug = tp.file.folder();
-%>
# <% slug %> TODO

- [ ] 
