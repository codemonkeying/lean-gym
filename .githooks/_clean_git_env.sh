# Sourced by the hooks. git sets GIT_DIR (and friends) in a hook's environment when the
# push/commit runs from a linked worktree. Every child - the test suite, scenario setup
# doing `git init`/`git commit` in temp dirs, a model's own git calls - would inherit it and
# act on THIS repo instead of its temp dir. That once committed a scenario fixture onto a
# pushed branch and flipped core.bare (2026-10-31). Clear the git variables
# AFTER the hook has cd'd to the top level; plain discovery from cwd still finds the repo.
# Unset every GIT_* except the exec path and auth helpers (config vars too, so a
# `git -c ... push` can't change how test fixtures behave).
for _v in $(env | sed -n 's/^\(GIT_[A-Za-z0-9_]*\)=.*/\1/p'); do
	case "$_v" in
		GIT_EXEC_PATH|GIT_SSH|GIT_SSH_COMMAND|GIT_ASKPASS) ;;
		*) unset "$_v" ;;
	esac
done
unset _v
