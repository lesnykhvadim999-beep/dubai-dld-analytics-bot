#!/bin/bash
# Install pre-commit hook in current repo
HOOK=".git/hooks/pre-commit"
cat > "$HOOK" <<'EOF'
#!/bin/bash
FILES=$(git diff --cached --name-only --diff-filter=ACM | grep '\.py$')
if [ -z "$FILES" ]; then exit 0; fi
echo "$FILES" | python C:/Projects/shared/precommit_secrets_check.py
exit $?
EOF
chmod +x "$HOOK"
echo "pre-commit hook installed"
