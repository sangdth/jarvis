from __future__ import annotations

import enum

LAUNCHD_LABEL = "com.sangdth.jarvis"


class GitState(enum.Enum):
    """Relationship between the running checkout's HEAD and `origin/master`."""

    UP_TO_DATE = "up_to_date"
    FAST_FORWARD = "fast_forward"
    LOCAL_AHEAD = "local_ahead"
    DIVERGED = "diverged"


def classify_git_state(
    local_sha: str | None, origin_sha: str | None, merge_base: str | None
) -> GitState:
    """
    Pure git-topology classifier.

    `merge_base` is the output of `git merge-base HEAD origin/master` (or `None` when the two
    histories are unrelated / cannot be compared). Callers must pass full commit SHAs.
    """
    if origin_sha is None:
        # No remote-tracking branch to follow: there is nothing to pull.
        return GitState.UP_TO_DATE
    if local_sha is None:
        # Unborn local branch with a known remote: adopt `origin/master` wholesale.
        return GitState.FAST_FORWARD
    if local_sha == origin_sha:
        return GitState.UP_TO_DATE
    if merge_base is None:
        return GitState.DIVERGED
    if merge_base == local_sha:
        return GitState.FAST_FORWARD
    if merge_base == origin_sha:
        return GitState.LOCAL_AHEAD
    return GitState.DIVERGED
