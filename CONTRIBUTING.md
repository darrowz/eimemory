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

# Install the project in development mode
pip install -e .

# Install verification tooling separately
python -m pip install pytest

# Run the behavior suites affected by your change
python -m pytest tests/test_runtime.py -q
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
   python -m pytest <affected-test-files> -q
   python -m compileall -q eimemory
   git diff --check
   ```

4. **Commit with Clear Messages**
   ```bash
   git commit -m "Add feature: clear description of changes"
   ```

5. **Push and Create Pull Request**
   - Provide a clear PR description
   - Reference related issues
   - Include testing notes

Release validation is a separate decision; broaden testing when a change or
failure justifies it. Report pre-existing failures honestly. Never treat tests,
service health or smoke as production acceptance evidence.

## Documentation Contributions

- Keep the homepage, FAQ, package/integration descriptions and
  [public positioning](docs/github-recommendation.md) consistent
- Help improve the [documentation index](docs/README.md), architecture and deployment guides
- Separate released behavior, unreleased repairs and dated production evidence
- Retain original measurements in historical audits; link fresh receipt-based status
- Keep gate standards and missing-evidence limits explicit
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
