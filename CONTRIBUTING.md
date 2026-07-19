# Contributing to eimemory

Thank you for your interest in contributing to eimemory! This document provides guidelines and instructions for contributing.

## Code of Conduct

Be respectful and constructive in all interactions. We're building this project together.

## Getting Started

### Prerequisites
- Python 3.11+
- Git

### Local Setup

```bash
# Clone the repository
git clone https://github.com/darrowz/eimemory.git
cd eimemory

# Create a virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install in development mode with dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/
```

## Contribution Types

### Bug Reports
- Use GitHub Issues to report bugs
- Include reproducible steps, expected vs actual behavior
- Provide Python version and OS information

### Feature Requests
- Open a GitHub Issue describing the feature
- Explain the use case and why it matters
- Discuss implementation approach if possible

### Code Contributions

1. **Fork and Branch**
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **Make Changes**
   - Follow existing code style
   - Add tests for new functionality
   - Update documentation

3. **Test Your Changes**
   ```bash
   pytest tests/
   ```

4. **Commit with Clear Messages**
   ```bash
   git commit -m "Add feature: clear description of changes"
   ```

5. **Push and Create Pull Request**
   - Provide a clear PR description
   - Reference related issues
   - Include testing notes

## Documentation Contributions

- Help improve README, architecture docs, and deployment guides
- Fix typos and clarify examples
- Add new use case documentation
- Share real-world deployment experiences

## Areas We're Looking For

- Memory system enhancements
- Performance optimizations
- New recall strategies
- Better evaluation metrics
- Integration examples
- Documentation improvements
- Test coverage expansion

## Review Process

All contributions go through:
1. Automated checks (tests, linting)
2. Code review for clarity and correctness
3. Feedback and iteration
4. Merge and release planning

## Questions?

- Open an Issue for discussion
- Check existing documentation in `docs/`
- Review architecture docs for system design context

## License

By contributing, you agree your work will be licensed under the same terms as eimemory.

Thank you for making eimemory better! 🙏
