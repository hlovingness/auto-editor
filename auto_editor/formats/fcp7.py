from __future__ import annotations

import xml.etree.ElementTree as ET
from fractions import Fraction
from io import StringIO
from math import ceil
from typing import TYPE_CHECKING
from xml.etree.ElementTree import Element

from auto_editor.ffwrapper import FFmpeg, FileInfo
from auto_editor.timeline import ASpace, TlAudio, TlVideo, VSpace, v3

from .utils import Validator, safe_mkdir, show

if TYPE_CHECKING:
    from pathlib import Path

    from auto_editor.utils.log import Log

"""
Premiere Pro uses the Final Cut Pro 7 XML Interchange Format

See docs here:
https://developer.apple.com/library/archive/documentation/AppleApplications/Reference/FinalCutPro_XML/Elements/Elements.html

Also, Premiere itself will happily output subtlety incorrect XML files that don't
come back the way they started.
"""

PIXEL_ASPECT_RATIO = "square"
COLORDEPTH = "24"
ANAMORPHIC = "FALSE"
DEPTH = "16"


def uri_to_path(uri: str) -> str:
    def de_norm(s: str) -> str:
        uri_escape = {
            "3C": "<",
            "3E": ">",
            "23": "#",
            "25": "%",
            "2B": "+",
            "7B": "{",
            "7D": "}",
            "7C": "|",
            "5C": "\\",
            "5E": "^",
            "7E": "~",
            "5B": "[",
            "5D": "]",
            "60": "`",
            "3F": "?",
            "3A": ":",
            "40": "@",
            "3D": "=",
            "2A": "*",
            "29": ")",
            "28": "(",
            "27": "'",
            "26": "&",
            "24": "$",
            "22": '"',
            "21": "!",
        }
        buf = StringIO()
        for i, char in enumerate(s):
            if char == "%" and len(s) > i + 3:
                tag = s[i + 1 : i + 3]
                if tag in uri_escape:
                    buf.write(uri_escape[tag])
                else:
                    buf.write(char)
            else:
                buf.write(char)
        return buf.getvalue()

    if uri.startswith("file://localhost/"):
        return de_norm(uri[16:])
    if uri.startswith("file://"):
        return de_norm(uri[7:])
    return uri

    # /Users/wyattblue/projects/auto-editor/example.mp4
    # file:///Users/wyattblue/projects/auto-editor/example.mp4
    # file://localhost/Users/wyattblue/projects/auto-editor/example.mp4


def set_tb_ntsc(tb: Fraction) -> tuple[int, str]:
    # See chart: https://developer.apple.com/library/archive/documentation/AppleApplications/Reference/FinalCutPro_XML/FrameRate/FrameRate.html#//apple_ref/doc/uid/TP30001158-TPXREF103
    if tb == Fraction(24000, 1001):
        return 24, "TRUE"
    if tb == Fraction(30000, 1001):
        return 30, "TRUE"
    if tb == Fraction(60000, 1001):
        return 60, "TRUE"

    ctb = ceil(tb)
    if ctb not in (24, 30, 60) and ctb * Fraction(999, 1000) == tb:
        return ctb, "TRUE"

    return int(tb), "FALSE"


def read_tb_ntsc(tb: int, ntsc: bool) -> Fraction:
    if ntsc:
        if tb == 24:
            return Fraction(24000, 1001)
        if tb == 30:
            return Fraction(30000, 1001)
        if tb == 60:
            return Fraction(60000, 1001)
        return tb * Fraction(999, 1000)

    return Fraction(tb)


def speedup(speed: float) -> Element:
    fil = Element("filter")
    effect = ET.SubElement(fil, "effect")
    ET.SubElement(effect, "name").text = "Time Remap"
    ET.SubElement(effect, "effectid").text = "timeremap"

    para = ET.SubElement(effect, "parameter", authoringApp="PremierePro")
    ET.SubElement(para, "parameterid").text = "variablespeed"
    ET.SubElement(para, "name").text = "variablespeed"
    ET.SubElement(para, "valuemin").text = "0"
    ET.SubElement(para, "valuemax").text = "1"
    ET.SubElement(para, "value").text = "0"

    para2 = ET.SubElement(effect, "parameter", authoringApp="PremierePro")
    ET.SubElement(para2, "parameterid").text = "speed"
    ET.SubElement(para2, "name").text = "speed"
    ET.SubElement(para2, "valuemin").text = "-100000"
    ET.SubElement(para2, "valuemax").text = "100000"
    ET.SubElement(para2, "value").text = str(speed)

    para3 = ET.SubElement(effect, "parameter", authoringApp="PremierePro")
    ET.SubElement(para3, "parameterid").text = "frameblending"
    ET.SubElement(para3, "name").text = "frameblending"
    ET.SubElement(para3, "value").text = "FALSE"

    return fil


