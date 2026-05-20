# Contribution Guide

### Creating A Pull Request
- **Target branch:** double-check the PR is opened against the correct branch before submitting.
- **Naming convention:** name of the branch should be in kebab case, for example `feat/some-feature` or `fix/some-bug`.
- **Tag relevant ticket/issue:** describe the purpose of the PR with the relevant ticket or issue link.
- **Include a sensible description:** descriptions help reviewers understand the purpose and context of the proposed changes.
- **Properly comment non-obvious code** to enhance maintainability.
- **Linters:** make sure every linter and check passes before making a commit.
- **Tests:** include tests for newly added or updated code.

For a clean workflow, run checks in the following order before opening a PR:

- `make format`
- `make code-checks`
- `make security`

**Run only if you've modified an AbciApp definition:**
- `make abci-docstrings`

**Only run the following if you have modified a file in `packages/`:**
- `make generators`
- `make common-checks-1`

**Run after making a commit:**
- `make common-checks-2`

### Agent development

You can find general recommendations in the **Considerations to Develop FSM Apps** section in our documentation [here](https://stack.olas.network/open-autonomy/key_concepts/fsm_app_introduction/).
