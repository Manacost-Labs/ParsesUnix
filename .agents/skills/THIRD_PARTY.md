# Third-party skills

Eight engineering-practice skills in this directory are **vendored from
third-party repositories**, all MIT-licensed. They are kept as close to upstream
as possible so they can be re-synced; every modification is listed below.

The project's own skills (`web-scraper`, `scraper-regression`, `scraper-debugger`)
are original work and are not covered by this file.

| Skill | Upstream | Copyright |
|---|---|---|
| `karpathy-coder` | [alirezarezvani/claude-skills](https://github.com/alirezarezvani/claude-skills/tree/main/engineering/karpathy-coder/skills/karpathy-coder) | © 2025 Alireza Rezvani |
| `pr-review-expert` | [alirezarezvani/claude-skills](https://github.com/alirezarezvani/claude-skills/tree/main/engineering/skills/pr-review-expert) | © 2025 Alireza Rezvani |
| `test-driven-development` | [obra/superpowers](https://github.com/obra/superpowers/tree/main/skills/test-driven-development) | © 2025 Jesse Vincent |
| `systematic-debugging` | [obra/superpowers](https://github.com/obra/superpowers/tree/main/skills/systematic-debugging) | © 2025 Jesse Vincent |
| `verification-before-completion` | [obra/superpowers](https://github.com/obra/superpowers/tree/main/skills/verification-before-completion) | © 2025 Jesse Vincent |
| `python-lint` | [Paldom/python-skills](https://github.com/Paldom/python-skills/tree/main/skills/python-lint) | © 2026 Paldom |
| `python-typing` | [Paldom/python-skills](https://github.com/Paldom/python-skills/tree/main/skills/python-typing) | © 2026 Paldom |
| `python-ci` | [Paldom/python-skills](https://github.com/Paldom/python-skills/tree/main/skills/python-ci) | © 2026 Paldom |

Vendored on 2026-08-19.

## Modifications from upstream

* `systematic-debugging/SKILL.md` — the two cross-skill references were
  de-namespaced (`superpowers:test-driven-development` →
  `test-driven-development`, same for `verification-before-completion`) so they
  resolve to the skills as installed here.
* Upstream `evals/`, `CREATION-LOG.md`, and `test-pressure-*.md` files were not
  copied: they are the authors' own test harnesses, not part of the skill.

Everything else is byte-identical to upstream.

## Applying them to this project

These skills describe general practice; where they prescribe tooling, this
project's conventions win:

* **Tests** — `python -m unittest discover -s tests`, standard library only. Do
  not introduce pytest as a runtime or test dependency; browser-dependent tests
  must skip when Playwright is absent.
* **Lint / typing** — `ruff` and `mypy` belong in the `dev` extra and in CI only.
  The shipped package must keep importing on a bare Python 3.11+ with no
  third-party runtime dependencies.
* **CI** — this repo already has `.github/workflows/ci.yml` (unittest matrix
  3.11–3.13 + a browser job) and `staleness-watchdog.yml`. Extend those rather
  than generating a parallel workflow set.

## MIT License

The following notice covers all three upstream projects; it is reproduced once
because the license text is identical.

```
MIT License

Copyright (c) 2025 Jesse Vincent (obra/superpowers)
Copyright (c) 2025 Alireza Rezvani (alirezarezvani/claude-skills)
Copyright (c) 2026 Paldom (Paldom/python-skills)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
