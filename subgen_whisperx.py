import sys
import os
import ffmpeg
import whisperx
import utils.timer as timer
import logging
import coloredlogs
from datetime import datetime
from typing import Dict, Tuple, List
from torch import cuda
from utils.constants import DEFAULT_INPUT_VIDEO, MODELS_AVAILABLE
import argparse
from halo import Halo

# Setup logging
log_dir = "logs"
os.makedirs(log_dir, exist_ok=True)
log_filename = os.path.join(
    log_dir, f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_subgen.log"
)
LOGGING_LEVEL = ["ERROR", logging.ERROR]
logging.basicConfig(filename=log_filename, filemode="a", level=LOGGING_LEVEL[1])
coloredlogs.install(level=LOGGING_LEVEL[0])

# Init global timer
stopwatch: timer.Timer = timer.Timer(LOGGING_LEVEL[0])


def get_device(device_selection: str = None) -> str:
    """Determine the best available device with graceful fallback"""
    logger = logging.getLogger("get_device")
    # Check if an nVidia card is available
    # If nvida-smi is not available, it will fall back to CPU
    # if os.system("nvidia-smi") == 0:
    #     logger.info("nVidia GPU detected.")
    #     if cuda.is_available():
    #         logger.info("CUDA available.")
    #         return "cuda"
    #     else:
    #         logger.warning("CUDA is not accessible on your nVidia GPU.")
    #         logger.warning(
    #             "Please refer to the CUDNN and CUDA installation guide at https://developer.nvidia.com/cudnn and https://developer.nvidia.com/cuda-downloads."
    #         )
    #         logger.warning("Using CPU instead.")
    #         return "cpu"
    # else:
    #     logger.info("nVidia GPU not available.")

    if device_selection is None or "cuda" in device_selection.lower():
        try:
            if cuda.is_available():
                logger.debug("CUDA available.")
                return "cuda"
            else:
                logger.warning("CUDA not available, falling back to CPU")
        except Exception as e:
            logger.error(f"Warning: Error checking CUDA availability ({str(e)})")
            logger.warning("Falling back to CPU.")
    else:
        pass
    return "cpu"


# Function to check if media file is valid
def is_media_file(file_path):
    logger = logging.getLogger("is_media_file")
    _valid_media_flag = False
    _valid_audio_flag = False
    try:
        probe = ffmpeg.probe(file_path)
        # Check whether a media stream exists in the file
        if len(probe["streams"]) > 0:
            stream_type: str = probe["streams"][0]["codec_type"]
            if stream_type == "audio" or stream_type == "video":
                _valid_media_flag = True
                if stream_type == "audio":
                    _valid_audio_flag = True
        return _valid_media_flag, _valid_audio_flag
    except Exception as e:
        logger.error(f"An error occurred while probing the file: {e}")
        return False, False


def get_media_files(directory: str = None, file: str = None) -> List[Tuple[str, bool]]:
    """Get list of valid media files from directory and/or single file.

    Args:
        directory (str, optional): Directory path to search for media files
        file (str, optional): Single file path to check

    Returns:
        List[Tuple[str, bool]]: List of tuples containing (file_path, is_audio_flag)
    """
    logger = logging.getLogger("get_media_files")
    media_files: List[Tuple[str, bool]] = []

    if directory:
        for root, _, files in os.walk(directory):
            for f in files:
                file_path = os.path.join(root, f)
                is_valid, is_audio = is_media_file(file_path)
                if is_valid:
                    media_files.append((file_path, is_audio))

        if not media_files:
            logger.error(f"No valid media files found in directory '{directory}'")
            return None

    if file:
        is_valid, is_audio = is_media_file(file)
        if is_valid:
            media_files.append((file, is_audio))
        else:
            logger.error(f"Error: File '{file}' is not a valid media file.")
            return None

    return media_files


def extract_audio(video_path: str = DEFAULT_INPUT_VIDEO) -> str:
    """Extract audio from input video file using ffmpeg.
    This function extracts the audio track from the input video file and converts it to MP3 format
    with optimized settings for transcription (mono, 16kHz sample rate). The extraction process
    uses ffmpeg with performance optimizations like multi-threading and VBR encoding.
    Returns:
        str: Path to the extracted audio file (format: 'audio-{INPUT_VIDEO_NAME}.mp3')
    Raises:
        ValueError: If the extracted audio file exceeds 25MB in size
    Notes:
        - Uses libmp3lame codec for faster MP3 encoding
        - Converts audio to mono channel
        - Downsamples to 16kHz for compatibility with Whisper
        - Uses variable bitrate (VBR) encoding
        - Utilizes all available CPU threads for processing
        - Uses a larger thread queue size for better throughput
        - Enables fast seeking for improved performance
    """
    logger = logging.getLogger("extract_audio")
    stopwatch.start("Audio Extraction")
    extracted_audio_path: str = (
        f"audio-{os.path.splitext(os.path.basename(video_path))[0]}.mp3"
    )

    try:
        # Add optimization flags to ffmpeg
        stream = ffmpeg.input(video_path)
        stream = ffmpeg.output(
            stream,
            extracted_audio_path,
            acodec="libmp3lame",  # Faster MP3 encoder
            ac=1,  # Convert to mono
            ar=16000,  # Lower sample rate (whisper uses 16kHz)
            **{
                "q:a": 0,  # VBR encoding
                "threads": 0,  # Use all CPU threads
                "thread_queue_size": 1024,  # Larger queue for better throughput
                "fflags": "+fastseek",  # Fast seeking
            },
        )

        ffmpeg.run(stream, overwrite_output=True)
    except Exception as e:
        logger.error(f"An error occurred while extracting audio: {e}")
    stopwatch.stop("Audio Extraction")
    return extracted_audio_path

