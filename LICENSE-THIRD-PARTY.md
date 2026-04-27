# Third-Party Licenses

This file lists third-party assets vendored into the Aurora repository, the
component that uses them, and their upstream licenses.

## Gitleaks rule corpus

- Path: `server/utils/security/rules/gitleaks-v8.28.0.toml`
- Used by: L5 output redaction (`server/utils/security/output_redaction.py`),
  via the build-time pattern generator `scripts/gen_secret_patterns.py`.
- Upstream: https://github.com/gitleaks/gitleaks
- Version: v8.28.0 (pinned, vendored verbatim; regenerate by refetching the
  file at `config/gitleaks.toml` from the pinned tag).
- License: MIT.

```
MIT License

Copyright (c) 2019 Zachary Rice

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```
