# Camera switches are encoded as a disabled MAIN video clipitem

A **camera switch** reveals the DIV angle by splitting and disabling
(`<enabled>FALSE</enabled>`) *only* MAIN's video clipitem across the window — never
DIV, never audio, never all tracks. This keeps angle-switching (`switch_angles`) and
dead-air **ripple-delete** (`delete_enable_clip`) separable: the final stage removes
only spans disabled on *every* track, so a span with one video track muted is preserved
because DIV still covers it. Any module that applies a **mute** — including the cut
engine we are extracting from `remove_silence` — must preserve this: disabling a span
across all tracks would silently turn a camera switch into dead air.
