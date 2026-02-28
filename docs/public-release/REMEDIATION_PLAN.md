# Remediation Plan — Public Release

Ordered by priority. Effort: S = small (< 30 min), M = medium (30–60 min), L = large (> 1 hr).

---

## 1. Add LICENSE (S)

**Action:** Add `LICENSE` at repo root.

**Recommendation:** MIT License (permissive, common for infra/templates).

**Steps:**
1. Create `LICENSE` with MIT text
2. Add copyright holder and year
3. Commit: `Add MIT LICENSE`

---

## 2. Add SECURITY.md (S)

**Action:** Create `SECURITY.md` with:
- Supported versions
- How to report vulnerabilities
- Security considerations (WEBUI_AUTH, network binding, token handling)

**Commit:** `Add SECURITY.md`

---

## 3. Add CONTRIBUTING.md (S)

**Action:** Create `CONTRIBUTING.md` with:
- How to contribute (PRs, issues)
- Security notes (no secrets in PRs, use .env.example)
- Code/style expectations (minimal — scripts + compose)

**Commit:** `Add CONTRIBUTING.md`

---

## 4. Document WEBUI_AUTH and network exposure (S)

**Action:** Update README and/or SECURITY.md to state:
- `WEBUI_AUTH=False` disables Open WebUI login — suitable for local/single-user only
- Services bind to 0.0.0.0 — accessible on LAN; use firewall if needed
- Recommend enabling auth if exposing to untrusted networks

**Commit:** `Document security defaults for WEBUI_AUTH and network binding`

---

## 5. Add CI workflow (M)

**Action:** Create `.github/workflows/ci.yml`:
- Secret scanning (e.g. `trufflehog` or `gitleaks` action)
- Basic validation: `docker compose config` (syntax check)
- Run on: push to main, pull_request

**Commit:** `Add CI workflow for secret scanning and compose validation`

---

## 6. Harden .gitignore (S)

**Action:** Add explicit entries for clarity:
```
openclaw/.env
*.pem
*.key
```

(Redundant for `.env` pattern but improves clarity.)

**Commit:** `Harden .gitignore with explicit secret patterns`

---

## 7. Optional: Pre-commit hook (M)

**Action:** Add `.pre-commit-config.yaml` with `detect-secrets` or `gitleaks`.

**Commit:** `Add pre-commit hook for secret detection`

---

## Summary

| # | Item | Effort |
|---|------|--------|
| 1 | LICENSE | S |
| 2 | SECURITY.md | S |
| 3 | CONTRIBUTING.md | S |
| 4 | WEBUI_AUTH docs | S |
| 5 | CI workflow | M |
| 6 | .gitignore | S |

**Total estimated:** ~1–2 hours for items 1–6.
