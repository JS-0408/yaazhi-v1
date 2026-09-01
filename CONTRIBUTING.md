# Contributing to Yaazhi

Thank you for your interest in contributing! This document provides guidelines and instructions for contributing.

## Code of Conduct

- Be respectful and constructive in all interactions
- Provide clear, actionable feedback
- Credit others for their contributions
- Maintain inclusive and professional communication

## Getting Started

1. **Fork the repository** on GitHub
2. **Clone your fork** locally:
   ```bash
   git clone https://github.com/your-username/yaazhi.git
   cd yaazhi
   ```
3. **Create a virtual environment**:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```
4. **Install dependencies**:
   ```bash
   pip install -r requirements-dev.txt
   ```

## Development Workflow

### 1. Create a Feature Branch
```bash
git checkout -b feature/your-feature-name
```

### 2. Code Style

- **Python**: Follow [PEP 8](https://pep8.org/) and [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html)
- **Linting**: Use `ruff check .` and `ruff format .`
- **Type Checking**: Use `mypy --strict` for all new code
- **Testing**: Aim for >80% coverage

Run checks locally:
```bash
ruff check . --fix
ruff format .
mypy . --strict
pytest tests/ -v --cov=
```

### 3. Commit Messages

Follow conventional commits:
```
type(scope): subject

body

footer
```

**Types**: `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`

**Example**:
```
feat(memory): add episodic memory caching

Implement Redis-backed episodic memory to improve
retrieval latency for recent conversations by 40%.

Closes #42
```

### 4. Documentation

- Add docstrings to all public functions using Google style:
  ```python
  def fetch_and_summarize(url: str) -> str:
      """Fetch URL content and return a summary.
      
      Args:
          url: The URL to fetch and summarize.
          
      Returns:
          A brief summary of the fetched content.
          
      Raises:
          ValueError: If URL is invalid.
      """
  ```
- Update README.md if adding new features
- Document architectural decisions in `docs/decisions/`

## Testing

- Write tests for all new functionality
- Ensure existing tests pass: `pytest tests/`
- Test locally before submitting PR:
  ```bash
  pytest tests/ -v
  ```

## Pull Request Process

1. **Push to your fork**:
   ```bash
   git push origin feature/your-feature-name
   ```

2. **Create a Pull Request** on GitHub with:
   - Clear title and description
   - Link to related issues (e.g., "Closes #42")
   - Summary of changes
   - Any breaking changes noted

3. **Respond to feedback** constructively

4. **Ensure CI passes** (linting, tests, type checking)

5. Maintainers will review and merge when approved

## Architecture Decisions

New architectural decisions should be documented as ADRs (Architecture Decision Records) in `docs/decisions/`:

1. Copy `docs/decisions/TEMPLATE.md`
2. Create `docs/decisions/NNNN-short-title.md` (e.g., `0001-use-langraph.md`)
3. Include the ADR in your PR

## Areas for Contribution

- **Bug Fixes**: Review open issues labeled `bug`
- **Features**: Check `docs/roadmap/` for planned features
- **Tests**: Improve coverage in untested modules
- **Documentation**: Improve clarity, examples, or translations
- **Performance**: Profile and optimize hot paths
- **Voice**: Add new language support (Bhashini integration)

## Questions?

- Open an issue for questions
- Check existing issues for similar topics
- Ask in discussions or reach out

## Acknowledgments

All contributors will be credited in:
- [CONTRIBUTORS.md](CONTRIBUTORS.md)
- Release notes for related versions

Thank you for contributing to Yaazhi! 🚀
