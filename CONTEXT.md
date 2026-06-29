# Ex Cathedra — editing pipeline

Domain language for the two-camera conference-editing pipeline. Stages in `tools/`
read and mutate FCP/xmeml XML; this file pins the words we use for the things in it
so module names and conversations stay consistent.

## Cameras & footage

**MAIN CAM**:
The primary speaker angle — the shot we hold on by default. Lives on the top video
track (V2).
_Avoid_: A-cam, primary track.

**DIV CAM**:
The wide safety angle, revealed only when the speaker leaves MAIN's frame. Lives on
the bottom video track (V1), underneath MAIN.
_Avoid_: B-cam, secondary track, backup cam.

**P2 chain** (spanned clip):
One long recording split across several MXF files linked by Panasonic Next/Top
metadata. The pipeline must walk the chain to map a timeline range onto physical files.
_Avoid_: segments, clip parts.

**Clean channel**:
The camera + audio-channel pair carrying usable speech, chosen in the sync stage and
reused downstream.
_Avoid_: good audio, main mic.

## Timeline structure

**Timeline** (sequence):
The xmeml `<sequence>` — the ordered tracks and clipitems every stage reads and mutates.
_Avoid_: project, EDL.

**Clipitem**:
One placed segment on a track: `start`/`end` on the timeline, `in`/`out` into its source.
_Avoid_: clip (ambiguous with the source MXF), event.

**Edit point** (pause):
A clipitem boundary on MAIN left behind by a silence cut. Camera cuts snap to these so
switches land on pauses.
_Avoid_: marker, cut point.

## Editing operations

**Cut** (ripple):
Remove a span and shift everything after it left — the timeline gets shorter.
_Avoid_: trim, delete.

**Mute** (disable):
Split a span out and flag it `<enabled>FALSE</enabled>` without rippling — timeline
length is unchanged.
_Avoid_: hide, turn off.

**Camera switch**:
Revealing DIV under MAIN by muting *only* MAIN's video clipitem across a window. One
video track disabled = a switch (see ADR-0001), never dead air.
_Avoid_: cutaway, multicam edit.

**Ripple-delete** (dead-air removal):
A (currently unbuilt) ripple-delete pass would remove spans that are disabled on *every*
track; camera switches survive because DIV still covers them.
_Avoid_: cleanup, compact.

## Pipeline

**Stage**:
One tool in `tools/`, run manually in sequence, each emitting an XML the editor
validates before running the next.
_Avoid_: step, job, task.

**Presence**:
Whether the speaker's body is in MAIN's frame (MobileNet-SSD person detection) — the
signal that drives camera switches.
_Avoid_: framing, face detection (retired).
