# Contributing to Aurora

Thank you for your interest in contributing to Aurora! This document provides guidelines and instructions for contributing to the project.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [How to Contribute](#how-to-contribute)
- [Branch Naming Convention](#branch-naming-convention)
- [Pull Request Process](#pull-request-process)
- [Code Style Guidelines](#code-style-guidelines)
- [Testing](#testing)
- [Reporting Bugs](#reporting-bugs)
- [Suggesting Enhancements](#suggesting-enhancements)

## Code of Conduct

This project adheres to a Code of Conduct that all contributors are expected to follow. Please read [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) before contributing.

## Getting Started

Aurora is a natural-language interface for managing cloud infrastructure. Before contributing, familiarize yourself with:

- The [README.md](README.md) for project overview
- The [AGENTS.md](AGENTS.md) for agent-specific guidelines
- The [documentation site](https://arvo-ai.github.io/aurora/docs) for additional documentation

## Development Setup

### Prerequisites

- Docker and Docker Compose >= 28.x
- Node.js >= 18.x (for frontend development)
- Python >= 3.11 (for backend development)
- Make (for using Makefile commands)

### Initial Setup

1. **Fork the repository**

   - Go to https://github.com/Arvo-AI/aurora
   - Click the "Fork" button in the top right
   - This creates a copy under your GitHub account

2. **Clone your fork**

   ```bash
   git clone https://github.com/YOUR-USERNAME/aurora.git
   cd aurora
   ```

3. **Add upstream remote**

   ```bash
   git remote add upstream https://github.com/Arvo-AI/aurora.git
   ```

4. **Initialize configuration**

   ```bash
   make init
   ```

   This generates secure secrets automatically.

5. **Edit .env and add your LLM API key**

   Get one from https://openrouter.ai/keys or https://console.anthropic.com/settings/keys

   ```bash
   nano .env  # Add OPENROUTER_API_KEY=sk-or-v1-... or ANTHROPIC_API_KEY=sk-ant-...
   ```

6. **Start Aurora in development mode**

   ```bash
   make dev
   ```

   Development mode includes hot reloading for both frontend and backend changes.

7. **Get Vault root token and add to .env**

   Check the vault-init container logs for the root token:

   ```bash
   docker logs vault-init 2>&1 | grep "Root Token:"
   ```

   Copy the root token value and add it to your .env file:

   ```bash
   nano .env  # Add VAULT_TOKEN=hvs.xxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```

8. **Restart Aurora to load the Vault token**

   ```bash
   make down
   make dev
   ```

9. **Access the application**

   - Frontend: http://localhost:3000
   - REST API: http://localhost:5080
   - Chatbot WebSocket: ws://localhost:5006

10. **Stop the environment**

    ```bash
    make down
    ```

### Keeping Your Fork Updated

Before starting new work, sync your fork with the upstream repository:

```bash
git fetch upstream
git checkout main
git merge upstream/main
git push origin main
```

## How to Contribute

### Types of Contributions

We welcome various types of contributions:

- Bug fixes
- New features
- Documentation improvements
- Performance improvements
- Test coverage improvements
- Code refactoring

### Before You Start

1. Check the issue tracker to see if your issue/feature is already being discussed
2. For major changes, open an issue first to discuss your proposed changes
3. Make sure your fork is up to date with the upstream repository
4. Create a feature branch from `main` in your fork

## Branch Naming Convention

Use the following prefixes for your branch names:

- `feature/` - New features or enhancements
  - Example: `feature/add-aws-cost-optimization`
- `bugfix/` - Bug fixes
  - Example: `bugfix/fix-gcp-auth-error`
- `hotfix/` - Urgent fixes for production issues
  - Example: `hotfix/security-patch-vault`
- `docs/` - Documentation updates
  - Example: `docs/update-kubernetes-guide`
- `refactor/` - Code refactoring without behavior changes
  - Example: `refactor/cleanup-db-utils`
- `test/` - Adding or updating tests
  - Example: `test/add-connector-tests`
- `chore/` - Maintenance tasks, dependency updates
  - Example: `chore/update-dependencies`

Branch names should be lowercase and use hyphens to separate words.

## Pull Request Process

1. **Update your fork**

   ```bash
   git fetch upstream
   git checkout main
   git merge upstream/main
   ```

2. **Create a feature branch**

   ```bash
   git checkout -b feature/your-feature-name
   ```

3. **Make your changes**

   - Write clear, concise commit messages
   - Follow the code style guidelines below
   - Add tests if applicable
   - Update documentation as needed

4. **Test your changes**

   ```bash
   # Test development build
   make dev
   ```

5. **Commit your changes**

   ```bash
   git add .
   git commit -m "Add feature: your feature description"
   ```

6. **Push to your fork**

   ```bash
   git push origin feature/your-feature-name
   ```

7. **Open a Pull Request**

   - Go to the main Aurora repository on GitHub
   - You should see a prompt to create a PR from your recently pushed branch
   - Click "Compare & pull request"
   - Ensure the PR is targeting the `main` branch of the upstream repository
   - Fill out the PR template with:
     - Clear description of changes
     - Related issue numbers (if applicable)
     - Screenshots (for UI changes)
     - Testing steps

8. **Address review feedback**

   - Respond to reviewer comments
   - Make requested changes in your local branch
   - Push additional commits to your fork (they'll appear in the PR automatically)

   ```bash
   git add .
   git commit -m "Address review feedback"
   git push origin feature/your-feature-name
   ```

9. **PR Approval and Merge**

   - PRs require approval from maintainers
   - Once approved, a maintainer will squash-merge your PR to `main`
   - You can then delete your feature branch

10. **Sync your fork after merge**

    ```bash
    git checkout main
    git fetch upstream
    git merge upstream/main
    git push origin main
    git branch -d feature/your-feature-name  # Delete local branch
    git push origin --delete feature/your-feature-name  # Delete remote branch
    ```

### General Guidelines

- Use kebab-case for URLs and file names
- Keep functions small and focused
- Avoid deep nesting
- Write self-documenting code
- Add comments for complex logic only
- No commented-out code in PRs
- No emojis in code or logs

### Linting

Run linters before submitting:

```bash
# Frontend linting
cd client && npm run lint

```

## Reporting Bugs

When reporting bugs, please include:

1. **Description**: Clear description of the bug
2. **Steps to Reproduce**: Detailed steps to reproduce the issue
3. **Expected Behavior**: What you expected to happen
4. **Actual Behavior**: What actually happened
5. **Environment**:
   - OS and version
   - Docker version
   - Browser (for frontend issues)
   - Relevant configuration from `.env`
6. **Logs**: Relevant error messages or logs
7. **Screenshots**: If applicable

Use the issue tracker with the bug label.

## Suggesting Enhancements

We welcome feature suggestions! Please include:

1. **Use Case**: Describe the problem you're trying to solve
2. **Proposed Solution**: Your suggested approach
3. **Alternatives**: Other solutions you've considered
4. **Additional Context**: Any other relevant information

Open an issue with the enhancement label.

## Environment Variables

When adding new environment variables:

1. Add them to `.env.example` with documentation
2. Update relevant docker-compose files
3. The CI pipeline will automatically validate env var consistency

## Docker Compose

- Always update both `docker-compose.yaml` and `prod.docker-compose.yml`
- Keep environment variables in sync between Docker Compose files
- Test production build: `make prod-build && make prod`

## Questions?

If you have questions about contributing:

- Open a discussion on GitHub
- Contact the maintainers: info@arvoai.ca
- Check existing issues and documentation

## License

By contributing to Aurora, you agree that your contributions will be licensed under the Apache License 2.0. See [LICENSE](LICENSE) for details.

Thank you for contributing to Aurora!
