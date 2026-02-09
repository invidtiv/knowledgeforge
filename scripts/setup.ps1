# KnowledgeForge Setup Script (Windows)
Write-Host "=== KnowledgeForge Setup ===" -ForegroundColor Green

# Check Python
$pythonVersion = python --version 2>&1
Write-Host "Python version: $pythonVersion"

# Install
Write-Host "Installing KnowledgeForge..."
pip install -e .

# Config directory
$configDir = "$env:USERPROFILE\.config\knowledgeforge"
if (-not (Test-Path $configDir)) {
    New-Item -ItemType Directory -Path $configDir -Force | Out-Null
}

# Copy default config
$configPath = "$configDir\config.yaml"
if (-not (Test-Path $configPath)) {
    Copy-Item "config.yaml" $configPath
    Write-Host "Created default config at $configPath"
    Write-Host "Edit it to set your vault and project paths."
} else {
    Write-Host "Config already exists at $configPath"
}

# Data directory
$dataDir = "$env:USERPROFILE\.local\share\knowledgeforge"
if (-not (Test-Path $dataDir)) {
    New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
}

Write-Host ""
Write-Host "=== Setup Complete ===" -ForegroundColor Green
Write-Host "Next steps:"
Write-Host "  1. Edit $configPath"
Write-Host "  2. Set your Obsidian vault path"
Write-Host "  3. Run: knowledgeforge index all"
Write-Host "  4. Run: knowledgeforge serve"
