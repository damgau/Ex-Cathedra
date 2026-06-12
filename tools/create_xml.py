#!/usr/bin/env python3
"""
Stage 1 — create_xml.py
Reads P2 chain metadata from INPUT/, generates a base FCP XML with both
cameras placed end-to-end on V1 (DIV CAM) and V2 (MAIN CAM), unsynchronised.
Output: OUTPUT/01_create.xml
"""

import sys
import uuid
import argparse
from pathlib import Path
from urllib.parse import quote
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()

BASE_DIR       = Path(__file__).parent.parent
MAIN_CLIP_DIR  = BASE_DIR / "INPUT" / "MAIN CAM" / "CLIP"
MAIN_VIDEO_DIR = BASE_DIR / "INPUT" / "MAIN CAM" / "VIDEO"
DIV_CLIP_DIR   = BASE_DIR / "INPUT" / "DIV CAM" / "CLIP"
DIV_VIDEO_DIR  = BASE_DIR / "INPUT" / "DIV CAM" / "VIDEO"
OUTPUT_DIR     = BASE_DIR / "OUTPUT"

P2_NS            = "urn:schemas-Professional-Plug-in:P2:ClipMetadata:v3.1"
TICKS_PER_FRAME  = 10_160_640_000   # Premiere internal ticks at 25 fps NDF


# ---------------------------------------------------------------------------
# P2 metadata helpers
# ---------------------------------------------------------------------------

def parse_p2_clip(xml_path: Path) -> dict:
    tree = ET.parse(xml_path)
    root = tree.getroot()
    ns   = {"p2": P2_NS}
    name     = root.find(".//p2:ClipName", ns).text
    duration = int(root.find(".//p2:Duration", ns).text)
    top_el   = root.find(".//p2:Connection/p2:Top/p2:ClipName",  ns)
    next_el  = root.find(".//p2:Connection/p2:Next/p2:ClipName", ns)
    return {
        "name":     name,
        "duration": duration,
        "top":      top_el.text  if top_el  is not None else None,
        "next":     next_el.text if next_el is not None else None,
    }


def build_chain(clip_dir: Path) -> list:
    clips = {}
    for f in clip_dir.glob("*.XML"):
        m = parse_p2_clip(f)
        clips[m["name"]] = m
    if not clips:
        sys.exit(f"[ERROR] No P2 XMLs found in {clip_dir}")
    try:
        first = next(n for n, m in clips.items() if m["top"] == n)
    except StopIteration:
        sys.exit(f"[ERROR] Cannot find chain start (Top == self) in {clip_dir}")
    chain, current = [], first
    while current:
        m = clips.get(current)
        if m is None:
            sys.exit(f"[ERROR] Chain broken: clip '{current}' not found in {clip_dir}")
        chain.append(m)
        current = m["next"]
    return chain


def mxf_path(video_dir: Path, clip_name: str) -> Path:
    p = video_dir / f"{clip_name}.MXF"
    if not p.exists():
        sys.exit(f"[ERROR] MXF not found: {p}")
    return p


def pathurl(p: Path) -> str:
    return "file://localhost" + quote(str(p), safe="/:")


# ---------------------------------------------------------------------------
# XML fragment helpers
# ---------------------------------------------------------------------------

def _rate() -> str:
    return "<rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>"


def _logginginfo() -> str:
    return (
        "<logginginfo><description></description><scene></scene>"
        "<shottake></shottake><lognote></lognote><good></good>"
        "<originalvideofilename></originalvideofilename>"
        "<originalaudiofilename></originalaudiofilename></logginginfo>"
    )


def _colorinfo() -> str:
    return (
        "<colorinfo><lut></lut><lut1></lut1><asc_sop></asc_sop>"
        "<asc_sat></asc_sat><lut2></lut2></colorinfo>"
    )


