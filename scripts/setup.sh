#!/bin/bash
# KnowledgeForge Setup Script
set -e

echo "=== KnowledgeForge Setup ==="

# Check Python version
python_version=$(python3 --version 2>&1 | cut -d' ' -f2)
echo "Python version: $python_version"

# Install in development mode
echo "Installing KnowledgeForge..."
pip install -e .

# Create config directory
config_dir="$HOME/.config/knowledgeforge"
mkdir -p "$config_dir"

# Create default config if it doesn't exist
if [ ! -f "$config_dir/config.yaml" ]; then
    echo "Creating default config at $config_dir/config.yaml"
    cp config.yaml "$config_dir/config.yaml"
    echo "Edit $config_dir/config.yaml to set your vault and project paths."
else
    echo "Config already exists at $config_dir/config.yaml"
fi

# Create data directory
data_dir="$HOME/.local/share/knowledgeforge"
mkdir -p "$data_dir"
echo "Data directory: $data_dir"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit $config_dir/config.yaml"
echo "  2. Set your Obsidian vault path"
echo "  3. Add your code project paths"
echo "  4. Run: knowledgeforge index all"
echo "  5. Run: knowledgeforge serve"
