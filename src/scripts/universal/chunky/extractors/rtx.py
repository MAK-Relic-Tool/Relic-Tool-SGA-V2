import argparse
from typing import Dict

from relic.chunky import GenericRelicChunky
from relic.chunky_formats.dow.rtx import RtxChunky, write_rtx
from scripts.universal.chunky.extractors.common import get_runner
from scripts.universal.common import SharedExtractorParser


def add_args(parser: argparse.ArgumentParser):
    parser.add_argument("-f", "--fmt", "--format", default=None, choices=["png", "tga", "dds"], type=str.lower,  help="Choose what format to convert textures to.")
    parser.add_argument("-c", "-t", "--conv", "--converter", "--texconv", help="Path to texconv.exe to use.")


def build_parser():
    parser = argparse.ArgumentParser(prog="RTX 2 Image", description="Convert Relic RTX (Default Texture) files to Images.", parents=[SharedExtractorParser])
    add_args(parser)
    return parser


def extract_rtx(output_path: str, chunky: GenericRelicChunky, out_format: str, texconv_path: str) -> None:
    rtx = RtxChunky.convert(chunky)
    write_rtx(output_path, rtx, out_format=out_format, texconv_path=texconv_path)


def extract_args(args: argparse.Namespace) -> Dict:
    return {'out_format': args.fmt, 'texconv_path': args.conv}


Runner = get_runner(extract_rtx, extract_args, ["rtx"], True)


if __name__ == "__main__":
    p = build_parser()
    args = p.parse_args()
    Runner(args)