def _file_element(file_id: int, clip: dict, video_path: Path) -> str:
    url = pathurl(video_path)
    d   = clip["duration"]
    return (
        f'<file id="file-{file_id}">'
        f"<name>{clip['name']}</name>"
        f"<pathurl>{url}</pathurl>"
        f"{_rate()}"
        f"<duration>{d}</duration>"
        f"<timecode>{_rate()}<string>00:00:00:00</string>"
        f"<frame>0</frame><displayformat>NDF</displayformat></timecode>"
        f"<media>"
        f"<video><samplecharacteristics>{_rate()}"
        f"<width>1440</width><height>1080</height>"
        f"<anamorphic>FALSE</anamorphic>"
        f"<pixelaspectratio>HD-(1440x1080)</pixelaspectratio>"
        f"<fielddominance>upper</fielddominance>"
        f"</samplecharacteristics></video>"
        f"<audio><samplecharacteristics>"
        f"<depth>16</depth><samplerate>48000</samplerate>"
        f"</samplecharacteristics><channelcount>4</channelcount></audio>"
        f"</media></file>"
    )


def _links_for_video(clip_id, track_idx, clip_idx, aud_ids, aud_tracks) -> str:
    parts = [
        f"<link><linkclipref>clipitem-{clip_id}</linkclipref>"
        f"<mediatype>video</mediatype><trackindex>{track_idx}</trackindex>"
        f"<clipindex>{clip_idx}</clipindex></link>"
    ]
    for aid, atr in zip(aud_ids, aud_tracks):
        parts.append(
            f"<link><linkclipref>clipitem-{aid}</linkclipref>"
            f"<mediatype>audio</mediatype><trackindex>{atr}</trackindex>"
            f"<clipindex>{clip_idx}</clipindex><groupindex>1</groupindex></link>"
        )
    return "".join(parts)


def _links_for_audio(vid_id, vid_track, clip_idx, sib_ids, sib_tracks) -> str:
    parts = [
        f"<link><linkclipref>clipitem-{vid_id}</linkclipref>"
        f"<mediatype>video</mediatype><trackindex>{vid_track}</trackindex>"
        f"<clipindex>{clip_idx}</clipindex></link>"
    ]
    for sid, str_ in zip(sib_ids, sib_tracks):
        parts.append(
            f"<link><linkclipref>clipitem-{sid}</linkclipref>"
            f"<mediatype>audio</mediatype><trackindex>{str_}</trackindex>"
            f"<clipindex>{clip_idx}</clipindex><groupindex>1</groupindex></link>"
        )
    return "".join(parts)


def _video_clipitem(clip_id, mc_id, file_id, clip, t_start,
                    track_idx, clip_idx, aud_ids, aud_tracks, video_path) -> str:
    d    = clip["duration"]
    t_end = t_start + d
    ticks = d * TICKS_PER_FRAME
    file_el = _file_element(file_id, clip, video_path)
    links   = _links_for_video(clip_id, track_idx, clip_idx, aud_ids, aud_tracks)
    return (
        f'<clipitem id="clipitem-{clip_id}">'
        f"<masterclipid>masterclip-{mc_id}</masterclipid>"
        f"<name>{clip['name']}</name>"
        f"<enabled>TRUE</enabled><duration>{d}</duration>"
        f"{_rate()}"
        f"<start>{t_start}</start><end>{t_end}</end>"
        f"<in>0</in><out>{d}</out>"
        f"<pproTicksIn>0</pproTicksIn><pproTicksOut>{ticks}</pproTicksOut>"
        f"<alphatype>none</alphatype>"
        f"<pixelaspectratio>HD-(1440x1080)</pixelaspectratio>"
        f"<anamorphic>FALSE</anamorphic>"
        f"{file_el}"
        f"{links}"
        f"{_logginginfo()}{_colorinfo()}"
        f"<labels><label2>Iris</label2></labels>"
        f"</clipitem>"
    )


