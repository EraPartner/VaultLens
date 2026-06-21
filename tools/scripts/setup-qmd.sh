#!/bin/bash
# QMD Setup Script - Initialize local search for the wiki
# Run this once to set up qmd search engine

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "Setting up QMD search engine for wiki..."
echo "Wiki directory: $REPO_ROOT"

# Check if qmd is installed (any package manager)
if ! command -v qmd &> /dev/null; then
    echo "qmd not found. Installing..."
    if command -v bun &> /dev/null; then
        bun install -g @tobilu/qmd
    elif command -v npm &> /dev/null; then
        npm install -g @tobilu/qmd
    else
        echo "Error: Neither bun nor npm found. Install one first."
        exit 1
    fi
fi

echo "Using qmd at: $(command -v qmd)"

# Add wiki collection
echo ""
echo "Adding wiki collection..."
cd "$REPO_ROOT"
qmd collection add wiki/ --name wiki 2>/dev/null || echo "Wiki collection may already exist"

# Add raw sources collection
echo "Adding raw sources collection..."
qmd collection add raw/ --name raw 2>/dev/null || echo "Raw collection may already exist"

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
echo "  qmd status                   # Check index health"
echo ""
echo "For MCP integration, add to your AI config:"
echo '  { "command": "qmd", "args": ["mcp"] }'
