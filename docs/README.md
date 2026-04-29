# Documentation Build Instructions

## Quick Start

Build the complete documentation site:

```bash
# Install documentation dependencies
pip install -r docs/requirements.txt

# Build documentation
python3 build_docs.py

# Serve locally
cd docs/_build/html && python -m http.server 8000
# Visit: http://localhost:8000
```

## Manual Build Process

If you prefer to build manually:

```bash
# 1. Install dependencies
pip install sphinx sphinx-rtd-theme myst-parser

# 2. Generate API documentation
sphinx-apidoc -o docs/api . --separate --force

# 3. Build HTML documentation
cd docs && make html

# 4. Serve documentation
cd _build/html && python -m http.server 8000
```

## Documentation Structure

```
docs/
├── index.md                    # Main documentation index
├── quickstart.md              # 5-minute getting started guide
├── api_reference.md           # Complete API documentation
├── architecture.md            # System architecture with diagrams
├── advanced_usage.md          # Enterprise examples and CI/CD
├── troubleshooting.md         # Comprehensive troubleshooting
├── tutorials/
│   └── adding_services.md     # Tutorial for adding new AWS services
├── examples/
│   └── index.md              # Usage examples and integrations
├── conf.py                   # Sphinx configuration
├── requirements.txt          # Documentation dependencies
└── Makefile                  # Build automation
```

## Generated Documentation

The build process creates:

- **Interactive HTML Documentation**: Complete searchable documentation site
- **API Reference**: Auto-generated from code docstrings
- **Architecture Diagrams**: Visual system and service integration diagrams
- **Usage Examples**: Copy-paste ready code examples
- **Troubleshooting Database**: Searchable solutions for common issues

## Features

- **Search Functionality**: Full-text search across all documentation
- **Responsive Design**: Works on desktop and mobile devices
- **Code Highlighting**: Syntax highlighting for all code examples
- **Cross-References**: Automatic linking between documentation sections
- **Version Information**: Tracks documentation version with code version

## Deployment

For production deployment:

```bash
# Build documentation
python3 build_docs.py

# Deploy to web server
rsync -av docs/_build/html/ user@server:/var/www/docs/

# Or deploy to GitHub Pages
# Copy docs/_build/html/* to gh-pages branch
```

## Maintenance

Keep documentation up to date:

1. **API Changes**: Docstrings are automatically included
2. **New Features**: Update relevant documentation files
3. **Architecture Changes**: Regenerate diagrams as needed
4. **Examples**: Keep code examples current with latest version

The documentation system is designed to grow with the project and provide comprehensive support for users, contributors, and enterprise deployments.
