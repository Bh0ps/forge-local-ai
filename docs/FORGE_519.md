# Forge 5.1.9 Windows preview

This preview improves small details that interrupt everyday work: supporting text
in Performance, owner changes in GitHub browsing, and keyboard focus when a
reasoning disclosure becomes part of a saved reply. It also expands the reviewed
starter library with Windows development workflows.

## Performance and connections

The introduction beneath **Measured adaptive profiles** uses the panel's existing
supporting-text style with readable spacing and a consistent inset. The font
family, measurement controls and surrounding Forge colors remain unchanged.

GitHub results and their next-page controls belong to the owner used for that
query. Editing the owner clears that result scope; the next explicit Browse starts
at page one. A delayed response or error from the previous owner cannot fill the
new owner's list. Changing accounts also clears the old scope. Browsing remains
an explicit action.

## Reasoning and computer-tool reliability

Keyboard focus on a reasoning summary now carries through the delayed load of
its saved assistant turn, including after closing the disclosure. The disclosure
retains the user's open or closed choice. Focus in the reading region still
requires an open disclosure. Another interaction, navigation or window change
cancels the return, preserving the user's current focus.

Windows inspection now checks that the same visible window identity and bounds
survive accessibility traversal and screenshot capture. If the target changes or
the action is cancelled, Forge refuses the snapshot before a tool action and asks
the agent to inspect again. This closes a race that could otherwise accept a
coordinate click based on an inconsistent capture.

The packaged standalone-preview smoke also checks each actual tool result and
records a bounded failure category. A safe refusal or uncertain effect cannot be
hidden behind a later text assertion. The check never retries dispatched actions
or changes the existing time limits and target guards.

Existing Windows computer tools use the host desktop and its cursor. They require
the existing permission controls and fresh snapshots. This patch does not claim
isolated desktop control or two independent host input devices.

## More specific Windows development skills

The starter library adds three reviewed workflows:

- **Windows .NET**: discover and work with existing C# solutions and projects,
  including WPF and Windows Forms; build and verify with the actual available SDK.
- **Windows native C++**: follow an existing CMake or MSBuild project, use the
  available compiler and SDK, and verify the resulting native application.
- **Desktop executable**: follow the project's packaging pipeline, produce an
  identifiable executable, and check it with disposable data before delivery.

The skills distinguish tool availability from successful execution. They require
agents to inspect the project, respect its chosen framework and dependency locks,
record build and check results, and identify the actual output. Missing SDKs and
failed commands require accurate recovery rather than invented success. Existing
customized, disabled or removed starter skills retain their user choices.

Skill selection also distinguishes C# from C++, rather than collapsing both names
to a generic C token. Detailed examples remain resources loaded when relevant,
within the existing guidance limits.

## Updating and privacy

There is no new database migration. The patch retains [5.1.8 reading controls](FORGE_518.md),
automatic helper cleanup, saved provider authority and publisher-verified GitHub
downloads. Windows executables do not yet have an Authenticode certificate.

Release notes record the completed full suites and packaged Windows checks.
Visual checks and regression fixtures use synthetic data. Private prompts,
credentials, provider state, backups and raw diagnostics stay outside release
assets.
