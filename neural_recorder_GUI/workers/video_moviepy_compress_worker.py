import argparse
import os
import sys
from moviepy.video.io.VideoFileClip import VideoFileClip

try:
    from ..support.windows_runtime import apply_windows_process_role
except ImportError:
    try:
        from support.windows_runtime import apply_windows_process_role
    except ImportError:
        def apply_windows_process_role(role: str, active_recording: bool = False) -> None:
            return None

def compress_and_replace(video_path: str, bitrate: str = "256k") -> int:
    # try:
    #     from moviepy import VideoFileClip
    # except ImportError:
    #     print("moviepy is not installed. Please install it with: pip install moviepy")
    #     return 5

    source_path = os.path.abspath(video_path)
    if not os.path.isfile(source_path):
        return 2

    folder = os.path.dirname(source_path)
    name, ext = os.path.splitext(os.path.basename(source_path))
    temp_output_path = os.path.join(folder, f"{name}.compressed{ext}")

    try:
        with VideoFileClip(source_path) as clip:
            clip.write_videofile(
                temp_output_path,
                codec='libx264',
                bitrate=bitrate,
                audio_codec='copy',
                preset='medium',
                ffmpeg_params=['-pix_fmt', 'yuv420p'],
            )
    except Exception as e:
        print(f"Error during compression: {e}")
        return 1

    if not os.path.isfile(temp_output_path) or os.path.getsize(temp_output_path) == 0:
        return 4

    os.replace(temp_output_path, source_path)
    print(f"Compressed and replaced: {source_path}")
    return 0


def main() -> int:
    apply_windows_process_role("video_compress")
    parser = argparse.ArgumentParser(description="Compress video using moviepy VideoFileClip")
    parser.add_argument("video_path", help="Path to the video file to compress")
    parser.add_argument(
        "--bitrate", "-b",
        default="256k",
        help="Target bitrate (e.g., '256k', '1M', '500k'). Default: 256k"
    )
    args = parser.parse_args()
    return compress_and_replace(args.video_path, args.bitrate)


if __name__ == "__main__":
    sys.exit(main())
