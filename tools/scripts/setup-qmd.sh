#!/bin/bash
# QMD Setup Script - Initialize local search for the wiki
# Run this once to set up qmd search engine

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "Setting up QMD search engine for wiki..."
echo "Wiki directory: $REPO_ROOT"

# Check if qmd is installed (any package manager). A global install changes the
# host, so ask first and never install from a non-interactive run.
if ! command -v qmd &> /dev/null; then
    if command -v bun &> /dev/null; then
        INSTALL=(bun install -g @tobilu/qmd)
    elif command -v npm &> /dev/null; then
        INSTALL=(npm install -g @tobilu/qmd)
    else
        echo "Error: qmd is not installed, and neither bun nor npm was found." >&2
        exit 1
    fi
    echo "qmd is not installed. Install command: ${INSTALL[*]}"
    if [ ! -t 0 ]; then
        echo "Run it yourself, then re-run this script." >&2
        exit 1
    fi
    read -r -p "Run it now? [y/N] " reply
    case "$reply" in
        [yY]*) "${INSTALL[@]}" ;;
        *) echo "Not installing. Install qmd, then re-run this script." >&2; exit 1 ;;
    esac
fi

echo "Using qmd at: $(command -v qmd)"

# Add wiki collection
echo ""
echo "Adding wiki collection..."
cd "$REPO_ROOT"
# qmd's own output stays visible, so a real failure is not mistaken for "exists".
qmd collection add wiki/ --name wiki || echo "Note: the wiki collection was not added (see above); it may already exist"

# Add raw sources collection
echo "Adding raw sources collection..."
qmd collection add raw/ --name raw || echo "Note: the raw collection was not added (see above); it may already exist"

# raw/review-inbox is a human consent queue. Keep it out of lexical, vector,
# and hybrid search so an agent cannot discover its contents before approval.
echo "Applying review-inbox consent boundary..."
python3 "$SCRIPT_DIR/configure-qmd.py"

# Build the BM25 index
echo ""
echo "Building search index..."
qmd update

# Generate embeddings for semantic search
echo ""
echo "Generating embeddings for semantic/vector search..."
echo "Note: First run downloads ~1.3GB model to ~/.cache/qmd/models/"
echo "This may take a few minutes."
echo ""
qmd embed

echo ""
echo "QMD setup complete!"
echo ""
echo "Usage:"
echo "  qmd search \"query\"           # BM25 keyword search (fast, no model)"
echo "  qmd vsearch \"query\"          # Vector semantic search"
echo "  qmd query \"query\"            # Hybrid search (best quality)"
echo "  qmd query \"query\" --format json   # JSON output for LLM context"
echo ""
echo "Maintenance:"
echo "  qmd update                   # Re-index after adding content"
echo "  qmd embed                    # Refresh embeddings"
echo "  qmd cleanup                  # Remove inactive documents and orphan chunks"
echo "  qmd status                   # Check index health"
echo ""
echo "For MCP integration, add to your AI config:"
echo '  { "command": "qmd", "args": ["mcp"] }'