def _audio_clipitem(clip_id, mc_id, file_id, clip, t_start,
                    src_ch, track_idx, clip_idx,
                    vid_id, vid_track, sib_ids, sib_tracks) -> str:
    d     = clip["duration"]
    t_end = t_start + d
    ticks = d * TICKS_PER_FRAME
    links = _links_for_audio(vid_id, vid_track, clip_idx, sib_ids, sib_tracks)
    return (
        f'<clipitem id="clipitem-{clip_id}" premiereChannelType="mono">'
        f"<masterclipid>masterclip-{mc_id}</masterclipid>"
        f"<name>{clip['name']}</name>"
        f"<enabled>TRUE</enabled><duration>{d}</duration>"
        f"{_rate()}"
        f"<start>{t_start}</start><end>{t_end}</end>"
        f"<in>0</in><out>{d}</out>"
        f"<pproTicksIn>0</pproTicksIn><pproTicksOut>{ticks}</pproTicksOut>"
        f'<file id="file-{file_id}"/>'
        f"<sourcetrack><mediatype>audio</mediatype>"
        f"<trackindex>{src_ch}</trackindex></sourcetrack>"
        f"{links}"
        f"{_logginginfo()}{_colorinfo()}"
        f"<labels><label2>Iris</label2></labels>"
        f"</clipitem>"
    )


_AUD_TRACK_ATTRS = (
    'TL.SQTrackAudioKeyframeStyle="0" TL.SQTrackShy="0" '
    'TL.SQTrackExpandedHeight="25" TL.SQTrackExpanded="0" '
    'MZ.TrackTargeted="0" PannerCurrentValue="0.5" PannerIsInverted="true" '
    'PannerStartKeyframe="-91445760000000000,0.5,0,0,0,0,0,0" '
    'PannerName="Balance" currentExplodedTrackIndex="0" '
    'totalExplodedTrackCount="1" premiereTrackType="Stereo"'
)


def _audio_track_xml(clipitems: list, out_chan: int) -> str:
    items = "".join(clipitems)
    return (
        f"<track {_AUD_TRACK_ATTRS}>"
        f"{items}"
        f"<enabled>TRUE</enabled><locked>FALSE</locked>"
        f"<outputchannelindex>{out_chan}</outputchannelindex>"
        f"</track>"
    )


# ---------------------------------------------------------------------------
# Main XML builder
# ---------------------------------------------------------------------------

