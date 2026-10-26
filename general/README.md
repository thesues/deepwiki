# General assistant

Run with `python -m general.main`. This service uses Hermes with its own Web UI,
configuration, sessions and domain skills; it has no research-service navigation.

## Handwritten new-session introduction

`skills-introduction.html` is the **manually authored** capability introduction
shown on the general assistant's fresh conversation screen. It is not generated
from skill metadata, a filesystem scan or model inference. When general skills
are added, removed or changed, manually review and update this copy in the same
change. Describe user-facing capabilities rather than exposing a list of tool names.

Both the standalone general page and the unified single-execution mode use
this same handwritten copy; research modes hide it. Restart the service after
editing it to refresh the versioned HTML snapshot.
