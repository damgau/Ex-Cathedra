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
    # Canonical FCP-XML Windows form (matches what Premiere itself exports):
    #   file://localhost/G%3A/MATELE/EX%20Cathedra/.../0440RX.MXF
    # as_posix() forces forward slashes; safe="/" keeps separators but
    # percent-encodes the drive colon (%3A) and spaces (%20).
    return "file://localhost/" + quote(p.as_posix(), safe="/")


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


def _file_element(file_id: int, clip: dict, video_path: Path, file_duration: int = None) -> str:
    url = pathurl(video_path)
    d   = file_duration if file_duration is not None else clip["duration"]
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
                    track_idx, clip_idx, aud_ids, aud_tracks, video_path,
                    file_duration=None) -> str:
    d    = clip["duration"]
    t_end = t_start + d
    ticks = d * TICKS_PER_FRAME
    file_el = _file_element(file_id, clip, video_path, file_duration)
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

    main_total = sum(c["duration"] for c in main_chain)
    div_total  = sum(c["duration"] for c in div_chain)
    seq_duration   = max(main_total, div_total)
    work_out_ticks = seq_duration * TICKS_PER_FRAME

    # One clipitem per camera: reference the Top (first) physical MXF with
    # out = full chain duration. Premiere reads the P2 CLIP XML to span
    # across all physical files automatically (see panasonic_p2_reference.md §6).
    # <file>/<duration> stays at the physical file's actual frame count.
    main_top  = main_chain[0]
    div_top   = div_chain[0]
    main_clip = {"name": main_top["name"], "duration": main_total}
    div_clip  = {"name": div_top["name"],  "duration": div_total}

    # Fixed clipitem IDs — 10 total (1 vid + 4 aud) × 2 cameras
    MAIN_VID_ID  = 1
    MAIN_AUD_IDS = [2, 3, 4, 5]
    DIV_VID_ID   = 6
    DIV_AUD_IDS  = [7, 8, 9, 10]
    MAIN_FILE_ID = 1
    DIV_FILE_ID  = 2

    MAIN_VID_TRACK  = 2
    DIV_VID_TRACK   = 1
    MAIN_AUD_TRACKS = [1, 2, 3, 4]
    DIV_AUD_TRACKS  = [5, 6, 7, 8]
    OUT_CHANS       = [1, 2, 1, 2]

    main_vid_item = _video_clipitem(
        clip_id=MAIN_VID_ID, mc_id=MAIN_FILE_ID, file_id=MAIN_FILE_ID,
        clip=main_clip, t_start=0,
        track_idx=MAIN_VID_TRACK, clip_idx=1,
        aud_ids=MAIN_AUD_IDS, aud_tracks=MAIN_AUD_TRACKS,
        video_path=mxf_path(MAIN_VIDEO_DIR, main_top["name"]),
        file_duration=main_top["duration"],
    )

    div_vid_item = _video_clipitem(
        clip_id=DIV_VID_ID, mc_id=DIV_FILE_ID, file_id=DIV_FILE_ID,
        clip=div_clip, t_start=0,
        track_idx=DIV_VID_TRACK, clip_idx=1,
        aud_ids=DIV_AUD_IDS, aud_tracks=DIV_AUD_TRACKS,
        video_path=mxf_path(DIV_VIDEO_DIR, div_top["name"]),
        file_duration=div_top["duration"],
    )

    main_aud_tracks_xml = []
    for ch in range(4):
        item = _audio_clipitem(
            clip_id=MAIN_AUD_IDS[ch], mc_id=MAIN_FILE_ID, file_id=MAIN_FILE_ID,
            clip=main_clip, t_start=0,
            src_ch=ch + 1, track_idx=MAIN_AUD_TRACKS[ch], clip_idx=1,
            vid_id=MAIN_VID_ID, vid_track=MAIN_VID_TRACK,
            sib_ids=MAIN_AUD_IDS, sib_tracks=MAIN_AUD_TRACKS,
        )
        main_aud_tracks_xml.append(_audio_track_xml([item], OUT_CHANS[ch]))

    div_aud_tracks_xml = []
    for ch in range(4):
        item = _audio_clipitem(
            clip_id=DIV_AUD_IDS[ch], mc_id=DIV_FILE_ID, file_id=DIV_FILE_ID,
            clip=div_clip, t_start=0,
            src_ch=ch + 1, track_idx=DIV_AUD_TRACKS[ch], clip_idx=1,
            vid_id=DIV_VID_ID, vid_track=DIV_VID_TRACK,
            sib_ids=DIV_AUD_IDS, sib_tracks=DIV_AUD_TRACKS,
        )
        div_aud_tracks_xml.append(_audio_track_xml([item], OUT_CHANS[ch]))

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
        f"{_rate()}<width>1440</width><height>1080</height>"
        f"<anamorphic>FALSE</anamorphic><pixelaspectratio>HD-(1440x1080)</pixelaspectratio>"
        f"<fielddominance>none</fielddominance><colordepth>24</colordepth>"
        f"</samplecharacteristics></format>\n"
        f'        <track TL.SQTrackShy="0" TL.SQTrackExpandedHeight="15"'
        f' TL.SQTrackExpanded="0" MZ.TrackTargeted="1">\n'
        f"          {div_vid_item}\n"
        f"          <enabled>TRUE</enabled><locked>FALSE</locked>\n"
        f"        </track>\n"
        f'        <track TL.SQTrackShy="0" TL.SQTrackExpandedHeight="15"'
        f' TL.SQTrackExpanded="0" MZ.TrackTargeted="0">\n'
        f"          {main_vid_item}\n"
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
