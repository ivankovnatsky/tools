# Show what would change (dry-run)
diff:
    tools diff

# Apply config to bring system to desired state
deploy:
    tools deploy

# Format and lint code
format:
    treefmt

# Lint without modifying
lint:
    ruff check .
    ruff format --check .

# Remove build artifacts
clean:
    find . -type d -name __pycache__ -exec rm -rf {} +
    find . -type d -name "*.egg-info" -exec rm -rf {} +
    rm -rf dist build .pytest_cache .ruff_cache .coverage htmlcov result

# Increment patch version
bump:
    #!/usr/bin/env bash
    current=$(grep 'version' pyproject.toml | head -1 | sed 's/.*"\(.*\)"/\1/')
    major=$(echo $current | cut -d. -f1)
    minor=$(echo $current | cut -d. -f2)
    patch=$(echo $current | cut -d. -f3)
    new_patch=$((patch + 1))
    new_version="${major}.${minor}.${new_patch}"
    if [ "$(uname)" = "Darwin" ]; then
      sed -i '' "s/version = \"${current}\"/version = \"${new_version}\"/" pyproject.toml
    else
      sed -i "s/version = \"${current}\"/version = \"${new_version}\"/" pyproject.toml
    fi
    echo "Bumped version: ${current} -> ${new_version}"

# Bump version and create GitHub release
release: bump
    #!/usr/bin/env bash
    version=$(grep 'version' pyproject.toml | head -1 | sed 's/.*"\(.*\)"/\1/')
    git add pyproject.toml
    git commit -m "Bump version to ${version}"
    git tag "v${version}"
    git push origin main --tags
    gh release create "v${version}" --generate-notes