@Halo(text="Transcribing....", text_color="green", spinner="dots", placement="right")
def transcribe(audio_path: str, device: str, model_size: str) -> Dict:
    logger = logging.getLogger("transcribe")
    stopwatch.start("Transcription")

    # Load model
    model = whisperx.load_model(model_size, device, compute_type="int8")

    # Initial transcription
    initial_result = model.transcribe(audio_path, batch_size=16)

    # Store language before alignment
    language = initial_result["language"]

    # Align timestamps
    model_a, metadata = whisperx.load_align_model(language_code=language, device=device)
    aligned_result = whisperx.align(
        initial_result["segments"], model_a, metadata, audio_path, device
    )

    # Get aligned segments
    segments = aligned_result["segments"]

    logger.info(f"Language: {language}")
    for segment in segments:
        logger.debug(
            f"[{segment['start']:.2f}s -> {segment['end']:.2f}s] {segment['text']}"
        )

    stopwatch.stop("Transcription")
    return language, segments


def generate_subtitles(segments: Dict) -> str:
    logger = logging.getLogger("generate_subtitles")
    srt_content = []
    for i, segment in enumerate(segments, start=1):
        segment_start = timer.Timer.format_time(segment["start"])
        segment_end = timer.Timer.format_time(segment["end"])
        text = segment["text"].strip()

        # SRT format: [segment number] [start] --> [end] [text]
        srt_content.append(f"{os.linesep}{i}")
        srt_content.append(f"{segment_start} --> {segment_end}")
        srt_content.append(f"{text}{os.linesep}")

    return f"{os.linesep}".join(srt_content)


def post_process(subtitles: str) -> str:
    logger = logging.getLogger("post_process")
    """Post-process the generated subtitles.
    This function performs additional processing on the generated subtitles to improve readability
    and ensure compliance with common subtitle standards.
    Args:
        subtitles (list): The generated subtitles as a list of strings
    Returns:
        str: The post-processed subtitles as a single string
    """

    # Clip lines that go over 150 characters taking into account word boundaries
    subtitles_clean: str = ""
    for line in subtitles:
        if len(line) > 150:
            try:
                line = line[:150].rsplit(" ", 1)[0]
            except ValueError:
                logger.warning(
                    f"Line too long and cannot be split: {line}. Clipping to 150 characters."
                )
                line = line[:150]
        else:
            pass

        subtitles_clean += line

    return subtitles_clean


def main():
    logger = logging.getLogger("main")
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Subtitle Generator")
    parser.add_argument(
        "-f",
        "--file",
        default=None,
        help="Path to the input media file",
    )
    parser.add_argument(
        "-d",
        "--directory",
        default=None,
        help="Path to directory containing media files",
    )
    parser.add_argument(
        "-c",
        "--compute_device",
        default=None,
        choices=["cuda", "cpu"],
        help="Device to use for computation (cuda or cpu)",
    )
    parser.add_argument(
        "-m",
        "--model_size",
        default="base.en",
        choices=MODELS_AVAILABLE,
        help="Whisper model size to use for transcription",
    )
    parser.add_argument(
        "-l",
        "--log-level",
        default="ERROR",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set the logging level",
    )
    args = parser.parse_args()

    # Set logging level
    logging_level = getattr(logging, args.log_level.upper(), logging.DEBUG)
    logging.getLogger().setLevel(logging_level)
    coloredlogs.install(level=logging_level)

    # If no args are passed to argparser, print help and exit
    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        sys.exit(1)

    # Check that args.directory is a valid directory only if specified in the arguments
    if args.directory and not os.path.isdir(args.directory):
        logger.error(f"Error: Directory '{args.directory}' does not exist.")
        return
    # Check that args.file is a valid file only if specified in the arguments
    if args.file and not os.path.isfile(args.file):
        logger.error(f"Error: File '{args.file}' does not exist.")
        return

    media_files = get_media_files(args.directory, args.file)
    if not media_files:
        return

    # Process each media file
    for media_file in media_files:
        input_media_path = media_file[0]
        audio_flag = media_file[1]
        file_name = str(os.path.basename(input_media_path.rsplit(".", 1)[0]))
        stopwatch.start(file_name)

        # Extract Audio
        if not audio_flag:
            logger.info(f"Processing video file: {input_media_path}")
            audio_path: str = extract_audio(video_path=input_media_path)
        else:
            logger.info(f"Processing audio file: {input_media_path}")
            audio_path = input_media_path

        # Transcribe audio
        language, segments = transcribe(
            audio_path=audio_path,
            device=get_device(args.compute_device.lower()),
            model_size=args.model_size.lower(),
        )

        # Generate unprocessed raw subtitles
        subtitles_raw: str = generate_subtitles(segments=segments)

        # Post-process subtitles
        subtitles: str = post_process(subtitles=subtitles_raw)

        # The following should generate something like "input.ai.srt" from "input.mp4"
        subtitle_file_name = f"{file_name}.ai-{language}.srt"

        # Write subtitles to file
        subtitle_path = os.path.join(
            os.path.dirname(input_media_path), subtitle_file_name
        )
        try:
            with open(subtitle_path, "w", encoding="utf-8") as f:
                f.write(subtitles)
                logger.info(f"Subtitle file generated: {subtitle_file_name}")
        except Exception as e:
            logger.error(f"An error occurred while writing the subtitle file: {e}")
        stopwatch.stop(file_name)

    # Print summary of processing times
    stopwatch.summary()


if __name__ == "__main__":
    import gc

    gc.collect()
    main()
    gc.collect()
