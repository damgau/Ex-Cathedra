# Panasonic P2 Format — Practical Reference

Discovered through working with AJ-PX270EJ recordings. All scripts in `tools/` should
follow these conventions.

---

## 1. P2 Directory Structure

```
INPUT/{camera}/
  VIDEO/      ← video MXF files (one per clip)
  AUDIO/      ← audio MXF files (one per channel per clip)
  CLIP/       ← P2 metadata XML (one per clip, describes chain links & timecode)
  PROXY/      ← low-res proxy files
  ICON/       ← thumbnail BMPs
  VOICE/      ← voice memo files
```

---

## 2. MXF File Conventions

### Video MXF — `VIDEO/{clip_name}.MXF`

Contains H.264 video at stream index 0. **Audio channels are stored as `codec_type: data`
streams** (index 1–4) and are NOT decodable by ffmpeg from this file.

```
# ffprobe reveals no audio streams:
ffprobe VIDEO/0158Q5.MXF  →  stream 0: h264 (video)
                              stream 1-4: data (tagged data_type=audio — unusable)
```

**Never use `-map 0:a:N` on a video MXF** — it will fail with "Stream map matches no streams".

### Audio MXF — `AUDIO/{clip_name}{channel_idx:02d}.MXF`

Each audio channel is a separate file, channel index is **0-based**:

| Channel | File |
|---|---|
| CH1 | `AUDIO/0158Q500.MXF` |
| CH2 | `AUDIO/0158Q501.MXF` |
| CH3 | `AUDIO/0158Q502.MXF` |
| CH4 | `AUDIO/0158Q503.MXF` |

Inside each audio MXF:
- Stream 0: `codec_type: data` (a video reference — ignore)
- Stream 1: `codec_type: audio`, `pcm_s16le`, 48 000 Hz, mono → **this is the audio**

Always extract with `-map 0:a:0` (first actual audio stream).

---

## 3. ffmpeg Extraction Recipe

```bash
ffmpeg \
  -ss <start_seconds> \
  -i AUDIO/{clip_name}{channel_idx:02d}.MXF \
  -t <duration_seconds> \
  -map 0:a:0 \
  -ar 8000 -ac 1 \
  -f f32le pipe:1
```

- `-ss` before `-i` for fast seeking (input-side seek)
- `-ar 8000` downsample to 8 kHz for silence detection / cross-correlation (speed)
- `-f f32le pipe:1` streams raw float32 to stdout for numpy

Python pattern (from `tools/remove_silence.py`):
```python
channel_idx = channel - 1   # channel is 1-indexed in audio_config.json
audio_mxf = mxf.parent.parent / "AUDIO" / f"{mxf.stem}{channel_idx:02d}.MXF"
if not audio_mxf.exists():
    audio_mxf = mxf.parent.parent / "AUDIO" / f"{mxf.stem}{channel_idx:02d}.mxf"
```

---

## 4. Chained (Spanned) Clips

A long recording is split into multiple ~10-minute MXF files linked via `CLIP/*.XML`.

### CLIP XML structure

Namespace: `urn:schemas-Professional-Plug-in:P2:ClipMetadata:v3.1`

Key fields:
```xml
<Duration>14472</Duration>             <!-- clip duration in frames -->
<Relation>
  <OffsetInShot>28944</OffsetInShot>   <!-- frame offset from shot start -->
  <Connection>
    <Top>
      <ClipName>0158Q5</ClipName>      <!-- first clip in the recording -->
    </Top>
    <Previous>
      <ClipName>0159E2</ClipName>
    </Previous>
    <Next>
      <ClipName>0161VE</ClipName>      <!-- absent on the last clip -->
    </Next>
  </Connection>
</Relation>
<EssenceList>
  <Video>
    <StartTimecode>00:28:57:21</StartTimecode>   <!-- wall-clock timecode -->
  </Video>
</EssenceList>
```

### Walking the chain

```python
P2_NS = 'urn:schemas-Professional-Plug-in:P2:ClipMetadata:v3.1'

def walk_chain(clip_dir, start_name):
    """Returns ordered list of {name, offset, duration} dicts."""
    chain, name = [], start_name
    while name:
        root = ET.parse(clip_dir / f"{name}.XML").getroot()
        dur    = int(root.find(f'.//{{{P2_NS}}}Duration').text)
        next_  = root.find(f'.//{{{P2_NS}}}Next/{{{P2_NS}}}ClipName')
        offset = root.find(f'.//{{{P2_NS}}}OffsetInShot')
        chain.append({'name': name, 'duration': dur,
                      'offset': int(offset.text) if offset is not None else sum(c['duration'] for c in chain)})
        name = next_.text if next_ is not None else None
    return chain
```

---

## 5. FCP XML Pathurl Decoding

Premiere/FCP exports paths as percent-encoded URLs:
```
file://localhost/G%3a/MATELE/EX%20Cathedra/00_AI_project/INPUT/DIV%20CAM/VIDEO/0158Q5.mxf
```

Decode correctly (Python):
```python
from urllib.parse import unquote

raw = unquote(furl.replace("file://localhost", ""))
# Strip leading "/" before Windows drive letter: /G:/... → G:/...
if len(raw) > 2 and raw[0] == "/" and raw[2] == ":":
    raw = raw[1:]
path = Path(raw)
```

`%3a` = `:` (drive letter colon), `%20` = space, `%28` = `(`, `%29` = `)`.
Using `unquote` handles all of these in one call.

---

## 6. FCP XML In/Out for Spanned Clips

When Premiere exports a manually-synced sequence, it may write a **single `<clipitem>`**
whose `<in>/<out>` span the full recording chain — far beyond the referenced physical file.

```xml
<clipitem>
  <file><pathurl>…/VIDEO/01677J.mxf</pathurl></file>  <!-- last clip in chain -->
  <start>0</start>
  <end>88155</end>
  <in>5386</in>      <!-- offset from chain start, NOT from this file's start -->
  <out>93541</out>   <!-- 01677J.MXF is only 5422 frames — this exceeds it -->
</clipitem>
```

`<in>` and `<out>` are frame offsets measured from the **Top of the chain**, not from
the referenced physical file. To extract audio across the correct files:

1. Read `CLIP/{referenced_clip}.XML` → find `Top`
2. Walk the chain from `Top` (see §4)
3. Map `[in, out]` across physical clips:

```python
g_start, g_end = source_in, source_in + duration
for clip in chain:
    O, D = clip['offset'], clip['duration']
    c_start, c_end = max(g_start, O), min(g_end, O + D)
    if c_end <= c_start:
        continue
    local_in  = c_start - O   # frame offset within this physical file
    local_dur = c_end - c_start
    # … extract audio from this physical clip
```

Reference implementation: `tools/remove_silence.py:_resolve_p2_chain`

---

## 7. Key Constants

| Constant | Value |
|---|---|
| Frame rate | 25 fps NDF |
| FCP ticks per frame | 10 160 640 000 |
| Audio sample rate (native) | 48 000 Hz |
| Audio sample rate (analysis) | 8 000 Hz (downsampled) |
| P2 XML namespace | `urn:schemas-Professional-Plug-in:P2:ClipMetadata:v3.1` |
| Camera labels (FCP XML) | `"MAIN CAM"`, `"DIV CAM"` (percent-encoded as `%20` in pathurl) |
| Audio channel numbering | `audio_config.json` uses 1-indexed; file suffix uses 0-indexed (`channel - 1`) |
