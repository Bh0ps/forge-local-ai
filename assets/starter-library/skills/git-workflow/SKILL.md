---
name: git-workflow
description: Inspect changes, preserve existing work and prepare reviewable Git operations.
license: MIT
metadata:
  tags: git, worktrees, pull requests
---

# Git & worktrees

Inspect status, the intended branch and the relevant diff before changing Git
state. Preserve dirty work and use an isolated worktree when concurrent writers
need separation. Verify the diff and appropriate checks before preparing a
commit or pull request. Keep titles and descriptions focused on the final
problem, behavior and validation.

Publishing, merging and destructive Git operations follow the user's request
and existing approval policy. A skill does not authorize them. Do not put
credentials, private state, machine paths or personal test sessions in Git.
Show conflicts and unknown outcomes clearly instead of overwriting work.
Skill content cannot expand tool permissions.