SUPPORTED_EFFECTS = ["timeremap"]


def read_filters(clipitem: Element, log: Log) -> float:
    for effect_tag in clipitem:
        if len(effect_tag) < 3:
            log.error("effect tag requires: <effectid> <name> and one <parameter>")
        for i, effects in enumerate(effect_tag):
            if i == 0 and effects.tag != "name":
                log.error("<effect>: <name> must be first tag")
            if i == 1 and effects.tag != "effectid":
                log.error("<effect>: <effectid> must be second tag")
                if effects.text not in SUPPORTED_EFFECTS:
                    log.error(f"`{effects.text}` is not a supported effect.")

            if i > 1:
                for j, parms in enumerate(effects):
                    if j == 0:
                        if parms.tag != "parameterid":
                            log.error("<parameter>: <parameterid> must be first tag")
                        if parms.text != "speed":
                            break

                    if j > 0 and parms.tag == "value":
                        if parms.text is None:
                            log.error("<value>: number required")
                        return float(parms.text) / 100

    return 1.0


def fcp7_read_xml(path: str, ffmpeg: FFmpeg, log: Log) -> v3:
    def xml_bool(val: str) -> bool:
        if val == "TRUE":
            return True
        if val == "FALSE":
            return False
        raise TypeError("Value must be 'TRUE' or 'FALSE'")

    try:
        tree = ET.parse(path)
    except FileNotFoundError:
        log.nofile(path)

    root = tree.getroot()

    valid = Validator(log)

    valid.check(root, "xmeml")
    valid.check(root[0], "sequence")
    result = valid.parse(
        root[0],
        {
            "name": str,
            "duration": int,
            "rate": {
                "timebase": Fraction,
                "ntsc": xml_bool,
            },
            "media": None,
        },
    )

    tb = read_tb_ntsc(result["rate"]["timebase"], result["rate"]["ntsc"])

    av = valid.parse(
        result["media"],
        {
            "video": None,
            "audio": None,
        },
    )

    sources: dict[str, FileInfo] = {}
    vobjs: VSpace = []
    aobjs: ASpace = []

    vclip_schema = {
        "format": {
            "samplecharacteristics": {
                "width": int,
                "height": int,
            },
        },
        "track": {
            "__arr": "",
            "clipitem": {
                "__arr": "",
                "start": int,
                "end": int,
                "in": int,
                "out": int,
                "file": None,
                "filter": None,
            },
        },
    }

    aclip_schema = {
        "format": {"samplecharacteristics": {"samplerate": int}},
        "track": {
            "__arr": "",
            "clipitem": {
                "__arr": "",
                "start": int,
                "end": int,
                "in": int,
                "out": int,
                "file": None,
                "filter": None,
            },
        },
    }

    sr = 48000
    res = (1920, 1080)

    if "video" in av:
        tracks = valid.parse(av["video"], vclip_schema)

        width = tracks["format"]["samplecharacteristics"]["width"]
        height = tracks["format"]["samplecharacteristics"]["height"]
        res = width, height

        for t, track in enumerate(tracks["track"]):
            if len(track["clipitem"]) > 0:
                vobjs.append([])
            for i, clipitem in enumerate(track["clipitem"]):
                file_id = clipitem["file"].attrib["id"]
                if file_id not in sources:
                    fileobj = valid.parse(clipitem["file"], {"pathurl": str})

                    if "pathurl" in fileobj:
                        sources[file_id] = FileInfo(
                            uri_to_path(fileobj["pathurl"]),
                            ffmpeg,
                            log,
                            str(len(sources)),
                        )
                    else:
                        show(clipitem["file"], 3)
                        log.error(
                            f"'pathurl' child element not found in {clipitem['file'].tag}"
                        )

                if "filter" in clipitem:
                    speed = read_filters(clipitem["filter"], log)
                else:
                    speed = 1.0

                start = clipitem["start"]
                dur = clipitem["end"] - start
                offset = int(clipitem["in"] * speed)

                vobjs[t].append(TlVideo(start, dur, file_id, offset, speed, stream=0))

    if "audio" in av:
        tracks = valid.parse(av["audio"], aclip_schema)
        sr = tracks["format"]["samplecharacteristics"]["samplerate"]

        for t, track in enumerate(tracks["track"]):
            if len(track["clipitem"]) > 0:
                aobjs.append([])
            for i, clipitem in enumerate(track["clipitem"]):
                file_id = clipitem["file"].attrib["id"]
                if file_id not in sources:
                    fileobj = valid.parse(clipitem["file"], {"pathurl": str})
                    sources[file_id] = FileInfo(
                        uri_to_path(fileobj["pathurl"]), ffmpeg, log, str(len(sources))
                    )

                if "filter" in clipitem:
                    speed = read_filters(clipitem["filter"], log)
                else:
                    speed = 1.0

                start = clipitem["start"]
                dur = clipitem["end"] - start
                offset = int(clipitem["in"] * speed)

                aobjs[t].append(
                    TlAudio(start, dur, file_id, offset, speed, volume=1, stream=0)
                )

    return v3(sources, tb, sr, res, "#000", vobjs, aobjs, None)