def generate_xml(main_chain: list, div_chain: list, output_path: Path):
    OUTPUT_DIR.mkdir(exist_ok=True)

    n_main = len(main_chain)
    n_div  = len(div_chain)

    # Clipitem ID layout (1-based, contiguous blocks):
    #   MAIN video   :   1 ..   n_main
    #   MAIN aud ch1 :   n_main+1 .. 2*n_main
    #   MAIN aud ch2 : 2*n_main+1 .. 3*n_main
    #   MAIN aud ch3 : 3*n_main+1 .. 4*n_main
    #   MAIN aud ch4 : 4*n_main+1 .. 5*n_main
    #   DIV  video   : 5*n_main+1 .. 5*n_main+n_div
    #   DIV  aud ch1 : 5*n_main+  n_div+1 .. 5*n_main+2*n_div
    #   ... (same pattern)
    def main_vid_id(i):     return 1 + i
    def main_aud_id(ch, i): return n_main * (1 + ch) + 1 + i  # ch 0-3
    def div_vid_id(i):      return 5 * n_main + 1 + i
    def div_aud_id(ch, i):  return 5 * n_main + n_div * (1 + ch) + 1 + i

    # File IDs: one per MXF file
    def main_fid(i): return 1 + i
    def div_fid(i):  return n_main + 1 + i

    MAIN_VID_TRACK  = 2
    DIV_VID_TRACK   = 1
    MAIN_AUD_TRACKS = [1, 2, 3, 4]
    DIV_AUD_TRACKS  = [5, 6, 7, 8]
    OUT_CHANS       = [1, 2, 1, 2]   # alternating L/R for tracks 1-4

    # Timeline offsets per camera
    main_starts, pos = [], 0
    for c in main_chain:
        main_starts.append(pos); pos += c["duration"]
    main_total = pos

    div_starts, pos = [], 0
    for c in div_chain:
        div_starts.append(pos); pos += c["duration"]
    div_total = pos

    seq_duration   = max(main_total, div_total)
    work_out_ticks = seq_duration * TICKS_PER_FRAME

    # ---- MAIN CAM video clipitems ----
    main_vid_items = []
    for i, (clip, start) in enumerate(zip(main_chain, main_starts)):
        aud_ids = [main_aud_id(ch, i) for ch in range(4)]
        main_vid_items.append(_video_clipitem(
            clip_id=main_vid_id(i), mc_id=main_fid(i), file_id=main_fid(i),
            clip=clip, t_start=start,
            track_idx=MAIN_VID_TRACK, clip_idx=i + 1,
            aud_ids=aud_ids, aud_tracks=MAIN_AUD_TRACKS,
            video_path=mxf_path(MAIN_VIDEO_DIR, clip["name"]),
        ))

    # ---- DIV CAM video clipitems ----
    div_vid_items = []
    for i, (clip, start) in enumerate(zip(div_chain, div_starts)):
        aud_ids = [div_aud_id(ch, i) for ch in range(4)]
        div_vid_items.append(_video_clipitem(
            clip_id=div_vid_id(i), mc_id=div_fid(i), file_id=div_fid(i),
            clip=clip, t_start=start,
            track_idx=DIV_VID_TRACK, clip_idx=i + 1,
            aud_ids=aud_ids, aud_tracks=DIV_AUD_TRACKS,
            video_path=mxf_path(DIV_VIDEO_DIR, clip["name"]),
        ))

    # ---- MAIN CAM audio clipitems (4 channels × n_main clips) ----
    main_aud_tracks_xml = []
    for ch in range(4):
        items = []
        for i, (clip, start) in enumerate(zip(main_chain, main_starts)):
            sib_ids = [main_aud_id(c, i) for c in range(4)]
            items.append(_audio_clipitem(
                clip_id=main_aud_id(ch, i), mc_id=main_fid(i), file_id=main_fid(i),
                clip=clip, t_start=start,
                src_ch=ch + 1, track_idx=MAIN_AUD_TRACKS[ch], clip_idx=i + 1,
                vid_id=main_vid_id(i), vid_track=MAIN_VID_TRACK,
                sib_ids=sib_ids, sib_tracks=MAIN_AUD_TRACKS,
            ))
        main_aud_tracks_xml.append(_audio_track_xml(items, OUT_CHANS[ch]))

    # ---- DIV CAM audio clipitems (4 channels × n_div clips) ----
    div_aud_tracks_xml = []
    for ch in range(4):
        items = []
        for i, (clip, start) in enumerate(zip(div_chain, div_starts)):
            sib_ids = [div_aud_id(c, i) for c in range(4)]
            items.append(_audio_clipitem(
                clip_id=div_aud_id(ch, i), mc_id=div_fid(i), file_id=div_fid(i),
                clip=clip, t_start=start,
                src_ch=ch + 1, track_idx=DIV_AUD_TRACKS[ch], clip_idx=i + 1,
                vid_id=div_vid_id(i), vid_track=DIV_VID_TRACK,
                sib_ids=sib_ids, sib_tracks=DIV_AUD_TRACKS,
            ))
        div_aud_tracks_xml.append(_audio_track_xml(items, OUT_CHANS[ch]))

    all_audio_tracks = "".join(main_aud_tracks_xml + div_aud_tracks_xml)
    seq_uuid = str(uuid.uuid4())

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!DOCTYPE xmeml>\n"
        "<xmeml version=\"4\">\n"
        f'  <sequence id="sequence-1"'
        f' TL.SQAudioVisibleBase="0" TL.SQVideoVisibleBase="0"'
        f' TL.SQVisibleBaseTime="0" TL.SQAVDividerPosition="0.19140625"'
        f' TL.SQHideShyTracks="0" TL.SQHeaderWidth="236"'
        f' MZ.WorkOutPoint="{work_out_ticks}"'
        f' MZ.Sequence.AudioTimeDisplayFormat="200"'
        f' MZ.Sequence.PreviewFrameSizeHeight="1080"'
        f' MZ.Sequence.PreviewFrameSizeWidth="1920"'
        f' MZ.Sequence.VideoTimeDisplayFormat="101"'
        f' explodedTracks="true">\n'
        f"    <uuid>{seq_uuid}</uuid>\n"
        f"    <duration>{seq_duration}</duration>\n"
        f"    {_rate()}\n"
        f"    <name>sync</name>\n"
        f"    <media>\n"
        f"      <video>\n"
        f"        <format><samplecharacteristics>"
        f"{_rate()}<width>1920</width><height>1080</height>"
        f"<anamorphic>FALSE</anamorphic><pixelaspectratio>square</pixelaspectratio>"
        f"<fielddominance>none</fielddominance><colordepth>24</colordepth>"
        f"</samplecharacteristics></format>\n"
        f'        <track TL.SQTrackShy="0" TL.SQTrackExpandedHeight="15"'
        f' TL.SQTrackExpanded="0" MZ.TrackTargeted="1">\n'
        f"          {''.join(div_vid_items)}\n"
        f"          <enabled>TRUE</enabled><locked>FALSE</locked>\n"
        f"        </track>\n"
        f'        <track TL.SQTrackShy="0" TL.SQTrackExpandedHeight="15"'
        f' TL.SQTrackExpanded="0" MZ.TrackTargeted="0">\n'
        f"          {''.join(main_vid_items)}\n"
        f"          <enabled>TRUE</enabled><locked>FALSE</locked>\n"
        f"        </track>\n"
        f"      </video>\n"
        f"      <audio>\n"
        f"        <numOutputChannels>2</numOutputChannels>\n"
        f"        <format><samplecharacteristics>"
        f"<depth>16</depth><samplerate>48000</samplerate>"
        f"</samplecharacteristics></format>\n"
        f"        <outputs>"
        f"<group><index>1</index><numchannels>1</numchannels>"
        f"<downmix>0</downmix><channel><index>1</index></channel></group>"
        f"<group><index>2</index><numchannels>1</numchannels>"
        f"<downmix>0</downmix><channel><index>2</index></channel></group>"
        f"</outputs>\n"
        f"        {all_audio_tracks}\n"
        f"      </audio>\n"
        f"    </media>\n"
        f"    <timecode>{_rate()}<string>00:00:00:00</string>"
        f"<frame>0</frame><displayformat>NDF</displayformat></timecode>\n"
        f"    <labels><label2>Forest</label2></labels>\n"
        f"    {_logginginfo()}\n"
        f"  </sequence>\n"
        f"</xmeml>\n"
    )

    output_path.write_text(xml, encoding="utf-8")
    print(f"[OK] Written: {output_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Stage 1: Generate base FCP XML from INPUT/ P2 metadata."
    )
    parser.add_argument("--output", default=str(OUTPUT_DIR / "01_create.xml"),
                        help="Output XML path (default: OUTPUT/01_create.xml)")
    args = parser.parse_args()

    print("[1/2] Building MAIN CAM clip chain...")
    main_chain = build_chain(MAIN_CLIP_DIR)
    total_main = sum(c["duration"] for c in main_chain)
    print(f"      {len(main_chain)} clips — {total_main} frames "
          f"({total_main / 25 / 60:.1f} min)")
    for c in main_chain:
        print(f"        {c['name']}  {c['duration']} frames")

    print("[2/2] Building DIV CAM clip chain...")
    div_chain = build_chain(DIV_CLIP_DIR)
    total_div = sum(c["duration"] for c in div_chain)
    print(f"      {len(div_chain)} clips — {total_div} frames "
          f"({total_div / 25 / 60:.1f} min)")
    for c in div_chain:
        print(f"        {c['name']}  {c['duration']} frames")

    print("[3/3] Generating XML...")
    generate_xml(main_chain, div_chain, Path(args.output))
    print(f"      Sequence duration: {max(total_main, total_div)} frames "
          f"({max(total_main, total_div) / 25 / 60:.1f} min)")


if __name__ == "__main__":
    main()