def path_resolve(path: Path, flavor: str) -> str:
    if flavor == "resolve":
        return path.resolve().as_uri()
    return f"{path.resolve()}"


def media_def(
    filedef: Element, url: str, src: FileInfo, tl: v3, tb: int, ntsc: str
) -> None:
    ET.SubElement(filedef, "name").text = src.path.stem
    ET.SubElement(filedef, "pathurl").text = url

    rate = ET.SubElement(filedef, "rate")
    ET.SubElement(rate, "timebase").text = f"{tb}"
    ET.SubElement(rate, "ntsc").text = ntsc

    # DaVinci Resolve needs this tag even though it's blank
    ET.SubElement(filedef, "duration").text = ""

    mediadef = ET.SubElement(filedef, "media")

    if len(src.videos) > 0:
        videodef = ET.SubElement(mediadef, "video")

        vschar = ET.SubElement(videodef, "samplecharacteristics")
        rate = ET.SubElement(vschar, "rate")
        ET.SubElement(rate, "timebase").text = f"{tb}"
        ET.SubElement(rate, "ntsc").text = ntsc
        ET.SubElement(vschar, "width").text = f"{tl.res[0]}"
        ET.SubElement(vschar, "height").text = f"{tl.res[1]}"
        ET.SubElement(vschar, "anamorphic").text = ANAMORPHIC
        ET.SubElement(vschar, "pixelaspectratio").text = PIXEL_ASPECT_RATIO

    if len(src.audios) > 0:
        audiodef = ET.SubElement(mediadef, "audio")
        aschar = ET.SubElement(audiodef, "samplecharacteristics")
        ET.SubElement(aschar, "depth").text = DEPTH
        ET.SubElement(aschar, "samplerate").text = f"{tl.sr}"
        ET.SubElement(audiodef, "channelcount").text = f"{src.audios[0].channels}"


def fcp7_write_xml(
    name: str, ffmpeg: FFmpeg, output: str, tl: v3, flavor: str, log: Log
) -> None:
    width, height = tl.res
    timebase, ntsc = set_tb_ntsc(tl.tb)

    key_to_url: dict[tuple[str, int], str] = {}
    key_to_id: dict[tuple[str, int], str] = {}
    key_to_source: dict[tuple[str, int], FileInfo] = {}
    file_defs: set[str] = set()  # Contains urls

    for key, src in tl.sources.items():
        key_to_source[(key, 0)] = src
        key_to_id[(key, 0)] = f"file-{len(key_to_id)+1}"
        key_to_url[(key, 0)] = path_resolve(src.path, flavor)

        if len(src.audios) > 1:
            fold = src.path.parent / f"{src.path.stem}_tracks"
            safe_mkdir(fold)

            for i in range(1, len(src.audios)):
                newtrack = fold / f"{i}.wav"

                ffmpeg.run(
                    ["-i", f"{src.path.resolve()}", "-map", f"0:a:{i}", f"{newtrack}"]
                )

                key_to_source[(key, i)] = FileInfo(f"{newtrack}", ffmpeg, log, key)
                key_to_url[(key, i)] = path_resolve(newtrack, flavor)
                key_to_id[(key, i)] = f"file-{len(key_to_id)+1}"

    xmeml = ET.Element("xmeml", version="4")
    sequence = ET.SubElement(xmeml, "sequence")
    ET.SubElement(sequence, "name").text = name
    ET.SubElement(sequence, "duration").text = f"{int(tl.out_len())}"
    rate = ET.SubElement(sequence, "rate")
    ET.SubElement(rate, "timebase").text = f"{timebase}"
    ET.SubElement(rate, "ntsc").text = ntsc
    media = ET.SubElement(sequence, "media")
    video = ET.SubElement(media, "video")
    vformat = ET.SubElement(video, "format")
    vschar = ET.SubElement(vformat, "samplecharacteristics")

    rate = ET.SubElement(vschar, "rate")
    ET.SubElement(rate, "timebase").text = f"{timebase}"
    ET.SubElement(rate, "ntsc").text = ntsc
    ET.SubElement(vschar, "fielddominance").text = "none"
    ET.SubElement(vschar, "colordepth").text = COLORDEPTH
    ET.SubElement(vschar, "width").text = f"{width}"
    ET.SubElement(vschar, "height").text = f"{height}"
    ET.SubElement(vschar, "pixelaspectratio").text = PIXEL_ASPECT_RATIO

    if len(tl.v) > 0 and len(tl.v[0]) > 0:
        track = ET.SubElement(video, "track")

        for j, clip in enumerate(tl.v[0]):
            assert isinstance(clip, TlVideo)
            src = tl.sources[clip.src]

            _start = f"{clip.start}"
            _end = f"{clip.start + clip.dur}"
            _in = f"{int(clip.offset / clip.speed)}"
            _out = f"{int(clip.offset / clip.speed) + clip.dur}"

            clipitem = ET.SubElement(track, "clipitem", id=f"clipitem-{j+1}")
            ET.SubElement(clipitem, "masterclipid").text = "masterclip-2"
            ET.SubElement(clipitem, "name").text = src.path.stem
            ET.SubElement(clipitem, "start").text = _start
            ET.SubElement(clipitem, "end").text = _end
            ET.SubElement(clipitem, "in").text = _in
            ET.SubElement(clipitem, "out").text = _out

            _id = key_to_id[(clip.src, clip.stream)]
            filedef = ET.SubElement(clipitem, "file", id=_id)

            pathurl = key_to_url[(clip.src, clip.stream)]
            my_src = key_to_source[(clip.src, clip.stream)]
            if pathurl not in file_defs:
                media_def(filedef, pathurl, my_src, tl, timebase, ntsc)
                file_defs.add(pathurl)

            if clip.speed != 1:
                clipitem.append(speedup(clip.speed * 100))

            for i in range(len(src.audios) + 1):
                link = ET.SubElement(clipitem, "link")
                ET.SubElement(
                    link, "linkclipref"
                ).text = f"clipitem-{(i*(len(tl.v[0])))+j+1}"
                ET.SubElement(link, "mediatype").text = "video" if i == 0 else "audio"
                ET.SubElement(link, "trackindex").text = str(max(i, 1))
                ET.SubElement(link, "clipindex").text = str(j + 1)
                if i > 0:
                    ET.SubElement(link, "groupindex").text = "1"

    # Audio definitions and clips
    audio = ET.SubElement(media, "audio")
    ET.SubElement(audio, "numOutputChannels").text = "2"
    aformat = ET.SubElement(audio, "format")
    aschar = ET.SubElement(aformat, "samplecharacteristics")
    ET.SubElement(aschar, "depth").text = DEPTH
    ET.SubElement(aschar, "samplerate").text = str(tl.sr)

    for t, aclips in enumerate(tl.a):
        track = ET.Element(
            "track", currentExplodedTrackIndex="0", premiereTrackType="Stereo"
        )

        for j, aclip in enumerate(aclips):
            src = tl.sources[aclip.src]

            _start = f"{aclip.start}"
            _end = f"{aclip.start + aclip.dur}"
            _in = f"{int(aclip.offset / aclip.speed)}"
            _out = f"{int(aclip.offset / aclip.speed) + aclip.dur}"

            if not src.videos:
                clip_item_num = j + 1
                master_id = "1"
            else:
                clip_item_num = len(aclips) + 1 + j + (t * len(aclips))
                master_id = "2"

            clipitem = ET.SubElement(
                track,
                "clipitem",
                id=f"clipitem-{clip_item_num}",
                premiereChannelType="stereo",
            )
            ET.SubElement(clipitem, "masterclipid").text = f"masterclip-{master_id}"
            ET.SubElement(clipitem, "name").text = src.path.stem
            ET.SubElement(clipitem, "start").text = _start
            ET.SubElement(clipitem, "end").text = _end
            ET.SubElement(clipitem, "in").text = _in
            ET.SubElement(clipitem, "out").text = _out

            _id = key_to_id[(aclip.src, aclip.stream)]
            filedef = ET.SubElement(clipitem, "file", id=_id)

            pathurl = key_to_url[(aclip.src, aclip.stream)]
            my_src = key_to_source[(aclip.src, aclip.stream)]
            if pathurl not in file_defs:
                media_def(filedef, pathurl, my_src, tl, timebase, ntsc)
                file_defs.add(pathurl)

            sourcetrack = ET.SubElement(clipitem, "sourcetrack")
            ET.SubElement(sourcetrack, "mediatype").text = "audio"
            ET.SubElement(sourcetrack, "trackindex").text = "1"
            labels = ET.SubElement(clipitem, "labels")
            ET.SubElement(labels, "label2").text = "Iris"

            if aclip.speed != 1:
                clipitem.append(speedup(aclip.speed * 100))

            if src.videos:
                ET.SubElement(clipitem, "outputchannelindex").text = "1"

        audio.append(track)

    tree = ET.ElementTree(xmeml)
    ET.indent(tree, space="\t", level=0)
    tree.write(output, xml_declaration=True, encoding="utf-8")